from __future__ import annotations

# ruff: noqa: E402, F811
import asyncio
import contextvars
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

pytest.importorskip("azure.ai.agentserver.responses")
pytest.importorskip("agent_framework.foundry")

from azure.monitor.opentelemetry.exporter.export.trace._exporter import _convert_span_to_envelope
from opentelemetry import trace
from opentelemetry.sdk.trace import Span, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, Decision, ParentBased, StaticSampler
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from app import agent_detail as detail
from app import telemetry
from app.foundry_agent_client import (
    FoundryAgentClient,
    GenerateCardAgentRequest,
    GenerateCardAgentResponse,
    _parse_success_envelope,
)
from app.generation import CardGenerationService, ModerationDecision
from hosted_agents.card_orchestrator import specialists
from hosted_agents.card_orchestrator.orchestrator import CardOrchestrator, RuntimeFailure
from hosted_agents.card_orchestrator.server import create_host
from hosted_agents.card_orchestrator.workflow import StageBoundary
from tests.test_agent_detail_telemetry import (
    PROMPT,
    LocalHostedTransport,
    capture,
    content,
    exported,  # noqa: F401
    generate,
    new_spans,
    record,
    stack,
)
from tests.test_agentic_generation import _client, _generate, _services
from tests.test_card_orchestrator import CARD, settings, wire
from tests.test_card_orchestrator_models import (  # noqa: F401
    model_response,
    model_transport,
    reasoning_response_item,
)

HOSTED_STAGES = tuple(specialists.SCHEMAS)
HOSTED_SPAN_COUNT = len(HOSTED_STAGES)


@contextmanager
def batched_provider(monkeypatch, sampler=None, *, queue_size=64):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=sampler or ParentBased(ALWAYS_ON))
    provider.add_span_processor(telemetry.PrivacySpanProcessor())
    processor = BatchSpanProcessor(
        exporter,
        max_queue_size=queue_size,
        max_export_batch_size=queue_size,
        schedule_delay_millis=60000,
    )
    provider.add_span_processor(processor)
    monkeypatch.setattr(telemetry, "_enabled", True)
    monkeypatch.setattr(telemetry, "_tracer", provider.get_tracer("baseline"))
    monkeypatch.setattr(detail, "_tracer", lambda: provider.get_tracer(detail.SCOPE))
    monkeypatch.setattr(detail, "_capacity", threading.BoundedSemaphore(16))
    try:
        yield provider, exporter, processor
    finally:
        provider.shutdown()


def assert_capacity_recovered():
    permits = [detail._capacity.acquire(blocking=False) for _ in range(17)]
    assert permits == [True] * 16 + [False]
    for _ in range(16):
        detail._capacity.release()
    assert detail.current() is None
    assert detail._ending.get() is None


def test_original_identity_stage_time_and_azure_batch_conversion(monkeypatch):
    with batched_provider(monkeypatch) as (provider, exporter, _):
        _, runtime, _, _ = stack(monkeypatch)
        stage_contexts, completed, finalized, captures = [], [], [], []
        original_run = StageBoundary.invoke
        original_candidate = detail.candidate
        original_end = Span.end

        async def run(self, context, call_next):
            stage_contexts.append(trace.get_current_span().get_span_context())
            await asyncio.sleep(0.002)
            return await original_run(self, context, call_next)

        async def candidate(*args, **kwargs):
            state = detail.current()
            captures.append(state)
            saved = state.pending[-1]
            completed.append(saved)
            assert saved.span.is_recording()
            assert trace.get_current_span().get_span_context() == saved.span.parent
            assert "fcg.detail.record" not in saved.span.attributes
            assert not content(exporter)
            await asyncio.sleep(0.01)
            await original_candidate(*args, **kwargs)

        def end(self, end_time=None):
            if self.instrumentation_scope.name == detail.SCOPE:
                finalized.append((self.get_span_context(), end_time, time.time_ns()))
            return original_end(self, end_time=end_time)

        monkeypatch.setattr(StageBoundary, "invoke", run)
        monkeypatch.setattr(detail, "candidate", candidate)
        monkeypatch.setattr(Span, "end", end)
        with provider.get_tracer("baseline").start_as_current_span("parent") as parent:
            parent_context = parent.get_span_context()
            result = asyncio.run(runtime.generate(GenerateCardAgentRequest(query=PROMPT)))
            assert trace.get_current_span() is parent
            assert result.status == "completed"
            assert "agentDetail" not in result.metadata
        assert provider.force_flush()
        spans = new_spans(exporter)
        assert [s.name for s in spans] == ["card_generation"]
        assert len(finalized) == HOSTED_SPAN_COUNT
        generation_context = spans[0].parent
        generation = next(
            s for s in exporter.get_finished_spans() if s.context == generation_context
        )
        assert generation.parent == parent_context
        for span, saved, current, ended in zip(spans, completed, stage_contexts, finalized):
            assert span.context == current == ended[0]
            assert span.parent == generation_context
            assert span.end_time == saved.end_time == ended[1]
            assert ended[2] - span.end_time > 8_000_000
            assert span.start_time < span.end_time
            value = json.loads(span.attributes["fcg.detail.record"])
            assert value["source"] == {
                "trace_id": f"{span.context.trace_id:032x}",
                "span_id": f"{span.context.span_id:016x}",
            }
            assert value["duration_ms"] == dict(saved.attributes)["fcg.detail.duration_ms"]
            assert abs(value["duration_ms"] - (span.end_time - span.start_time) / 1e6) < 5
            converted = _convert_span_to_envelope(span).as_dict()
            data = converted["data"]["baseData"]
            assert json.loads(data["properties"]["fcg.detail.record"]) == value
            assert data["id"] == f"{span.context.span_id:016x}"
            assert (
                converted["tags"]["ai.operation.parentId"] == f"{generation_context.span_id:016x}"
            )
            duration_ms = (span.end_time - span.start_time + 500000) // 1000000
            assert data["duration"] == f"0.00:00:00.{duration_ms:03d}"
            assert datetime.fromisoformat(converted["time"]) == datetime.fromtimestamp(
                span.start_time / 1e9, tz=timezone.utc
            )
        art = spans[-1]
        assert art.attributes["fcg.detail.moderation"] == "allowed"
        assert json.loads(art.attributes["fcg.detail.record"])["moderation"] == "allowed"
        assert all(
            c.closed and not c.pending and not c.records and c.execution is None for c in captures
        )
        assert_capacity_recovered()


@pytest.mark.parametrize("decision", [Decision.DROP, Decision.RECORD_ONLY])
def test_unsampled_has_no_capture_processing_or_sampling_resurrection(monkeypatch, decision):
    with batched_provider(monkeypatch, StaticSampler(decision)) as (provider, exporter, _):
        services, _, _, calls = stack(monkeypatch)

        def forbidden(*args, **kwargs):
            pytest.fail("sampled-out execution performed capture-only work")

        for name in ("candidate", "projection_view", "_contracts", "parse_envelope"):
            monkeypatch.setattr(detail, name, forbidden)
        assert asyncio.run(generate(CardGenerationService(services))).status == "completed"
        assert len(calls) == HOSTED_SPAN_COUNT
        assert provider.force_flush()
        assert not content(exporter)
        assert_capacity_recovered()


def test_parent_based_remote_unsampled_is_not_resurrected(monkeypatch):
    with batched_provider(monkeypatch) as (provider, exporter, _):
        _, runtime, _, _ = stack(monkeypatch)
        parent = NonRecordingSpan(SpanContext(123, 456, True, TraceFlags(0)))
        with trace.use_span(parent):
            response = asyncio.run(runtime.generate(GenerateCardAgentRequest(query=PROMPT)))
            assert trace.get_current_span() is parent
        assert response.status == "completed"
        provider.force_flush()
        assert not new_spans(exporter)
        assert_capacity_recovered()


def test_parent_sampled_decision_is_preserved_when_root_sampling_is_off(monkeypatch):
    with batched_provider(monkeypatch, ParentBased(StaticSampler(Decision.DROP))) as (
        provider,
        exporter,
        _,
    ):
        _, runtime, _, _ = stack(monkeypatch)
        parent = NonRecordingSpan(SpanContext(123, 456, True, TraceFlags(1)))
        with trace.use_span(parent):
            response = asyncio.run(runtime.generate(GenerateCardAgentRequest(query=PROMPT)))
            assert trace.get_current_span() is parent
        assert response.status == "completed"
        provider.force_flush()
        spans = new_spans(exporter)
        assert len(spans) == len(converted_records(exporter)) == HOSTED_SPAN_COUNT
        assert all(s.context.trace_id == 123 and s.context.trace_flags.sampled for s in spans)
        assert_capacity_recovered()


@pytest.mark.parametrize(
    "mutation",
    [
        "source",
        "stage",
        "runtime",
        "agent",
        "operation",
        "attempt",
        "duration_ms",
        "deployment_version",
        "result",
        "validation",
        "instruction",
        "extra",
        "foreign_scope",
        "foreign_name",
    ],
)
def test_original_export_rejects_mismatched_identity_and_content(monkeypatch, exported, mutation):
    tracer = detail._tracer()
    provider = None
    if mutation == "foreign_scope":
        provider = TracerProvider()
        provider.add_span_processor(telemetry.PrivacySpanProcessor())
        provider.add_span_processor(SimpleSpanProcessor(exported))
        tracer = provider.get_tracer("untrusted.sdk")
    span = tracer.start_span("invoke_agent" if mutation == "foreign_name" else "card_generation")
    value = record("hosted", source=detail._source(span))
    attributes = {
        "fcg.detail.runtime": "hosted",
        "fcg.detail.stage": "generation",
        "fcg.detail.agent": "card_generation",
        "fcg.detail.operation": "generate",
        "fcg.detail.attempt": 1,
        "fcg.detail.duration_ms": 1,
        "fcg.detail.deployment_version": "test",
        "fcg.detail.result": "completed",
        "fcg.detail.reason": "allowed",
        "fcg.detail.validation": "validated",
        "fcg.detail.moderation": "allowed",
    }
    payload = value.model_dump(mode="json")
    if mutation == "source":
        payload["source"]["span_id"] = "f" * 16
    elif mutation == "instruction":
        payload["instruction"]["text"] = "SYNTHETIC-PRIVATE-CONTENT"
    elif mutation == "extra":
        payload["raw"] = "SYNTHETIC-PRIVATE-CONTENT"
    elif mutation not in {"foreign_scope", "foreign_name"}:
        key = f"fcg.detail.{mutation}"
        attributes[key] = 2 if type(attributes[key]) is int else "wrong"
    span.set_attributes(attributes)
    span.set_attribute("fcg.detail.record", detail.encoded(payload).decode())
    span.end()
    assert not content(exported)
    converted = json.dumps(
        [_convert_span_to_envelope(s).as_dict() for s in exported.get_finished_spans()]
    )
    assert "SYNTHETIC-PRIVATE-CONTENT" not in converted
    if provider:
        provider.shutdown()


@pytest.mark.parametrize("mutation", ["source", "duration", "instruction", "unknown_field"])
def test_whole_hosted_batch_preflight_rejects_last_candidate(monkeypatch, exported, mutation):
    _, runtime, _, _ = stack(monkeypatch)
    original = detail.Capture._release
    captures = []

    def release(self):
        captures.append(self)
        value = self.records[-1]
        updates = {
            "source": {"source": detail.Source(trace_id="f" * 32, span_id="e" * 16)},
            "duration": {"duration_ms": value.duration_ms + 1},
            "instruction": {"instruction": detail.View(text="not the effective instructions")},
            "unknown_field": {"output": detail.View(text='{"raw":"unchecked"}')},
        }
        self.records[-1] = value.model_copy(update=updates[mutation])
        return original(self)

    monkeypatch.setattr(detail.Capture, "_release", release)
    result = asyncio.run(runtime.generate(GenerateCardAgentRequest(query=PROMPT)))
    assert result.status == "completed"
    assert not content(exported)
    assert len(new_spans(exported)) == HOSTED_SPAN_COUNT
    assert all(c.closed and not c.pending and not c.records for c in captures)
    assert_capacity_recovered()


def converted_records(exporter):
    return [
        json.loads(properties["fcg.detail.record"])
        for span in exporter.get_finished_spans()
        if "fcg.detail.record"
        in (
            properties := _convert_span_to_envelope(span).as_dict()["data"]["baseData"][
                "properties"
            ]
        )
    ]


def observe_hosted_lifecycle(monkeypatch):
    observed = {"created": [], "captures": [], "ended": [], "attached": []}
    tracer = detail._tracer()
    original_start, original_init = tracer.start_span, detail.Capture.__init__
    original_end, original_attribute = Span.end, Span.set_attribute

    def start(*args, **kwargs):
        span = original_start(*args, **kwargs)
        # NonRecordingSpan sampling results are not owned recording SDK originals.
        if isinstance(span, Span):
            observed["created"].append(span)
        return span

    def initialize(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        observed["captures"].append(self)

    def end(self, end_time=None):
        if self.instrumentation_scope.name == detail.SCOPE:
            observed["ended"].append((self, end_time, time.monotonic()))
        return original_end(self, end_time=end_time)

    def attribute(self, key, value):
        if key == "fcg.detail.record":
            observed["attached"].append((self, time.monotonic()))
        return original_attribute(self, key, value)

    monkeypatch.setattr(tracer, "start_span", start)
    monkeypatch.setattr(detail, "_tracer", lambda: tracer)
    monkeypatch.setattr(detail.Capture, "__init__", initialize)
    monkeypatch.setattr(Span, "end", end)
    monkeypatch.setattr(Span, "set_attribute", attribute)
    return observed


def assert_owned_lifecycle_drained(observed):
    created = observed["created"]
    ended = [span for span, _, _ in observed["ended"]]
    assert len(ended) == len(created)
    assert {id(span) for span in ended} == {id(span) for span in created}
    assert len({id(span) for span in ended}) == len(ended)
    assert all(not span.is_recording() for span in created)
    assert observed["captures"]
    assert all(
        c.closed
        and not c.pending
        and not c.records
        and not c._approved_records
        and c.execution is None
        for c in observed["captures"]
    )
    assert_capacity_recovered()


@pytest.mark.parametrize(
    "boundary",
    [
        "serialization",
        "revalidation",
        "last_pending",
        "last_record",
        "aggregate",
        "remaining_budget",
    ],
)
@pytest.mark.parametrize("phase", ["whole_request", "late_boundary"])
def test_original_deadline_covers_sync_acceptance_preflight(
    monkeypatch, exported, record_property, boundary, phase, model_transport
):
    budget = 2 if phase == "late_boundary" else (0.3 if boundary == "remaining_budget" else 0.1)
    runtime = CardOrchestrator(settings(timeout_seconds=budget))
    observed = observe_hosted_lifecycle(monkeypatch)
    events = []
    in_release = False
    original_serialize = GenerateCardAgentResponse.model_dump_json
    original_validate = GenerateCardAgentResponse.model_validate_json
    original_pending, original_record = detail._validate_pending, detail._validate_original
    original_encoded, original_release = detail.encoded, detail.Capture._release
    deadlines = []
    original_factory = runtime.specialist_factory

    @asynccontextmanager
    async def factory(config):
        deadlines.append(detail.current_deadline())
        async with original_factory(config) as owned:
            yield owned

    runtime.specialist_factory = factory

    if boundary == "remaining_budget":
        original_gate = runtime._gate

        async def gate(*args):
            await original_gate(*args)
            if args[2] == "final_art_prompt":
                await asyncio.sleep(0.18)

        runtime._gate = gate

    def delay(name):
        target = "serialization" if boundary == "remaining_budget" else boundary
        if name != target or events:
            return
        assert len(observed["created"]) == HOSTED_SPAN_COUNT
        assert not observed["attached"]
        assert not content(exported)
        state = detail.current()
        assert len(state.records) == len(state.pending) == HOSTED_SPAN_COUNT
        assert state.deadline == detail.current_deadline() == deadlines[0]
        assert all(p.deadline == deadlines[0] for p in state.pending)
        if phase == "late_boundary":
            remaining = deadlines[0] - asyncio.get_running_loop().time()
            assert remaining > 0.1
            time.sleep(remaining - 0.1)
            record_property(
                "target_remaining_seconds", deadlines[0] - asyncio.get_running_loop().time()
            )
        events.append((name, time.monotonic()))
        # Blocking work need not be preempted, but cannot authorize an expired release.
        time.sleep(0.18 if boundary == "remaining_budget" else 0.2)
        events.append(("blocking_work_returned", time.monotonic()))

    def serialize(self, *args, **kwargs):
        result = original_serialize(self, *args, **kwargs)
        delay("serialization")
        return result

    def validate(*args, **kwargs):
        result = original_validate(*args, **kwargs)
        delay("revalidation")
        return result

    def pending(owned, span, *, ending=False):
        result = original_pending(owned, span, ending=ending)
        if not ending and owned.stage == "generation":
            delay("last_pending")
        return result

    def record_check(record, *args, **kwargs):
        result = original_record(record, *args, **kwargs)
        if in_release and record.stage == "generation":
            delay("last_record")
        return result

    def encoded(value):
        result = original_encoded(value)
        if (
            in_release
            and type(value) is dict
            and value.keys() == {"version", "records"}
            and len(value["records"]) == HOSTED_SPAN_COUNT
        ):
            delay("aggregate")
        return result

    def release(self):
        nonlocal in_release
        in_release = True
        try:
            return original_release(self)
        finally:
            in_release = False

    monkeypatch.setattr(GenerateCardAgentResponse, "model_dump_json", serialize)
    monkeypatch.setattr(GenerateCardAgentResponse, "model_validate_json", validate)
    monkeypatch.setattr(detail, "_validate_pending", pending)
    monkeypatch.setattr(detail, "_validate_original", record_check)
    monkeypatch.setattr(detail, "encoded", encoded)
    monkeypatch.setattr(detail.Capture, "_release", release)

    async def scenario():
        with telemetry._tracer.start_as_current_span("outer") as outer:
            started = time.monotonic()
            try:
                with pytest.raises(RuntimeFailure) as failure:
                    await runtime.generate(GenerateCardAgentRequest(query=PROMPT))
                assert failure.value.reason.value == "timeout"
            finally:
                finished = time.monotonic()
                record_property("elapsed_seconds", finished - started)
                if phase == "whole_request":
                    assert finished - started < 0.5
                else:
                    assert finished - started >= budget
                    record_property("target_to_cleanup_seconds", finished - events[0][1])
                    assert finished - events[0][1] < 0.5
                record_property(
                    "lifecycle",
                    [(name, at - started) for name, at in events]
                    + [("end_attempt", at - started) for _, _, at in observed["ended"]]
                    + [("attachment", at - started) for _, at in observed["attached"]],
                )
                # Assert inside the invoking task, not after asyncio.run discarded its context.
                assert trace.get_current_span() is outer
                assert detail.current() is None

    asyncio.run(scenario())
    if phase == "late_boundary":
        assert len(events) == 2
        assert events[-1][1] - events[0][1] >= (0.18 if boundary == "remaining_budget" else 0.2)
        assert len(model_transport[0]) == len(new_spans(exported)) == HOSTED_SPAN_COUNT
        assert_owned_lifecycle_drained(observed)
    else:
        assert not events
        assert all(c.closed and not c.pending and not c.records for c in observed["captures"])
        assert_capacity_recovered()
    assert not observed["attached"]
    assert not converted_records(exported)


@pytest.mark.parametrize("phase", ["whole_request", "late_boundary"])
def test_original_deadline_covers_async_resource_finalization(
    monkeypatch, exported, record_property, phase, model_transport
):
    runtime = CardOrchestrator(settings(timeout_seconds=2 if phase == "late_boundary" else 0.1))
    observed = observe_hosted_lifecycle(monkeypatch)
    events = []

    original_factory = runtime.specialist_factory

    @asynccontextmanager
    async def factory(config):
        deadline = detail.current_deadline()
        try:
            async with original_factory(config) as owned:
                yield owned
                state = detail.current()
                assert len(state.pending) == len(state.records) == HOSTED_SPAN_COUNT
                assert state.deadline == deadline == detail.current_deadline()
                assert all(p.deadline == deadline for p in state.pending)
                remaining = deadline - asyncio.get_running_loop().time()
                assert remaining > 0.1
                await asyncio.sleep(remaining - 0.1)
                events.append(("finalization_entered", time.monotonic()))
                await asyncio.sleep(10)
        finally:
            events.append(("finalization_exited", time.monotonic()))

    runtime.specialist_factory = factory

    async def scenario():
        with telemetry._tracer.start_as_current_span("outer") as outer:
            started = time.monotonic()
            with pytest.raises(RuntimeFailure) as failure:
                await runtime.generate(GenerateCardAgentRequest(query=PROMPT))
            elapsed = time.monotonic() - started
            record_property("elapsed_seconds", elapsed)
            record_property("lifecycle", [(name, at - started) for name, at in events])
            assert failure.value.reason.value == "timeout"
            if phase == "whole_request":
                assert elapsed < 1
            else:
                assert elapsed >= 2
                tail = time.monotonic() - events[0][1]
                record_property("target_to_cleanup_seconds", tail)
                assert tail < 1
            assert trace.get_current_span() is outer
            assert detail.current() is None

    asyncio.run(scenario())
    if phase == "late_boundary":
        assert [name for name, _ in events] == ["finalization_entered", "finalization_exited"]
        assert len(model_transport[0]) == HOSTED_SPAN_COUNT
        assert_owned_lifecycle_drained(observed)
    else:
        assert all(name != "finalization_entered" for name, _ in events)
        assert_capacity_recovered()
    assert not observed["attached"]
    assert not converted_records(exported)


@pytest.mark.parametrize("stage", ["generation"])
@pytest.mark.parametrize("boundary", ["before_attributes", "after_attributes"])
@pytest.mark.parametrize("failure", ["cancel", "exception"])
def test_stage_exit_structural_failure_finalizes_and_restores_same_task_context(
    monkeypatch, exported, caplog, record_property, stage, boundary, failure
):
    _, runtime, _, _ = stack(monkeypatch)
    observed = observe_hosted_lifecycle(monkeypatch)
    original_set = Span.set_attributes
    interrupted = []

    def attributes(self, values):
        matches = (
            self.instrumentation_scope.name == detail.SCOPE
            and values.get("fcg.detail.stage") == stage
            and "fcg.detail.record" not in values
        )
        if not matches:
            return original_set(self, values)
        interrupted.append(self)
        assert self.is_recording()
        assert trace.get_current_span() is self
        if boundary == "after_attributes":
            original_set(self, values)
        if failure == "cancel":
            raise asyncio.CancelledError("SYNTHETIC-PRIVATE-STAGE-EXIT")
        raise RuntimeError("SYNTHETIC-PRIVATE-STAGE-EXIT")

    monkeypatch.setattr(Span, "set_attributes", attributes)

    async def scenario():
        with telemetry._tracer.start_as_current_span("outer") as outer:
            try:
                if failure == "cancel":
                    with pytest.raises(asyncio.CancelledError):
                        await runtime.generate(GenerateCardAgentRequest(query=PROMPT))
                else:
                    result = await runtime.generate(GenerateCardAgentRequest(query=PROMPT))
                    assert result.status == "completed"
            finally:
                record_property(
                    "lifecycle",
                    {
                        "created": len(observed["created"]),
                        "end_attempts": len(observed["ended"]),
                        "still_recording": sum(s.is_recording() for s in observed["created"]),
                        "attachments": len(observed["attached"]),
                        "outer_restored": trace.get_current_span() is outer,
                    },
                )
                assert trace.get_current_span() is outer
                assert detail.current() is None
                assert detail._ending.get() is None

    asyncio.run(scenario())
    assert len(interrupted) == 1
    expected = ["generation"].index(stage) + 1 if failure == "cancel" else HOSTED_SPAN_COUNT
    assert len(observed["created"]) == expected
    assert not observed["attached"]
    assert not converted_records(exported)
    assert "SYNTHETIC-PRIVATE-STAGE-EXIT" not in caplog.text
    assert_owned_lifecycle_drained(observed)


@pytest.mark.parametrize("parent_kind", ["remote_unsampled", "root_drop", "root_record_only"])
def test_nonrecording_requests_cannot_starve_sampled_hosted_capture(
    monkeypatch, record_property, parent_kind
):
    root_decision = Decision.RECORD_ONLY if parent_kind == "root_record_only" else Decision.DROP
    sampler = ParentBased(
        ALWAYS_ON if parent_kind == "remote_unsampled" else StaticSampler(root_decision)
    )
    with batched_provider(monkeypatch, sampler) as (provider, exporter, _):
        _, runtime, _, _ = stack(monkeypatch)
        observed = observe_hosted_lifecycle(monkeypatch)
        original_gate = runtime._gate
        held = []

        async def scenario():
            release, all_held = asyncio.Event(), asyncio.Event()

            async def gate(*args):
                if (
                    args[2] == "final_text"
                    and not trace.get_current_span().get_span_context().trace_flags.sampled
                ):
                    held.append(detail.current())
                    if len(held) == 16:
                        all_held.set()
                    await release.wait()
                return await original_gate(*args)

            async def unsampled(index):
                parent = (
                    NonRecordingSpan(SpanContext(123 + index, 456 + index, True, TraceFlags(0)))
                    if parent_kind == "remote_unsampled"
                    else trace.INVALID_SPAN
                )
                with trace.use_span(parent):
                    result = await runtime.generate(GenerateCardAgentRequest(query=PROMPT))
                    assert trace.get_current_span() is parent
                    return result

            runtime._gate = gate
            tasks = [asyncio.create_task(unsampled(i)) for i in range(16)]
            try:
                async with asyncio.timeout(5):
                    await all_held.wait()
                allocations_while_held = list(observed["captures"])
                held_buffers = list(held)
                permits = []
                try:
                    for _ in range(17):
                        permits.append(detail._capacity.acquire(blocking=False))
                finally:
                    for acquired in permits:
                        if acquired:
                            detail._capacity.release()
                sampled_parent = NonRecordingSpan(SpanContext(999, 888, True, TraceFlags(1)))
                with trace.use_span(sampled_parent):
                    response = await runtime.generate(GenerateCardAgentRequest(query=PROMPT))
                    assert trace.get_current_span() is sampled_parent
                assert response.status == "completed"
                assert provider.force_flush()
                # Verify usable sampling/capture while all sixteen ineligible calls remain live.
                spans = new_spans(exporter)
                record_property(
                    "lifecycle",
                    {
                        "ineligible_capture_allocations": len(allocations_while_held),
                        "ineligible_retained_buffers": sum(c is not None for c in held_buffers),
                        "available_permits": sum(permits),
                        "sampled_originals": len(spans),
                        "sampled_records": len(converted_records(exporter)),
                    },
                )
                assert [s.name for s in spans] == ["card_generation"] * HOSTED_SPAN_COUNT
                assert len(converted_records(exporter)) == HOSTED_SPAN_COUNT
                assert all(s.context.trace_id == 999 for s in spans)
                assert not allocations_while_held
                assert held_buffers == [None] * 16
                assert permits == [True] * 16 + [False]
            finally:
                release.set()
                results = await asyncio.gather(*tasks, return_exceptions=True)
            assert all(
                isinstance(r, GenerateCardAgentResponse) and r.status == "completed"
                for r in results
            )

        asyncio.run(scenario())
        assert provider.force_flush()
        assert len(converted_records(exporter)) == HOSTED_SPAN_COUNT
        assert len(observed["captures"]) == 1
        assert len(observed["created"]) == HOSTED_SPAN_COUNT
        assert_owned_lifecycle_drained(observed)


def test_late_recording_stage_uses_actual_sampler_decision_before_capture(monkeypatch):
    class DropConceptSampler(ParentBased):
        def should_sample(self, parent_context, trace_id, name, *args, **kwargs):
            if name == "card_generation":
                return StaticSampler(Decision.DROP).should_sample(
                    parent_context, trace_id, name, *args, **kwargs
                )
            return super().should_sample(parent_context, trace_id, name, *args, **kwargs)

    with batched_provider(monkeypatch, DropConceptSampler(ALWAYS_ON)) as (provider, exporter, _):
        _, runtime, _, _ = stack(monkeypatch)
        observed = observe_hosted_lifecycle(monkeypatch)
        original_run, original_candidate = StageBoundary.invoke, detail.candidate
        stages, candidates = [], []

        async def run(self, context, call_next):
            stage = self.stage
            span = trace.get_current_span()
            stages.append((stage, span.is_recording(), detail.current()))
            if stage == "generation":
                assert not observed["captures"]
                assert_capacity_recovered()
            return await original_run(self, context, call_next)

        async def candidate(stage, *args, **kwargs):
            candidates.append(stage)
            return await original_candidate(stage, *args, **kwargs)

        monkeypatch.setattr(StageBoundary, "invoke", run)
        monkeypatch.setattr(detail, "candidate", candidate)

        async def scenario():
            parent = NonRecordingSpan(SpanContext(123, 456, True, TraceFlags(1)))
            with trace.use_span(parent):
                response = await runtime.generate(GenerateCardAgentRequest(query=PROMPT))
                assert response.status == "completed"
                assert trace.get_current_span() is parent

        asyncio.run(scenario())
        assert [(stage, recording) for stage, recording, _ in stages] == [("generation", False)]
        assert stages[0][2] is None
        assert not observed["captures"]
        assert candidates == []
        assert provider.force_flush()
        assert not new_spans(exporter)
        assert not converted_records(exporter)
        assert not observed["created"] and not observed["ended"] and not observed["attached"]


@pytest.mark.parametrize(
    "mutation",
    [
        "attempt",
        "name",
        "legacy_name",
        "parent",
        "start_time",
        "scope",
        "foreign_scope",
        "context",
        "context_flags",
        "parent_flags",
        "duration_ms",
        "moderation",
        "stage",
        "runtime",
        "agent",
        "operation",
        "deployment_version",
        "result",
        "reason",
        "validation",
    ],
)
def test_last_live_original_corruption_preflights_entire_batch(monkeypatch, exported, mutation):
    _, runtime, _, _ = stack(monkeypatch)
    original_release, original_end = detail.Capture._release, Span.end
    retained, ended = [], []

    def release(self):
        retained.extend(self.pending)
        span = self.pending[-1].span
        if mutation in {"name", "legacy_name"}:
            span.update_name(
                "card_generation_invalid" if mutation == "name" else "fcg.agent.detail"
            )
        elif mutation == "parent":
            span._parent = SpanContext(span.context.trace_id, 987, False, TraceFlags(1))
        elif mutation == "start_time":
            span._start_time += 1
        elif mutation in {"scope", "foreign_scope"}:
            span._instrumentation_scope = InstrumentationScope(
                detail.SCOPE if mutation == "scope" else "foreign.sdk", version="wrong"
            )
        elif mutation == "context":
            span._context = SpanContext(span.context.trace_id, 987, False, TraceFlags(1))
        elif mutation in {"context_flags", "parent_flags"}:
            context = span.context if mutation == "context_flags" else span.parent
            changed = SpanContext(context.trace_id, context.span_id, False, TraceFlags(0))
            if mutation == "context_flags":
                span._context = changed
            else:
                span._parent = changed
        else:
            key = f"fcg.detail.{mutation}"
            current = span.attributes[key]
            invalid = {
                "duration_ms": current + 1,
                "moderation": "blocked",
                "stage": "hosted_invocation",
                "runtime": "web",
                "agent": "card_orchestrator",
                "operation": "artwork_retry",
                "deployment_version": "",
                "result": "failed",
                "reason": "not_allowed",
                "validation": "modified",
                "attempt": 2,
            }.get(mutation)
            span.set_attribute(
                key,
                (
                    invalid
                    if invalid is not None
                    else (current + 1 if type(current) is int else "invalid")
                ),
            )
        return original_release(self)

    def end(self, end_time=None):
        if any(self is saved.span for saved in retained):
            ended.append((self, end_time))
        return original_end(self, end_time=end_time)

    monkeypatch.setattr(detail.Capture, "_release", release)
    monkeypatch.setattr(Span, "end", end)
    response = asyncio.run(runtime.generate(GenerateCardAgentRequest(query=PROMPT)))
    assert response.status == "completed"
    assert not converted_records(exported)
    assert not content(exported)
    assert len(ended) == HOSTED_SPAN_COUNT
    assert all(ended[i] == (saved.span, saved.end_time) for i, saved in enumerate(retained))
    assert all(not saved.span.is_recording() for saved in retained)
    assert_capacity_recovered()


@pytest.mark.parametrize("unsafe", [False, True])
def test_unowned_original_lookalike_never_exports_content(exported, unsafe):
    from app.generation import HeuristicModerationService

    span = detail._tracer().start_span("card_generation")
    value = record("hosted", source=detail._source(span))
    if unsafe:
        query = "graphic gore"
        decision = asyncio.run(
            HeuristicModerationService("original-fantasy-v1", record_telemetry=False).moderate_text(
                query, stage="post_text"
            )
        )
        assert not decision.allowed
        value = value.model_copy(update={"input": detail.View(text=json.dumps({"query": query}))})
    wire_value = detail.encoded(value.model_dump(mode="json")).decode()
    detail.Record.model_validate_json(wire_value, strict=True)
    span.set_attributes(
        {
            "fcg.detail.runtime": "hosted",
            "fcg.detail.stage": value.stage,
            "fcg.detail.agent": value.agent_name,
            "fcg.detail.operation": value.operation,
            "fcg.detail.attempt": value.attempt,
            "fcg.detail.duration_ms": value.duration_ms,
            "fcg.detail.deployment_version": value.deployment_version,
            "fcg.detail.result": "completed",
            "fcg.detail.reason": "allowed",
            "fcg.detail.validation": "validated",
            "fcg.detail.moderation": "allowed",
            "fcg.detail.record": wire_value,
        }
    )
    assert detail.current() is None
    span.end()
    assert not converted_records(exported)
    assert not content(exported)


def test_schema_valid_record_replacement_is_not_candidate_approval(monkeypatch, exported):
    _, runtime, _, _ = stack(monkeypatch)
    original = detail.Capture._release

    def release(self):
        value = self.records[-1].model_copy(
            update={
                "input": detail.View(text=detail.encoded(CARD | {"name": "graphic gore"}).decode())
            }
        )
        detail.Record.model_validate_json(
            detail.encoded(value.model_dump(mode="json")), strict=True
        )
        self.records[-1] = value
        return original(self)

    monkeypatch.setattr(detail.Capture, "_release", release)
    assert (
        asyncio.run(runtime.generate(GenerateCardAgentRequest(query=PROMPT))).status == "completed"
    )
    assert not converted_records(exported)
    assert len(new_spans(exported)) == HOSTED_SPAN_COUNT
    assert_capacity_recovered()


def test_preflight_checks_retained_span_even_without_a_candidate(monkeypatch, exported):
    _, runtime, _, _ = stack(monkeypatch)
    original = detail.Capture._release

    def release(self):
        self.records.pop()
        self.pending[-1].span.set_attribute("fcg.detail.attempt", 2)
        return original(self)

    monkeypatch.setattr(detail.Capture, "_release", release)
    assert (
        asyncio.run(runtime.generate(GenerateCardAgentRequest(query=PROMPT))).status == "completed"
    )
    assert not converted_records(exported)
    assert len(new_spans(exported)) == HOSTED_SPAN_COUNT
    assert_capacity_recovered()


@pytest.mark.parametrize(
    "mutation", ["record", "parent", "name", "start_time", "end_time", "approved_batch"]
)
def test_ending_rechecks_exact_record_identity_and_frozen_times(monkeypatch, exported, mutation):
    _, runtime, _, _ = stack(monkeypatch)
    original_end, original_finish = Span.end, detail.Capture.finish_hosted
    authorizations, contexts, captures = [], [], []

    def changed_record(value):
        payload = json.loads(value)
        payload["input"]["text"] = json.dumps({"query": "graphic gore"})
        changed = detail.encoded(payload).decode()
        detail.Record.model_validate_json(changed, strict=True)
        return changed

    def finish(self, batch=None):
        captures.append(self)
        if batch and mutation == "approved_batch":
            first = self.pending[0].source.span_id
            batch[first] = changed_record(batch[first])
        return original_finish(self, batch)

    def end(self, end_time=None):
        if self.name == "card_generation":
            authorization = detail._ending.get()
            assert list(authorization) == [id(self)]
            authorizations.append(authorization)
            contexts.append(contextvars.copy_context())
            if mutation == "record":
                self.set_attribute(
                    "fcg.detail.record", changed_record(self.attributes["fcg.detail.record"])
                )
            elif mutation == "parent":
                self._parent = SpanContext(self.context.trace_id, 987, False, TraceFlags(1))
            elif mutation == "name":
                self.update_name("fcg.agent.detail")
            elif mutation == "start_time":
                self._start_time += 1
            elif mutation == "end_time":
                end_time += 1
        return original_end(self, end_time=end_time)

    monkeypatch.setattr(detail.Capture, "finish_hosted", finish)
    monkeypatch.setattr(Span, "end", end)
    assert (
        asyncio.run(runtime.generate(GenerateCardAgentRequest(query=PROMPT))).status == "completed"
    )
    assert not converted_records(exported)
    assert all(not value for value in authorizations)
    assert all(not context.run(detail._ending.get) for context in contexts)
    assert all(not c._approved_records and not c.pending and not c.records for c in captures)
    assert_capacity_recovered()


def test_matching_context_clone_cannot_borrow_active_original_authorization(monkeypatch, exported):
    _, runtime, _, _ = stack(monkeypatch)
    original_processor = telemetry.PrivacySpanProcessor._on_ending
    originals, clones, authorizations, contexts = [], [], [], []

    def process(self, span):
        authorization = detail._ending.get()
        if authorization and id(span) in authorization:
            assert isinstance(span, Span)
            assert list(authorization) == [id(span)]
            owned, accepted, _ = authorization[id(span)]
            assert owned.span is span
            assert accepted == span.attributes["fcg.detail.record"]
            authorizations.append(authorization)
            contexts.append(contextvars.copy_context())
            originals.append(span)
            clone = detail._tracer().start_span(span.name)
            clone._context, clone._parent = span.context, span.parent
            clone._start_time = span.start_time
            clone.set_attributes(dict(span.attributes))
            clones.append(clone)
            clone.end(end_time=span.end_time)
        return original_processor(self, span)

    monkeypatch.setattr(telemetry.PrivacySpanProcessor, "_on_ending", process)
    assert (
        asyncio.run(runtime.generate(GenerateCardAgentRequest(query=PROMPT))).status == "completed"
    )
    assert len(originals) == len(clones) == len(converted_records(exported)) == HOSTED_SPAN_COUNT
    assert all("fcg.detail.record" not in clone.attributes for clone in clones)
    assert all("fcg.detail.record" in original.attributes for original in originals)
    assert all(
        span is not original for span in exported.get_finished_spans() for original in originals
    )
    assert all(not value for value in authorizations)
    assert all(not context.run(detail._ending.get) for context in contexts)
    assert_capacity_recovered()


@pytest.mark.parametrize("failure", ["attributes", "end", "processor"])
def test_release_authorization_is_cleared_on_instrumentation_failure(
    monkeypatch, exported, caplog, failure
):
    _, runtime, _, _ = stack(monkeypatch)
    original_end, original_set = Span.end, Span.set_attribute
    original_process = telemetry.PrivacySpanProcessor._on_ending
    authorizations, contexts = [], []

    def fail(span):
        if span.name == "card_generation":
            authorization = detail._ending.get()
            assert list(authorization) == [id(span)]
            authorizations.append(authorization)
            contexts.append(contextvars.copy_context())
            raise RuntimeError("SYNTHETIC-PRIVATE-FAILURE")

    def end(self, end_time=None):
        if failure == "end":
            fail(self)
        return original_end(self, end_time=end_time)

    def attribute(self, key, value):
        if key == "fcg.detail.record" and failure == "attributes":
            fail(self)
        return original_set(self, key, value)

    def process(self, span):
        if failure == "processor":
            fail(span)
        return original_process(self, span)

    monkeypatch.setattr(Span, "end", end)
    monkeypatch.setattr(Span, "set_attribute", attribute)
    monkeypatch.setattr(telemetry.PrivacySpanProcessor, "_on_ending", process)
    assert (
        asyncio.run(runtime.generate(GenerateCardAgentRequest(query=PROMPT))).status == "completed"
    )
    assert authorizations and all(not value for value in authorizations)
    assert all(not context.run(detail._ending.get) for context in contexts)
    assert not converted_records(exported)
    assert "SYNTHETIC-PRIVATE-FAILURE" not in caplog.text
    assert_capacity_recovered()


def test_simultaneous_threads_cannot_share_release_authorization(monkeypatch, exported):
    _, runtime, _, _ = stack(monkeypatch)
    original_processor = telemetry.PrivacySpanProcessor._on_ending
    barrier = threading.Barrier(2)
    authorizations = []

    def process(self, span):
        if span.name == "card_generation":
            authorization = detail._ending.get()
            assert list(authorization) == [id(span)]
            authorizations.append(authorization)
            barrier.wait(timeout=10)
            assert detail._ending.get() is authorization
            assert list(authorization) == [id(span)]
        return original_processor(self, span)

    def generate_in_thread():
        response = asyncio.run(runtime.generate(GenerateCardAgentRequest(query=PROMPT)))
        assert detail._ending.get() is None
        return response.status

    monkeypatch.setattr(telemetry.PrivacySpanProcessor, "_on_ending", process)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(generate_in_thread) for _ in range(2)]
        assert [future.result(timeout=20) for future in futures] == ["completed", "completed"]
    assert len(authorizations) == 2 and authorizations[0] is not authorizations[1]
    assert all(not value for value in authorizations)
    assert len(converted_records(exported)) == 2 * HOSTED_SPAN_COUNT
    assert_capacity_recovered()


def test_child_task_cannot_reuse_inherited_ending_authorization(monkeypatch, exported):
    _, runtime, _, _ = stack(monkeypatch)
    original_process = telemetry.PrivacySpanProcessor._on_ending
    children, holders = [], []

    async def child(span):
        inherited = detail._ending.get()
        assert inherited is not None and not inherited
        clone = detail._tracer().start_span(span.name)
        clone._context, clone._parent = span.context, span.parent
        clone._start_time = span.start_time
        clone.set_attributes(dict(span.attributes))
        clone.end(end_time=span.end_time)
        assert "fcg.detail.record" not in clone.attributes

    def process(self, span):
        authorization = detail._ending.get()
        if authorization and id(span) in authorization:
            holders.append(authorization)
            children.append(asyncio.create_task(child(span)))
        return original_process(self, span)

    async def scenario():
        result = await runtime.generate(GenerateCardAgentRequest(query=PROMPT))
        assert result.status == "completed"
        await asyncio.gather(*children)
        assert detail._ending.get() is None

    monkeypatch.setattr(telemetry.PrivacySpanProcessor, "_on_ending", process)
    asyncio.run(scenario())
    assert len(children) == len(converted_records(exported)) == HOSTED_SPAN_COUNT
    assert all(not holder for holder in holders)
    assert_capacity_recovered()


@pytest.mark.parametrize(
    "failure", ["closure", "serialization", "revalidation", "serialized_status", "evidence"]
)
def test_hosted_acceptance_includes_closure_and_business_serialization(
    monkeypatch, exported, failure
):
    _, runtime, _, _ = stack(monkeypatch)
    if failure == "closure":

        @asynccontextmanager
        async def factory(_):
            yield specialists.FoundrySpecialists(None)
            raise RuntimeError("SYNTHETIC-PRIVATE-FAILURE")

        runtime.specialist_factory = factory
    elif failure == "serialization":
        original = GenerateCardAgentResponse.model_dump_json

        def fail(self, *args, **kwargs):
            if self.metadata.get("safetyEvidence"):
                raise ValueError("SYNTHETIC-PRIVATE-FAILURE")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(GenerateCardAgentResponse, "model_dump_json", fail)
    elif failure == "revalidation":
        original = GenerateCardAgentResponse.model_validate_json

        def fail(*args, **kwargs):
            result = original(*args, **kwargs)
            if result.metadata.get("safetyEvidence"):
                raise ValueError("SYNTHETIC-PRIVATE-FAILURE")
            return result

        monkeypatch.setattr(GenerateCardAgentResponse, "model_validate_json", fail)
    elif failure == "serialized_status":
        original = GenerateCardAgentResponse.model_dump_json

        def serialize(self, *args, **kwargs):
            value = (
                self.model_copy(update={"status": "held"})
                if self.metadata.get("safetyEvidence")
                else self
            )
            return original(value, *args, **kwargs)

        monkeypatch.setattr(GenerateCardAgentResponse, "model_dump_json", serialize)
    else:
        original = GenerateCardAgentResponse.model_dump_json

        def serialize(self, *args, **kwargs):
            value = json.loads(original(self, *args, **kwargs))
            if value["metadata"].get("safetyEvidence"):
                value["metadata"]["safetyEvidence"][0]["reason"] = "invalid_evidence"
            return json.dumps(value)

        monkeypatch.setattr(GenerateCardAgentResponse, "model_dump_json", serialize)
    if failure == "closure":
        with pytest.raises(RuntimeFailure):
            asyncio.run(runtime.generate(GenerateCardAgentRequest(query=PROMPT)))
    else:
        assert (
            asyncio.run(runtime.generate(GenerateCardAgentRequest(query=PROMPT))).status
            == "completed"
        )
    assert not content(exported)
    assert len(new_spans(exported)) == HOSTED_SPAN_COUNT
    assert_capacity_recovered()


def test_unsafe_intermediate_cannot_be_authorized_by_safe_final_card(monkeypatch, exported):
    _, runtime, _, _ = stack(monkeypatch, outputs=[CARD | {"name": "graphic gore"}])

    async def permissive(text, *, stage):
        return ModerationDecision(
            stage=stage, allowed=True, reasonCode="allowed", details="allowed"
        )

    runtime.moderation.moderate_text = permissive
    result = asyncio.run(runtime.generate(GenerateCardAgentRequest(query=PROMPT)))
    assert result.status == "completed"
    assert result.card.name == "graphic gore"
    assert not content(exported)
    assert len(new_spans(exported)) == HOSTED_SPAN_COUNT
    assert_capacity_recovered()


@pytest.mark.parametrize("stage", ["final_text", "final_art_prompt"])
def test_last_hosted_policy_gate_suppresses_all_original_content(monkeypatch, exported, stage):
    _, runtime, _, _ = stack(monkeypatch)
    original = runtime._gate

    async def gate(text, moderation_stage, evidence_stage, *args):
        return await original(
            "graphic gore" if evidence_stage == stage else text,
            moderation_stage,
            evidence_stage,
            *args,
        )

    runtime._gate = gate
    response = asyncio.run(runtime.generate(GenerateCardAgentRequest(query=PROMPT)))
    assert response.status == "refused"
    assert len(new_spans(exported)) == HOSTED_SPAN_COUNT
    assert not content(exported)
    assert_capacity_recovered()


def test_hosted_release_and_close_are_exactly_once(monkeypatch, exported):
    _, runtime, _, _ = stack(monkeypatch)
    original_release, original_end = detail.Capture._release, Span.end
    finalized = []

    def release(self):
        original_release(self)
        original_release(self)
        self.close()
        self.close()

    def end(self, end_time=None):
        if self.instrumentation_scope.name == detail.SCOPE:
            finalized.append(self.get_span_context().span_id)
        return original_end(self, end_time=end_time)

    monkeypatch.setattr(detail.Capture, "_release", release)
    monkeypatch.setattr(Span, "end", end)
    assert (
        asyncio.run(runtime.generate(GenerateCardAgentRequest(query=PROMPT))).status == "completed"
    )
    assert len(content(exported)) == len(set(finalized)) == len(finalized) == HOSTED_SPAN_COUNT
    assert_capacity_recovered()


@pytest.mark.parametrize("boundary", ["attributes", "end", "processor"])
@pytest.mark.parametrize("accepted", [False, True])
def test_cancellation_during_finalization_drains_all_owned_spans(
    monkeypatch, exported, boundary, accepted
):
    _, runtime, _, _ = stack(monkeypatch)
    original_end, original_set = Span.end, Span.set_attribute
    original_process, original_close = (
        telemetry.PrivacySpanProcessor._on_ending,
        detail.Capture.close,
    )
    ended, captures, holders = [], [], []

    def interrupt(span):
        if span.name == "card_generation":
            holders.append(detail._ending.get())
            raise asyncio.CancelledError

    def end(self, end_time=None):
        if self.instrumentation_scope.name == detail.SCOPE:
            ended.append(self.get_span_context().span_id)
        if boundary == "end":
            interrupt(self)
        return original_end(self, end_time=end_time)

    def attribute(self, key, value):
        result = original_set(self, key, value)
        if key == "fcg.detail.record" and boundary == "attributes":
            interrupt(self)
        return result

    def process(self, span):
        if boundary == "processor":
            interrupt(span)
        return original_process(self, span)

    def close(self):
        captures.append(self)
        return original_close(self)

    if not accepted:
        original_gate = runtime._gate

        async def gate(text, moderation_stage, evidence_stage, *args):
            return await original_gate(
                "graphic gore" if evidence_stage == "final_text" else text,
                moderation_stage,
                evidence_stage,
                *args,
            )

        runtime._gate = gate
    monkeypatch.setattr(Span, "end", end)
    monkeypatch.setattr(Span, "set_attribute", attribute)
    monkeypatch.setattr(telemetry.PrivacySpanProcessor, "_on_ending", process)
    monkeypatch.setattr(detail.Capture, "close", close)
    if boundary == "attributes" and not accepted:
        response = asyncio.run(runtime.generate(GenerateCardAgentRequest(query=PROMPT)))
        assert response.status == "refused"
    else:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(runtime.generate(GenerateCardAgentRequest(query=PROMPT)))
    assert len(ended) == len(set(ended)) == HOSTED_SPAN_COUNT
    assert all(
        c.closed and not c.pending and not c.records and not c._approved_records for c in captures
    )
    assert all(not holder for holder in holders)
    assert not converted_records(exported)
    assert_capacity_recovered()


def test_business_closure_serialization_and_batch_preflight_precede_first_attachment(
    monkeypatch, exported
):
    _, runtime, _, _ = stack(monkeypatch)
    events = []
    original_serialize = GenerateCardAgentResponse.model_dump_json
    original_validate = GenerateCardAgentResponse.model_validate_json
    original_pending, original_set = detail._validate_pending, Span.set_attribute

    @asynccontextmanager
    async def factory(_):
        yield specialists.FoundrySpecialists(None)
        assert not content(exported)
        events.append("closed")

    def serialize(self, *args, **kwargs):
        assert not content(exported)
        events.append("serialized")
        return original_serialize(self, *args, **kwargs)

    def validate(*args, **kwargs):
        assert not content(exported)
        result = original_validate(*args, **kwargs)
        events.append("revalidated")
        return result

    def pending(owned, span, *, ending=False):
        result = original_pending(owned, span, ending=ending)
        if not ending:
            assert not content(exported)
            events.append(owned.stage)
        return result

    def attribute(self, key, value):
        if key == "fcg.detail.record":
            assert events[0] == "closed"
            first_pending = events.index("generation")
            assert events[1:first_pending] == ["serialized", "revalidated"] * 2
            assert events[first_pending : first_pending + 1] == ["generation"]
            events.append("attached")
        return original_set(self, key, value)

    runtime.specialist_factory = factory
    monkeypatch.setattr(GenerateCardAgentResponse, "model_dump_json", serialize)
    monkeypatch.setattr(GenerateCardAgentResponse, "model_validate_json", validate)
    monkeypatch.setattr(detail, "_validate_pending", pending)
    monkeypatch.setattr(Span, "set_attribute", attribute)
    assert (
        asyncio.run(runtime.generate(GenerateCardAgentRequest(query=PROMPT))).status == "completed"
    )
    assert events[events.index("attached") :] == ["attached"] * 1
    assert len(converted_records(exported)) == HOSTED_SPAN_COUNT
    assert_capacity_recovered()


@pytest.mark.parametrize(
    "boundary",
    ["generation", "candidate", "closure", "final_text", "final_art_prompt"],
)
@pytest.mark.parametrize("failure", ["cancel", "timeout"])
def test_every_hosted_await_boundary_finalizes_and_restores(
    monkeypatch, exported, boundary, failure
):
    _, runtime, _, _ = stack(monkeypatch)
    states, ended = [], []
    original_run, original_candidate, original_gate = (
        StageBoundary.invoke,
        detail.candidate,
        runtime._gate,
    )
    original_end = Span.end

    def fail():
        raise asyncio.CancelledError if failure == "cancel" else TimeoutError

    async def run(self, context, call_next):
        states.append(detail.current())
        if self.stage == boundary:
            fail()
        return await original_run(self, context, call_next)

    async def candidate(*args, **kwargs):
        if boundary == "candidate" and args[0] == "generation":
            fail()
        return await original_candidate(*args, **kwargs)

    async def gate(*args):
        if args[2] == boundary:
            fail()
        return await original_gate(*args)

    @asynccontextmanager
    async def factory(_):
        yield specialists.FoundrySpecialists(None)
        if boundary == "closure":
            fail()

    def end(self, end_time=None):
        if self.instrumentation_scope.name == detail.SCOPE:
            ended.append(self.get_span_context().span_id)
        return original_end(self, end_time=end_time)

    monkeypatch.setattr(StageBoundary, "invoke", run)
    monkeypatch.setattr(detail, "candidate", candidate)
    monkeypatch.setattr(Span, "end", end)
    runtime._gate, runtime.specialist_factory = gate, factory
    parent = trace.get_current_span()
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeFailure):
        asyncio.run(runtime.generate(GenerateCardAgentRequest(query=PROMPT)))
    assert trace.get_current_span() is parent
    assert len(ended) == len(set(ended)) == len(new_spans(exported))
    assert all(c.closed and not c.pending and not c.records and c.execution is None for c in states)
    assert not content(exported)
    assert_capacity_recovered()


@pytest.mark.parametrize("failure", ["attributes", "end", "processor"])
def test_instrumentation_failure_cannot_strand_other_originals(
    monkeypatch, exported, caplog, failure
):
    _, runtime, _, _ = stack(monkeypatch)
    original_end, original_set, original_processor = (
        Span.end,
        Span.set_attributes,
        telemetry.PrivacySpanProcessor._on_ending,
    )
    ended, captures = [], []
    original_candidate = detail.candidate

    async def candidate(*args, **kwargs):
        captures.append(detail.current())
        return await original_candidate(*args, **kwargs)

    def end(self, end_time=None):
        if self.instrumentation_scope.name == detail.SCOPE:
            ended.append(self.name)
            if self.name == "card_generation" and failure == "end":
                raise RuntimeError("SYNTHETIC-PRIVATE-FAILURE")
        return original_end(self, end_time=end_time)

    def attributes(self, values):
        if self.name == "card_generation" and failure == "attributes":
            raise RuntimeError("SYNTHETIC-PRIVATE-FAILURE")
        return original_set(self, values)

    def process(self, span):
        if span.name == "card_generation" and failure == "processor":
            raise RuntimeError("SYNTHETIC-PRIVATE-FAILURE")
        return original_processor(self, span)

    monkeypatch.setattr(detail, "candidate", candidate)
    monkeypatch.setattr(Span, "end", end)
    monkeypatch.setattr(Span, "set_attributes", attributes)
    monkeypatch.setattr(telemetry.PrivacySpanProcessor, "_on_ending", process)
    result = asyncio.run(runtime.generate(GenerateCardAgentRequest(query=PROMPT)))
    assert result.status == "completed"
    assert ended == ["card_generation"]
    assert not content(exported)
    assert "SYNTHETIC-PRIVATE-FAILURE" not in caplog.text
    assert all(c.closed and not c.pending and not c.records for c in captures)
    assert_capacity_recovered()


def test_hosted_send_failure_does_not_retract_originals(monkeypatch, exported):
    _, runtime, _, _ = stack(monkeypatch)
    host = create_host(runtime.settings, orchestrator=runtime)
    app = httpx.ASGITransport(app=host)
    original_app = app.app

    async def failing_send_app(scope, receive, send):
        async def fail(message):
            if message["type"] == "http.response.body":
                assert len(content(exported)) == HOSTED_SPAN_COUNT
                raise RuntimeError("synthetic delivery failure")
            await send(message)

        await original_app(scope, receive, fail)

    app.app = failing_send_app

    async def request():
        async with httpx.AsyncClient(transport=app, base_url="http://offline") as client:
            await client.post("/responses", json=wire())

    with pytest.raises(RuntimeError, match="synthetic delivery failure"):
        asyncio.run(request())
    assert len(content(exported)) == HOSTED_SPAN_COUNT
    assert_capacity_recovered()


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("consumer", ["unchanged_v1_parser", "new_web"])
def test_v1_web_consumer_accepts_both_producers_without_duplicate_specialists(
    monkeypatch, exported, legacy, consumer
):
    services, _, transport, _ = stack(monkeypatch, legacy=legacy)
    response = _generate(_client(monkeypatch, services))
    assert response.json()["status"] == "completed"
    assert len(content(exported)) == 2 + HOSTED_SPAN_COUNT
    hosted = [
        s
        for s in new_spans(exported)
        if "fcg.detail.record" in s.attributes
        and json.loads(s.attributes["fcg.detail.record"])["source_runtime"] == "hosted"
    ]
    assert len(hosted) == HOSTED_SPAN_COUNT
    assert all((s.name == "fcg.agent.detail") == legacy for s in hosted)
    assert ("agentDetail" in json.dumps(transport.responses)) == legacy
    assert "agentDetail" not in response.text
    if consumer == "unchanged_v1_parser":
        # This optional-carrier parser is unchanged from fff9b92 (old WEB).
        invocation = next(s for s in new_spans(exported) if s.name == "fcg.agent.invoke")
        parsed = _parse_success_envelope(
            transport.responses[0],
            request_id=None,
            expected_version="candidate-1",
            capture_details=True,
            detail_source=detail.Source(
                trace_id=f"{invocation.context.trace_id:032x}",
                span_id=f"{invocation.context.span_id:016x}",
            ),
        )
        assert parsed.success and not parsed.agent_detail_rejected
        assert (parsed.agent_detail is not None) == legacy


def test_real_parser_excludes_reasoning_sentinels_from_public_and_app_capture(
    monkeypatch, exported, caplog, model_transport
):
    calls, responses, *_ = model_transport
    summary_sentinel = "SUMMARY_SENTINEL_170_REASONING"
    encrypted_sentinel = "ENCRYPTED_SENTINEL_170_REASONING"
    responses[0] = model_response(CARD)
    responses[0]["output"].insert(
        0,
        reasoning_response_item(
            summary_text=summary_sentinel,
            encrypted_content=encrypted_sentinel,
        ),
    )
    hosted_settings = settings(agent_trace_enabled=True)
    runtime = CardOrchestrator(hosted_settings)
    transport = LocalHostedTransport(
        create_host(hosted_settings, orchestrator=runtime),
        legacy=True,
    )
    services = _services(monkeypatch, None)
    services.settings = replace(
        services.settings,
        agent_trace_enabled=True,
        foundry_agent_timeout_seconds=5,
        retry=replace(services.settings.retry, overall_timeout_seconds=10),
    )
    services.agent_client = FoundryAgentClient(
        services.settings,
        credential=SimpleNamespace(get_token=lambda *_: "offline-token"),
        transport=transport,
    )

    caplog.set_level("DEBUG")
    response = _generate(_client(monkeypatch, services), key="reasoning-sentinel-170")
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert len(calls) == 1
    assert "agentDetail" in json.dumps(transport.responses)

    invocation = next(s for s in new_spans(exported) if s.name == "fcg.agent.invoke")
    parsed = _parse_success_envelope(
        transport.responses[0],
        request_id=None,
        expected_version="candidate-1",
        capture_details=True,
        detail_source=detail.Source(
            trace_id=f"{invocation.context.trace_id:032x}",
            span_id=f"{invocation.context.span_id:016x}",
        ),
    )
    assert parsed.success and parsed.agent_detail is not None
    assert not parsed.agent_detail_rejected
    assert any("fcg.detail.record" in span.attributes for span in new_spans(exported))

    records_dump = json.dumps(content(exported))
    spans_dump = json.dumps(
        [
            {
                "name": span.name,
                "attributes": dict(span.attributes),
                "events": [
                    {"name": event.name, "attributes": dict(event.attributes)}
                    for event in span.events
                ],
            }
            for span in new_spans(exported)
        ],
        default=str,
    )
    detail_dump = parsed.agent_detail.model_dump_json()
    transport_dump = json.dumps(transport.responses)

    for sentinel in (summary_sentinel, encrypted_sentinel):
        assert sentinel not in response.text
        assert sentinel not in records_dump
        assert sentinel not in detail_dump
        assert sentinel not in spans_dump
        assert sentinel not in transport_dump
        assert sentinel not in caplog.text


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_entry",
        "duplicate_required",
        "unexpected_entry",
        "reordered_suffix",
        "malformed_entry",
        "nonallowed_required",
        "wrong_required_policy",
        "wrong_required_reason",
        "wrong_required_decision",
        "wrong_guardrails_decision",
        "wrong_guardrails_reason",
        "wrong_post_image_decision",
        "wrong_post_image_reason",
    ],
)
def test_release_validator_fail_closed_safety_evidence_matrix(monkeypatch, exported, mutation):
    services, _, transport, _ = stack(monkeypatch, legacy=True)
    response = _generate(_client(monkeypatch, services))
    assert response.json()["status"] == "completed"
    invocation = next(s for s in new_spans(exported) if s.name == "fcg.agent.invoke")
    payload = json.loads(transport.responses[0]["output"][0]["content"][0]["text"])
    evidence = payload["metadata"]["safetyEvidence"]
    assert len(evidence) == 6

    match mutation:
        case "missing_entry":
            evidence.pop()
        case "duplicate_required":
            evidence.insert(2, dict(evidence[1]))
        case "unexpected_entry":
            evidence.append(
                {
                    "stage": "unexpected_stage",
                    "policy": "original-fantasy-v1",
                    "decision": "allowed",
                    "reason": "allowed",
                }
            )
        case "reordered_suffix":
            evidence[4], evidence[5] = evidence[5], evidence[4]
        case "malformed_entry":
            evidence[0] = {"stage": "pre_prompt"}
        case "nonallowed_required":
            evidence[0] = evidence[0] | {"decision": "blocked", "reason": "blocked"}
        case "wrong_required_policy":
            evidence[1] = evidence[1] | {"policy": "not-original-fantasy-v1"}
        case "wrong_required_reason":
            evidence[2] = evidence[2] | {"reason": "not_observed"}
        case "wrong_required_decision":
            evidence[3] = evidence[3] | {"decision": "unavailable"}
        case "wrong_guardrails_decision":
            evidence[4] = evidence[4] | {"decision": "not_applicable"}
        case "wrong_guardrails_reason":
            evidence[4] = evidence[4] | {"reason": "text_only"}
        case "wrong_post_image_decision":
            evidence[5] = evidence[5] | {"decision": "unavailable"}
        case "wrong_post_image_reason":
            evidence[5] = evidence[5] | {"reason": "not_observed"}
        case _:
            raise AssertionError(f"Unhandled mutation {mutation}")
    transport.responses[0]["output"][0]["content"][0]["text"] = json.dumps(payload)

    parsed = _parse_success_envelope(
        transport.responses[0],
        request_id=None,
        expected_version="candidate-1",
        capture_details=True,
        detail_source=detail.Source(
            trace_id=f"{invocation.context.trace_id:032x}",
            span_id=f"{invocation.context.span_id:016x}",
        ),
    )
    assert parsed.success
    assert parsed.agent_detail is None
    assert parsed.agent_detail_rejected


@pytest.mark.parametrize("web", [False, True])
@pytest.mark.parametrize("hosted", [False, True])
def test_legacy_producer_mixed_flags_keep_web_as_release_authority(
    monkeypatch, exported, web, hosted
):
    services, _, transport, _ = stack(monkeypatch, legacy=True, web=web, hosted=hosted)
    response = _generate(_client(monkeypatch, services))
    assert response.json()["status"] == "completed"
    records = converted_records(exported)
    assert len(records) == (2 + (HOSTED_SPAN_COUNT if hosted else 0) if web else 0)
    assert ("agentDetail" in json.dumps(transport.responses)) == hosted
    assert "agentDetail" not in response.text
    assert all(
        s.name == "fcg.agent.detail"
        for s in new_spans(exported)
        if "fcg.detail.record" in s.attributes
    )
    assert_capacity_recovered()


@pytest.mark.parametrize("boundary", ["final_text", "candidate"])
@pytest.mark.parametrize("phase", ["whole_request", "late_boundary"])
def test_hosted_retained_capacity_with_real_deadline(
    monkeypatch, exported, record_property, boundary, phase, model_transport
):
    runtime = CardOrchestrator(settings(timeout_seconds=2 if phase == "late_boundary" else 0.1))
    captures = []
    targets = []
    deadlines = []
    original_gate = runtime._gate
    original_factory = runtime.specialist_factory

    @asynccontextmanager
    async def factory(config):
        deadlines.append(detail.current_deadline())
        async with original_factory(config) as owned:
            yield owned

    runtime.specialist_factory = factory

    async def wait():
        captures.append(detail.current())
        assert len(detail.current().pending) == HOSTED_SPAN_COUNT
        assert len(detail.current().records) == HOSTED_SPAN_COUNT
        assert detail.current().deadline == deadlines[0] == detail.current_deadline()
        assert all(p.deadline == deadlines[0] for p in detail.current().pending)
        remaining = deadlines[0] - asyncio.get_running_loop().time()
        assert remaining > 0.1
        await asyncio.sleep(remaining - 0.1)
        targets.append(time.monotonic())
        await asyncio.sleep(1)

    async def gate(*args):
        if args[2] == boundary:
            await wait()
        return await original_gate(*args)

    runtime._gate = gate
    if boundary == "candidate":
        original_candidate = detail.candidate

        async def candidate(*args, **kwargs):
            await original_candidate(*args, **kwargs)
            if args[0] == "generation":
                await wait()

        monkeypatch.setattr(detail, "candidate", candidate)
    start = time.monotonic()
    with pytest.raises(RuntimeFailure):
        asyncio.run(runtime.generate(GenerateCardAgentRequest(query=PROMPT)))
    elapsed = time.monotonic() - start
    record_property("elapsed_seconds", elapsed)
    if phase == "whole_request":
        assert elapsed < 0.5
        assert not captures
    else:
        assert elapsed >= 2
        tail = time.monotonic() - targets[0]
        record_property("target_to_cleanup_seconds", tail)
        assert tail < 0.5
        assert len(model_transport[0]) == len(new_spans(exported)) == HOSTED_SPAN_COUNT
        assert captures and all(c.closed and not c.pending and not c.records for c in captures)
    assert not content(exported)
    assert_capacity_recovered()


def test_nested_hosted_span_admission_never_retains_more_than_one(exported):
    with capture("hosted") as state:
        with detail.execution("generation"):
            with detail.execution("generation") as rejected:
                assert rejected is None
        assert len(state.pending) == HOSTED_SPAN_COUNT
        assert not state.eligible
    assert len(new_spans(exported)) == HOSTED_SPAN_COUNT
    assert_capacity_recovered()


@pytest.mark.parametrize("maximum_fields", [False, True])
def test_seventeenth_real_hosted_request_cannot_allocate_retained_spans(
    monkeypatch, exported, record_property, maximum_fields, model_transport
):
    glyph = "\U0001f9d9"
    outputs = (
        [
            CARD
            | {
                "name": glyph * 80,
                "rulesText": glyph * 400,
                "flavorText": glyph * 280,
                "artBrief": glyph * 300,
            }
        ]
        if maximum_fields
        else None
    )
    from app.specialist_contract import SCHEMAS

    runtime = CardOrchestrator(settings())
    # Hang watchdog, not a performance SLO; the runtime's own deadlines remain active.
    watchdog_seconds = 2 * runtime.settings.timeout_seconds
    stage_outputs = dict(zip(SCHEMAS, outputs or [CARD], strict=True))

    def respond(call):
        stage = next(
            stage
            for stage, schema in SCHEMAS.items()
            if schema.__name__ == call["text"]["format"]["name"]
        )
        return model_response(stage_outputs[stage])

    model_transport[1][:] = [respond]
    observed = observe_hosted_lifecycle(monkeypatch)
    original_gate = runtime._gate
    original_acquire = detail._capacity.acquire
    captures = []
    acquisitions = []

    def acquire(*args, **kwargs):
        acquired = original_acquire(*args, **kwargs)
        acquisitions.append(acquired)
        return acquired

    monkeypatch.setattr(detail._capacity, "acquire", acquire)

    async def scenario():
        gate = asyncio.Event()
        admitted_ready = asyncio.Event()
        all_ready = asyncio.Event()
        other_admitted_ready = asyncio.Event()

        async def wait(*args):
            if args[2] == "final_text":
                # Graph completion order need not match capture allocation order.
                if detail.current() is observed["captures"][0]:
                    await other_admitted_ready.wait()
                captures.append(detail.current())
                if len(captures) == 15:
                    other_admitted_ready.set()
                if len(captures) == 16:
                    admitted_ready.set()
                if len(captures) == 17:
                    all_ready.set()
                await gate.wait()
            return await original_gate(*args)

        runtime._gate = wait
        tasks = [
            asyncio.create_task(
                runtime.generate(
                    GenerateCardAgentRequest(query=glyph * 400 if maximum_fields else PROMPT)
                )
            )
            for _ in range(16)
        ]
        try:
            await asyncio.wait_for(admitted_ready.wait(), watchdog_seconds)
            tasks.append(
                asyncio.create_task(
                    runtime.generate(
                        GenerateCardAgentRequest(query=glyph * 400 if maximum_fields else PROMPT)
                    )
                )
            )
            await asyncio.wait_for(all_ready.wait(), watchdog_seconds)
            active = [c for c in captures if c is not None]
            assert len(active) == 16
            assert captures[-1] is None
            assert len(observed["captures"]) == 16
            assert {id(c) for c in observed["captures"]} == {id(c) for c in active}
            assert len({id(c) for c in active}) == 16
            assert [id(c) for c in observed["captures"]] != [id(c) for c in active]
            assert acquisitions == [True] * 16 + [False]
            assert all(len(c.pending) == len(c.records) == HOSTED_SPAN_COUNT for c in active)
            assert all(c.spans == HOSTED_SPAN_COUNT and not c.closed for c in active)
            assert (
                len({p.source.span_id for c in active for p in c.pending}) == 16 * HOSTED_SPAN_COUNT
            )
            assert len(observed["created"]) == 17
            assert len(observed["ended"]) == 1
            rejected = observed["ended"][0][0]
            assert rejected is observed["created"][-1]
            assert rejected.name == "card_generation"
            assert not rejected.is_recording()
            assert all(p.span is not rejected for c in active for p in c.pending)
            assert [s.context for s in new_spans(exported)] == [rejected.get_span_context()]
            assert "fcg.detail.record" not in rejected.attributes
            assert not observed["attached"]
            assert not content(exported)
            record_property(
                "capacity",
                {
                    "active_buffers": len(active),
                    "retained_originals": sum(len(c.pending) for c in active),
                    "successful_admissions": sum(acquisitions),
                    "rejected_finalized_content_free": len(observed["ended"]),
                },
            )
            if maximum_fields:
                assert all(
                    "omitted_budget" in record.output.flags
                    for c in active
                    for record in c.records
                    if record.stage == "generation"
                )
            tasks[0].cancel()
        finally:
            gate.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert all(r.status == "completed" for r in results[1:])

    asyncio.run(scenario())
    assert len(new_spans(exported)) == 17 * HOSTED_SPAN_COUNT
    assert len(model_transport[0]) == 17 * HOSTED_SPAN_COUNT
    assert all(client.is_closed() for client in model_transport[2])
    assert all(credential.closed for credential in model_transport[3])
    assert len(content(exported)) == len(observed["attached"]) == 15 * HOSTED_SPAN_COUNT
    assert acquisitions == [True] * 16 + [False]
    assert all(c is None or (c.closed and not c.pending and not c.records) for c in captures)
    assert_owned_lifecycle_drained(observed)


@pytest.mark.parametrize("limit", ["record", "runtime"])
@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_actual_hosted_byte_ceilings_include_utf8_escaping_and_envelope(
    exported, record_property, limit, delta
):
    def sized(size):
        # Deliberately invalid instruction projections isolate the admission-size guard.
        item = record("hosted", text=" " * detail.FIELD_BYTES).model_copy(
            update={"output": detail.View(text="\u00e9" * (detail.FIELD_BYTES // 2))}
        )
        extra = size - len(detail.encoded(item.model_dump(mode="json")))
        assert 0 <= extra <= detail.FIELD_BYTES
        item = item.model_copy(
            update={"instruction": detail.View(text='"' * extra + " " * (2048 - extra))}
        )
        assert len(detail.encoded(item.model_dump(mode="json"))) == size
        return item

    assert (detail.FIELD_BYTES, detail.RECORD_BYTES, detail.RUNTIME_BYTES) == (2048, 8192, 24576)
    with capture("hosted") as state:
        if limit == "record":
            target = detail.RECORD_BYTES + delta
            state.append(sized(target))
            assert len(state.records) == (1 if delta <= 0 else 0)
        else:
            prefix = [sized(detail.RECORD_BYTES), sized(detail.RECORD_BYTES)]
            wrapper = len(
                detail.encoded(
                    {"version": 1, "records": [r.model_dump(mode="json") for r in prefix]}
                )
            ) - sum(len(detail.encoded(r.model_dump(mode="json"))) for r in prefix)
            target = detail.RUNTIME_BYTES + delta
            last = sized(target - wrapper - 1 - 2 * detail.RECORD_BYTES)
            batch = prefix + [last]
            assert (
                len(
                    detail.encoded(
                        {"version": 1, "records": [r.model_dump(mode="json") for r in batch]}
                    )
                )
                == target
            )
            for item in batch:
                state.append(item)
            assert len(state.records) == (3 if delta <= 0 else 2)
        record_property("serialized_bytes_at_boundary", target)
    assert not content(exported)
    assert_capacity_recovered()


@pytest.mark.parametrize("failure", ["exception", "failure_result", "shutdown", "queue_loss"])
def test_batch_export_loss_is_best_effort_and_releases_all_handles(
    monkeypatch, exported, failure, caplog
):
    del exported
    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=ParentBased(ALWAYS_ON))
    provider.add_span_processor(telemetry.PrivacySpanProcessor())
    entered, unblock = threading.Event(), threading.Event()
    calls = []
    original_export = exporter.export

    def export(spans):
        calls.append(len(spans))
        if failure == "exception":
            raise RuntimeError("synthetic exporter unavailable")
        if failure == "failure_result":
            return SpanExportResult.FAILURE
        if failure == "queue_loss" and len(calls) == 1:
            entered.set()
            assert unblock.wait(10)
        return original_export(spans)

    monkeypatch.setattr(exporter, "export", export)
    processor = BatchSpanProcessor(
        exporter, max_queue_size=1, max_export_batch_size=1, schedule_delay_millis=60000
    )
    provider.add_span_processor(processor)
    monkeypatch.setattr(telemetry, "_tracer", provider.get_tracer("baseline"))
    monkeypatch.setattr(detail, "_tracer", lambda: provider.get_tracer(detail.SCOPE))
    _, runtime, _, _ = stack(monkeypatch)
    ended, captures = [], []
    original_end, original_release = Span.end, detail.Capture._release

    def end(self, end_time=None):
        if self.instrumentation_scope.name == detail.SCOPE:
            ended.append(self.get_span_context().span_id)
        return original_end(self, end_time=end_time)

    def release(self):
        captures.append(self)
        return original_release(self)

    monkeypatch.setattr(Span, "end", end)
    monkeypatch.setattr(detail.Capture, "_release", release)
    try:
        if failure == "shutdown":
            processor.shutdown()
        elif failure == "queue_loss":
            provider.get_tracer("baseline").start_span("fcg.generation").end()
            assert entered.wait(5)
        response = asyncio.run(runtime.generate(GenerateCardAgentRequest(query=PROMPT)))
        assert response.status == "completed"
        assert len(ended) == len(set(ended)) == HOSTED_SPAN_COUNT
        assert all(c.closed and not c.pending and not c.records for c in captures)
    finally:
        unblock.set()
        provider.force_flush()
        provider.shutdown()
    assert len(content(exporter)) <= HOSTED_SPAN_COUNT
    assert_capacity_recovered()


@pytest.mark.parametrize("failure", ["create", "activate", "detach"])
def test_context_lifecycle_failures_are_content_free_and_business_safe(
    monkeypatch, exported, caplog, failure
):
    _, runtime, _, _ = stack(monkeypatch)
    tracer = detail._tracer()
    original = tracer.start_span
    original_use = trace.use_span
    calls = []

    def start(name, *args, **kwargs):
        calls.append(name)
        if name == "card_generation" and failure == "create":
            raise RuntimeError("SYNTHETIC-PRIVATE-CREATION")
        return original(name, *args, **kwargs)

    @contextmanager
    def activate(span, *args, **kwargs):
        ours = (
            getattr(span, "name", None) == "card_generation"
            and getattr(getattr(span, "instrumentation_scope", None), "name", None) == detail.SCOPE
        )
        if ours and failure == "activate":
            raise RuntimeError("SYNTHETIC-PRIVATE-CREATION")
        with original_use(span, *args, **kwargs):
            yield span
        if ours and failure == "detach":
            raise RuntimeError("SYNTHETIC-PRIVATE-CREATION")

    monkeypatch.setattr(tracer, "start_span", start)
    monkeypatch.setattr(detail, "_tracer", lambda: tracer)
    monkeypatch.setattr(trace, "use_span", activate)
    assert (
        asyncio.run(runtime.generate(GenerateCardAgentRequest(query=PROMPT))).status == "completed"
    )
    assert calls == ["card_generation"]
    assert len(new_spans(exported)) == (0 if failure == "create" else HOSTED_SPAN_COUNT)
    assert not content(exported)
    assert "SYNTHETIC-PRIVATE-CREATION" not in caplog.text
    assert_capacity_recovered()
