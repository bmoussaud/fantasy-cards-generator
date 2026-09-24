"""Pinned real graph, real AgentExecutor/Agent, and offline HTTP acceptance."""

from __future__ import annotations

# ruff: noqa: F811
import asyncio
import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from agent_framework import Agent, AgentExecutor, Workflow, WorkflowBuilder
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import Decision, StaticSampler
from opentelemetry.trace import Link, SpanContext, Status, StatusCode, TraceFlags

from app import agent_detail as detail
from app import specialist_contract, telemetry
from app.foundry_agent_client import GenerateCardAgentRequest, GenerateCardAgentResponse
from app.specialist_contract import SCHEMAS
from hosted_agents.card_orchestrator import workflow
from hosted_agents.card_orchestrator.orchestrator import CardOrchestrator, RuntimeFailure
from hosted_agents.card_orchestrator.server import NoResponseStore, create_host
from tests.test_agent_detail_telemetry import content, exported  # noqa: F401
from tests.test_card_orchestrator import CARD, post, settings, wire
from tests.test_card_orchestrator_models import model_response, model_transport  # noqa: F401
from tests.test_original_agent_spans import (
    assert_capacity_recovered,
    assert_owned_lifecycle_drained,
    batched_provider,
    observe_hosted_lifecycle,
)

STAGES = ("generation",)


@pytest.mark.parametrize("stage_index", range(len(STAGES)))
@pytest.mark.parametrize(
    "checker",
    ["unexpected_agent_response", "missing_raw_response", "non_stop_finish", "non_text_content"],
)
def test_completion_checker_survives_graph_close_metadata_host_and_web(
    monkeypatch, model_transport, exported, stage_index, checker
):
    from agent_framework import Content

    from app.foundry_agent_client import _parse_success_envelope

    original = workflow.StageBoundary.invoke
    original_outcome = workflow.TerminalOutcome.from_exception

    def outcome(cls, exc, state):
        result = original_outcome(exc, state)
        if result.response is not None:
            result.response.metadata = {
                "private": "PRIVATE_METADATA",
                "completionDiagnostics": {"stage": "PRIVATE_STAGE", "checker": "PRIVATE_CHECKER"},
            }
        return result

    async def invoke(self, context, call_next):
        async def complete():
            await call_next()
            if self.stage != STAGES[stage_index]:
                return
            if checker == "unexpected_agent_response":
                context.result = None
            elif checker == "missing_raw_response":
                context.result.raw_representation = None
                context.result.finish_reason = "length"
            elif checker == "non_stop_finish":
                context.result.finish_reason = "length"
            else:
                context.result.messages[0].contents.append(
                    Content(
                        "function_call",
                        name="PRIVATE_TOOL",
                        call_id="PRIVATE_ID",
                        arguments="PRIVATE_ARGUMENTS",
                    )
                )

        return await original(self, context, complete)

    monkeypatch.setattr(workflow.StageBoundary, "invoke", invoke)
    monkeypatch.setattr(workflow.TerminalOutcome, "from_exception", classmethod(outcome))
    result = asyncio.run(post(create_host(settings()), wire()))
    assert result.status_code == 200
    envelope = result.json()
    domain = json.loads(envelope["output"][0]["content"][0]["text"])
    assert domain["status"] == "held"
    assert domain["card"] is domain["artPrompt"] is None
    diagnostic = domain["metadata"]["completionDiagnostics"]
    assert diagnostic["stage"] == STAGES[stage_index] and diagnostic["checker"] == checker
    parsed = _parse_success_envelope(envelope, request_id=None, expected_version=None)
    assert parsed.status == "held" and parsed.error_code == "agent_held"
    assert parsed.completion_diagnostics.model_dump(exclude_none=True) == diagnostic
    assert parsed.agent_detail is None
    assert len(model_transport[0]) == 1
    assert_closed(model_transport)
    assert not content(exported)
    assert "agentDetail" not in domain["metadata"]
    assert "PRIVATE" not in result.text + repr(exported.get_finished_spans())


@pytest.mark.parametrize("mode", ["on", "off", "drop", "record_only", "remote_unsampled"])
def test_completion_diagnostics_do_not_enable_capture_or_release_failed_batch(
    monkeypatch, model_transport, mode
):
    from opentelemetry.trace import NonRecordingSpan

    from app.foundry_agent_client import FoundryAgentClient
    from app.generation import CardGenerationService
    from app.problems import ProblemDetails
    from tests.test_agent_detail_telemetry import LocalHostedTransport, generate
    from tests.test_agentic_generation import _services
    from tests.test_telemetry import CapturingInstrument

    sampler = (
        StaticSampler(Decision.DROP)
        if mode == "drop"
        else StaticSampler(Decision.RECORD_ONLY) if mode == "record_only" else None
    )
    with batched_provider(monkeypatch, sampler) as (provider, exporter, _):
        if mode != "on":

            def forbidden(*args, **kwargs):
                pytest.fail("ineligible completion diagnostics activated content capture")

            for name in ("candidate", "projection_view", "_contracts", "parse_envelope"):
                monkeypatch.setattr(detail, name, forbidden)
        raw = model_transport[1][0]
        raw["status"] = "incomplete"
        raw["incomplete_details"] = {"reason": "max_output_tokens"}
        config = settings(agent_trace_enabled=mode != "off")
        transport = LocalHostedTransport(create_host(config))
        services = _services(monkeypatch, None)
        services.settings = replace(
            services.settings,
            agent_trace_enabled=mode != "off",
            foundry_agent_timeout_seconds=5,
            retry=replace(services.settings.retry, overall_timeout_seconds=10),
        )
        services.agent_client = FoundryAgentClient(
            services.settings,
            credential=SimpleNamespace(get_token=lambda *_: "offline-token"),
            transport=transport,
        )
        tokens = CapturingInstrument()
        monkeypatch.setattr(telemetry, "_token_counter", tokens)

        async def scenario():
            parent = NonRecordingSpan(SpanContext(123, 456, True, TraceFlags(0)))
            with (
                trace.use_span(parent)
                if mode == "remote_unsampled"
                else telemetry._tracer.start_as_current_span("parent")
            ):
                with pytest.raises(ProblemDetails) as caught:
                    await generate(CardGenerationService(services))
                assert caught.value.status_code == 502
                assert caught.value.error_code == "invalid_model_output"
            await services.agent_client.aclose()

        asyncio.run(scenario())
        assert provider.force_flush()
        assert len(model_transport[0]) == 1
        assert_closed(model_transport)
        assert not content(exporter)
        assert not tokens.measurements
        body = transport.responses[0]
        domain = json.loads(body["output"][0]["content"][0]["text"])
        assert "agentDetail" not in domain["metadata"]
        assert domain["metadata"]["completionDiagnostics"]["stage"] == "generation"
        events = [
            event
            for span in exporter.get_finished_spans()
            for event in span.events
            if event.name == "agent.invocation"
        ]
        if mode in {"on", "off"}:
            assert len(events) == 1
            assert events[0].attributes["fcg.completion_reason"] == "non_stop_finish"
            assert events[0].attributes["fcg.provider_incomplete_reason"] == "max_output_tokens"
            assert events[0].attributes["fcg.usage.total_tokens"] == 2
        else:
            assert not events
        assert_capacity_recovered()


@pytest.mark.parametrize("stage_index", range(len(STAGES)))
def test_real_provider_max_output_tokens_is_held_with_verified_diagnostics(
    model_transport, exported, stage_index
):
    raw = model_transport[1][stage_index]
    raw["status"] = "incomplete"
    raw["incomplete_details"] = {"reason": "max_output_tokens"}
    raw["usage"]["input_tokens"] = 0
    result = asyncio.run(
        CardOrchestrator(settings()).generate(GenerateCardAgentRequest(query="mountain drake"))
    )
    assert result.status == "held" and result.safetyHints == ["model_incomplete"]
    assert result.metadata["completionDiagnostics"] == {
        "stage": STAGES[stage_index],
        "checker": "non_stop_finish",
        "finishReason": "length",
        "incompleteReason": "max_output_tokens",
        "usage": {"inputTokens": 0, "outputTokens": 1, "totalTokens": 2},
    }
    assert len(model_transport[0]) == 1
    assert_closed(model_transport)
    assert not content(exported)


def user_payload(call):
    users = [message for message in call["input"] if message["role"] == "user"]
    assert len(users) == 1
    assert len(users[0]["content"]) == 1
    return json.loads(users[0]["content"][0]["text"])


def assert_closed(transport):
    _, _, clients, credentials, projects = transport
    assert all(client.is_closed() and client.max_retries == 0 for client in clients)
    assert all(credential.closed for credential in credentials)
    assert len(projects) == len(credentials)


def assert_no_children():
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    ]


def observe_outputs(monkeypatch):
    outputs = []
    original = Workflow.run

    async def run(self, *args, **kwargs):
        assert not kwargs.get("stream", False)
        result = await original(self, *args, **kwargs)
        outputs.extend(event.data for event in result if event.type == "output")
        return result

    monkeypatch.setattr(Workflow, "run", run)
    return outputs


def test_real_graph_exact_inputs_options_and_terminal_only(monkeypatch, model_transport, exported):
    outputs = observe_outputs(monkeypatch)
    calls = model_transport[0]
    runtime = CardOrchestrator(settings())
    original_factory, original_gate = runtime.specialist_factory, runtime._gate
    phases, owners = [], []

    async def gate(*args):
        if args[2].startswith("final"):
            assert phases == ["pre_prompt", "acquired", "closed"]
            assert_closed(model_transport)
        await original_gate(*args)
        if args[2] == "pre_prompt":
            phases.append("pre_prompt")

    @asynccontextmanager
    async def factory(config):
        assert phases == ["pre_prompt"]
        owners.append(asyncio.current_task())
        async with original_factory(config) as owned:
            phases.append("acquired")
            yield owned
        owners.append(asyncio.current_task())
        phases.append("closed")

    runtime.specialist_factory, runtime._gate = factory, gate
    result = asyncio.run(runtime.generate(GenerateCardAgentRequest(query="mountain sentinel")))
    assert phases == ["pre_prompt", "acquired", "closed"]
    assert owners[0] is owners[1]
    assert result.status == "completed"
    assert [user_payload(call) for call in calls] == [
        {"query": "mountain sentinel"},
    ]
    for stage, call in zip(STAGES, calls, strict=True):
        fmt = call["text"]["format"]
        assert fmt["name"] == SCHEMAS[stage].__name__
        assert fmt["schema"] == SCHEMAS[stage].model_json_schema()
        assert fmt["strict"] is True
        instruction = specialist_contract.effective_instructions(stage)
        record = next(record for record in content(exported) if record["stage"] == stage)
        assert record["instruction_digest"] == hashlib.sha256(instruction.encode()).hexdigest()
        assert call["instructions"] == instruction
        assert call["max_output_tokens"] == 1800
        assert call["store"] is False and call.get("stream", False) is False
        assert not call.get("tools")
        assert not {"conversation", "previous_response_id", "history"} & call.keys()
    assert len(outputs) == 1
    terminal = GenerateCardAgentResponse.model_validate_json(outputs[0])
    assert terminal.status == "completed" and terminal.card == result.card
    assert result.card.model_dump() == CARD
    assert len(content(exported)) == 1
    assert_closed(model_transport)


@pytest.mark.parametrize("stage", STAGES)
def test_static_contract_cache_is_lazy_and_schema_copies_are_isolated(monkeypatch, stage):
    contract = specialist_contract
    contract._static_contract.cache_clear()
    schema_type = SCHEMAS[stage]
    expected_schema = schema_type.model_json_schema()
    expected_instruction = (
        f"{contract.INSTRUCTIONS}\n{contract.TASKS[stage]}\nSchema: " + json.dumps(expected_schema)
    )
    calls = []
    original = schema_type.model_json_schema

    def schema():
        calls.append(stage)
        return original()

    monkeypatch.setattr(schema_type, "model_json_schema", schema)
    try:
        assert contract._static_contract.cache_info().currsize == 0
        options = contract.effective_schema(stage)
        options["properties"].clear()
        options["required"].append("untrusted")
        assert contract.effective_schema(stage) == expected_schema
        assert contract.effective_instructions(stage) == expected_instruction
        assert detail._contracts(stage)[0] == expected_instruction
        assert calls == [stage]
        assert contract._static_contract.cache_info().currsize == 1
        with pytest.raises(TypeError):
            contract.SCHEMAS[stage] = schema_type
        with pytest.raises(TypeError):
            contract.TASKS[stage] = "untrusted"
        with pytest.raises(KeyError):
            contract.effective_instructions("untrusted request")
        assert contract._static_contract.cache_info().currsize == 1
    finally:
        contract._static_contract.cache_clear()


def test_static_contract_cache_shares_only_immutable_values_across_threads():
    contract = specialist_contract
    contract._static_contract.cache_clear()
    expected = {stage: SCHEMAS[stage].model_json_schema() for stage in STAGES}

    def use_contract(stage):
        schema = contract.effective_schema(stage)
        assert schema == expected[stage]
        schema["properties"].clear()
        return contract.effective_instructions(stage)

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            actual = list(pool.map(use_contract, STAGES * 8))
        assert actual == [
            f"{contract.INSTRUCTIONS}\n{contract.TASKS[stage]}\nSchema: "
            + json.dumps(expected[stage])
            for stage in STAGES * 8
        ]
        assert contract._static_contract.cache_info().currsize == 1
        assert all(contract.effective_schema(stage) == expected[stage] for stage in STAGES)
    finally:
        contract._static_contract.cache_clear()


@pytest.mark.parametrize(
    "source,target,count",
    [
        ("pre_prompt", "generation", 0),
        ("generation_merge", "final_safety", 1),
    ],
)
def test_required_edges_control_actual_dispatch(
    monkeypatch, model_transport, exported, source, target, count
):
    original = WorkflowBuilder.add_edge
    blocked = []
    outputs = observe_outputs(monkeypatch)

    def add_edge(self, start, end, *args, **kwargs):
        if (start.id, end.id) == (source, target):

            def condition(message):
                blocked.append(message)
                return False

            kwargs["condition"] = condition
        return original(self, start, end, *args, **kwargs)

    monkeypatch.setattr(WorkflowBuilder, "add_edge", add_edge)

    async def scenario():
        with pytest.raises(RuntimeFailure):
            await CardOrchestrator(settings()).generate(GenerateCardAgentRequest(query="drake"))
        assert_no_children()

    asyncio.run(scenario())
    assert len(blocked) == 1
    assert len(model_transport[0]) == count
    assert not content(exported)
    assert not outputs
    assert_closed(model_transport)


@pytest.mark.parametrize("stage_index", range(len(STAGES)))
@pytest.mark.parametrize("failure", ["refusal", "filter", "held", "extra", "tool", "http"])
def test_real_stage_failure_matrix_closed_terminal(
    monkeypatch, model_transport, exported, caplog, stage_index, failure
):
    calls, responses, *_ = model_transport
    outputs = observe_outputs(monkeypatch)
    observed = observe_hosted_lifecycle(monkeypatch)
    raw = [CARD][stage_index]
    if failure == "http":
        responses[stage_index] = httpx.Response(
            500, json={"error": {"message": "PRIVATE_WORKFLOW_FAILURE"}}
        )
    elif failure == "filter":
        responses[stage_index] = httpx.Response(
            400, json={"error": {"code": "content_filter", "message": "PRIVATE_WORKFLOW_FAILURE"}}
        )
    elif failure == "refusal":
        responses[stage_index] = model_response(raw, refusal=True)
    elif failure == "held":
        responses[stage_index] = model_response({"invalid": "PRIVATE_WORKFLOW_FAILURE"})
    elif failure == "extra":
        responses[stage_index] = model_response(raw | {"extra": "PRIVATE_WORKFLOW_FAILURE"})
    else:
        responses[stage_index]["output"].append(
            {
                "type": "function_call",
                "id": "fc_private",
                "call_id": "call_private",
                "name": "PRIVATE_WORKFLOW_FAILURE",
                "arguments": "{}",
                "status": "completed",
            }
        )

    async def scenario():
        with telemetry._tracer.start_as_current_span("caller") as parent:
            if failure == "http":
                with pytest.raises(RuntimeFailure) as caught:
                    await CardOrchestrator(settings()).generate(
                        GenerateCardAgentRequest(query="drake")
                    )
                assert caught.value.stage == STAGES[stage_index]
                assert caught.value.reason == "service_error"
            else:
                result = await CardOrchestrator(settings()).generate(
                    GenerateCardAgentRequest(query="drake")
                )
                expected = "refused" if failure in {"refusal", "filter"} else "held"
                assert result.status == expected
                assert result.card is result.artPrompt is None
                assert len(outputs) == 1
                assert json.loads(outputs[0])["status"] == expected
            assert trace.get_current_span() is parent
            assert detail.current() is None
            assert_no_children()

    asyncio.run(scenario())
    assert len(calls) == 1
    assert len(observed["created"]) == 1
    assert not content(exported)
    assert "PRIVATE_WORKFLOW_FAILURE" not in json.dumps(outputs) + caplog.text
    assert_owned_lifecycle_drained(observed)
    assert_closed(model_transport)


def test_preprompt_rejection_precedes_resource_acquisition(monkeypatch, model_transport, exported):
    outputs = observe_outputs(monkeypatch)
    result = asyncio.run(
        CardOrchestrator(settings()).generate(GenerateCardAgentRequest(query="graphic gore"))
    )
    assert result.status == "refused"
    assert all(not values for values in (model_transport[0], *model_transport[2:]))
    assert len(outputs) == 1 and json.loads(outputs[0])["status"] == "refused"
    assert not content(exported)
    assert_capacity_recovered()


@pytest.mark.parametrize("stage_index", range(len(STAGES)))
@pytest.mark.parametrize("failure", ["cancel", "timeout"])
def test_real_http_await_cancellation_drains_graph(
    monkeypatch, model_transport, exported, stage_index, failure
):
    calls, responses, *_ = model_transport
    observed = observe_hosted_lifecycle(monkeypatch)

    async def scenario():
        entered, exited = asyncio.Event(), asyncio.Event()

        async def block(_):
            entered.set()
            try:
                await asyncio.sleep(10)
            finally:
                exited.set()

        responses[stage_index] = block
        runtime = CardOrchestrator(settings(stage_timeout_seconds=0.15))
        with telemetry._tracer.start_as_current_span("caller") as parent:
            task = asyncio.create_task(runtime.generate(GenerateCardAgentRequest(query="drake")))
            await asyncio.wait_for(entered.wait(), 3)
            if failure == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(RuntimeFailure) as caught:
                    await task
                assert caught.value.reason == "timeout"
                assert caught.value.stage == STAGES[stage_index]
            assert exited.is_set()
            assert trace.get_current_span() is parent
            assert detail.current() is None
            assert_no_children()

    asyncio.run(scenario())
    assert len(calls) == 1
    assert not content(exported)
    assert_owned_lifecycle_drained(observed)
    assert_closed(model_transport)


@pytest.mark.parametrize(
    "boundary,count",
    [
        ("pre_prompt", 0),
        ("setup", 0),
        ("candidate", 1),
        ("closure", 1),
        ("final_text", 1),
        ("final_art_prompt", 1),
    ],
)
@pytest.mark.parametrize("failure", ["cancel", "timeout"])
def test_real_graph_await_boundaries_restore_request_ownership(
    monkeypatch, model_transport, exported, record_property, boundary, count, failure
):
    observed = observe_hosted_lifecycle(monkeypatch)
    runtime = CardOrchestrator(settings(timeout_seconds=2))
    original_factory, original_gate = runtime.specialist_factory, runtime._gate
    original_candidate = detail.candidate
    owners, deadlines = [], []

    async def scenario():
        reached = asyncio.Event()
        baseline_tasks = set(asyncio.all_tasks())

        async def block():
            deadline = detail.current_deadline()
            deadlines.append(deadline)
            state = detail.current()
            if count == 1:
                assert len(state.pending) == len(state.records) == 1
                assert state.deadline == deadline
                assert all(p.deadline == deadline for p in state.pending)
            reached.set()
            await asyncio.sleep(10)

        @asynccontextmanager
        async def factory(config):
            owners.append(asyncio.current_task())
            try:
                if boundary == "setup":
                    await block()
                async with original_factory(config) as owned:
                    yield owned
                    if boundary == "closure":
                        await block()
            finally:
                owners.append(asyncio.current_task())

        async def gate(*args):
            if args[2] == boundary:
                if boundary.startswith("final"):
                    assert_closed(model_transport)
                await block()
            return await original_gate(*args)

        async def candidate(*args, **kwargs):
            await original_candidate(*args, **kwargs)
            if boundary == "candidate" and args[0] == "generation":
                await block()

        runtime.specialist_factory, runtime._gate = factory, gate
        monkeypatch.setattr(detail, "candidate", candidate)
        with telemetry._tracer.start_as_current_span("caller") as parent:
            started = asyncio.get_running_loop().time()
            operation = asyncio.create_task(
                runtime.generate(GenerateCardAgentRequest(query="drake"))
            )
            try:
                await asyncio.wait_for(reached.wait(), 3)
                assert deadlines[0] >= started + 2
                assert deadlines[0] < started + 2.1
                if failure == "cancel":
                    operation.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await operation
                else:
                    with pytest.raises(RuntimeFailure) as caught:
                        await operation
                    assert caught.value.reason == "timeout"
                    assert asyncio.get_running_loop().time() >= deadlines[0]
            finally:
                if not operation.done():
                    operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
            record_property("whole_request_seconds", asyncio.get_running_loop().time() - started)
            assert trace.get_current_span() is parent
            assert detail.current() is None
            assert not (set(asyncio.all_tasks()) - baseline_tasks)
        if boundary != "pre_prompt":
            assert owners == [operation, operation]
        else:
            assert not owners

    asyncio.run(scenario())
    assert len(model_transport[0]) == count
    assert not content(exported)
    if count:
        assert_owned_lifecycle_drained(observed)
    else:
        assert not observed["created"] and not observed["captures"]
        assert_capacity_recovered()
    assert_closed(model_transport)


@pytest.mark.parametrize(
    ("case", "mutate"),
    [
        ("missing_required", lambda evidence: evidence.pop(0)),
        ("duplicate_required", lambda evidence: evidence.insert(1, evidence[1])),
        (
            "unexpected_stage",
            lambda evidence: evidence.append(
                evidence[-1].model_copy(update={"stage": "unexpected_stage"})
            ),
        ),
        ("reordered_required", lambda evidence: evidence.__setitem__(0, evidence[1])),
        ("malformed_entry", lambda evidence: evidence.__setitem__(0, {"stage": "pre_prompt"})),
        (
            "nonallowed_required",
            lambda evidence: evidence.__setitem__(
                0,
                evidence[0].model_copy(update={"decision": "blocked", "reason": "blocked"}),
            ),
        ),
        (
            "wrong_required_policy",
            lambda evidence: evidence.__setitem__(
                0, evidence[0].model_copy(update={"policy": "not-original-fantasy-v1"})
            ),
        ),
        (
            "wrong_required_reason",
            lambda evidence: evidence.__setitem__(
                1, evidence[1].model_copy(update={"reason": "not_observed"})
            ),
        ),
        (
            "wrong_required_decision",
            lambda evidence: evidence.__setitem__(
                2, evidence[2].model_copy(update={"decision": "unavailable"})
            ),
        ),
    ],
)
def test_real_graph_rejects_invalid_authoritative_evidence(model_transport, exported, case, mutate):
    runtime = CardOrchestrator(settings())
    original = runtime._gate

    async def gate(*args):
        await original(*args)
        if args[2] == "final_art_prompt":
            mutate(args[3])

    runtime._gate = gate
    try:
        result = asyncio.run(runtime.generate(GenerateCardAgentRequest(query="drake")))
    except RuntimeFailure:
        assert case == "malformed_entry"
        assert len(model_transport[0]) == 1
        assert not content(exported)
        assert_closed(model_transport)
        return
    assert result.status == "held"
    assert result.safetyHints == ["invalid_evidence"], case
    assert result.card is result.artPrompt is None
    assert len(model_transport[0]) == 1
    assert not content(exported)
    assert_closed(model_transport)


@pytest.mark.parametrize("http_child", [False, True], ids=["sdk-current", "http-child"])
def test_real_agents_preserve_original_identity_and_measured_stage_end(
    monkeypatch, model_transport, record_property, http_child
):
    calls, responses, *_ = model_transport
    runtime = CardOrchestrator(settings())

    async def respond(call):
        span = detail.current().execution.span
        assert span.name == detail.NAMES["generation"]
        if http_child:
            with provider.get_tracer("synthetic.http").start_as_current_span("http") as child:
                assert child.parent == span.get_span_context()
                await asyncio.sleep(0.003)
        return model_response(CARD)

    responses[:] = [respond]
    with batched_provider(monkeypatch) as (provider, exporter, _):
        with provider.get_tracer("caller").start_as_current_span("caller") as parent:
            result = asyncio.run(runtime.generate(GenerateCardAgentRequest(query="drake")))
            assert trace.get_current_span() is parent
        assert result.status == "completed"
        assert len(calls) == 1
        assert provider.force_flush()
        exported_spans = [
            span
            for span in exporter.get_finished_spans()
            if span.instrumentation_scope.name == detail.SCOPE
        ]
        assert len(exported_spans) == 1
        assert exported_spans[0].name == detail.NAMES["generation"]
        payload = json.loads(exported_spans[0].attributes["fcg.detail.record"])
        assert payload["stage"] == "generation"
        assert payload["validation"] == "validated"
        measured_ms = (exported_spans[0].end_time - exported_spans[0].start_time) / 1e6
        duration_error_ms = abs(payload["duration_ms"] - measured_ms)
        assert duration_error_ms < 5
        record_property("max_original_duration_error_ms", duration_error_ms)
    assert_closed(model_transport)


def test_eight_real_engine_requests_have_distinct_ownership(
    monkeypatch, model_transport, exported, record_property
):
    calls, responses, *_ = model_transport
    graphs, states, executors, agents, sessions = [], [], [], [], []
    original_build = workflow.build_workflow
    original_executor = AgentExecutor.__init__
    original_session = workflow.DeferredSpecialist.create_session
    original_agent = Agent.__init__
    actual_agents = []
    runs = []
    original_run = Agent.run
    observed = observe_hosted_lifecycle(monkeypatch)

    def build(state, *args, **kwargs):
        states.append(state)
        graph = original_build(state, *args, **kwargs)
        graphs.append(graph)
        return graph

    def executor(self, agent, *args, **kwargs):
        executors.append(self)
        agents.append(agent)
        return original_executor(self, agent, *args, **kwargs)

    def session(self, *args, **kwargs):
        value = original_session(self, *args, **kwargs)
        sessions.append(value)
        return value

    def agent(self, *args, **kwargs):
        original_agent(self, *args, **kwargs)
        actual_agents.append(self)

    async def run(self, *args, **kwargs):
        assert kwargs["stream"] is False
        assert any(kwargs["session"] is session for session in sessions)
        runs.append((self, kwargs["session"], detail.current_deadline()))
        return await original_run(self, *args, **kwargs)

    monkeypatch.setattr(workflow, "build_workflow", build)
    monkeypatch.setattr(AgentExecutor, "__init__", executor)
    monkeypatch.setattr(workflow.DeferredSpecialist, "create_session", session)
    monkeypatch.setattr(Agent, "__init__", agent)
    monkeypatch.setattr(Agent, "run", run)

    async def scenario():
        reached, release = asyncio.Event(), asyncio.Event()
        first = set()
        seen = {i: [] for i in range(8)}
        deadlines = {}

        async def respond(call):
            payload = user_payload(call)
            stage = next(
                stage
                for stage in STAGES
                if call["text"]["format"]["name"] == SCHEMAS[stage].__name__
            )
            index = int(payload["query"].rsplit(" ", 1)[1])
            deadlines[index] = detail.current_deadline()
            first.add(index)
            if len(first) == 8:
                reached.set()
            await release.wait()
            output = CARD | {"name": f"Sentinel {index}", "flavorText": f"ember {index}"}
            seen[index].append(stage)
            assert detail.current().deadline == deadlines[index]
            assert all(p.deadline == deadlines[index] for p in detail.current().pending)
            return model_response(output, refusal=(index == 1 and stage == "generation"))

        responses[:] = [respond]
        runtime = CardOrchestrator(settings())
        tasks = [
            asyncio.create_task(runtime.generate(GenerateCardAgentRequest(query=f"sentinel {i}")))
            for i in range(8)
        ]
        try:
            await asyncio.wait_for(reached.wait(), 5)
            assert len(states) == len(graphs) == 8
            assert len(executors) == len(agents) == len(sessions) == 8
            assert len(actual_agents) == 8
            for values in (states, graphs, executors, agents, sessions):
                assert len({id(value) for value in values}) == len(values)
            assert len({id(state.evidence) for state in states}) == 8
            tasks[0].cancel()
            release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert results[1].status == "refused"
        assert results[1].card is None
        assert seen[1] == ["generation"]
        for index, result in enumerate(results[2:], 2):
            assert result.status == "completed"
            assert result.card.name == f"Sentinel {index}"
            assert result.card.flavorText == f"ember {index}"
            assert seen[index] == list(STAGES)
        assert len(calls) == 8
        assert len(actual_agents) == len({id(agent) for agent in actual_agents}) == 8
        assert len(runs) == len({id(session) for _, session, _ in runs}) == 8
        assert_no_children()

    asyncio.run(scenario())
    records = content(exported)
    assert len(records) == 6
    assert len({record["source"]["trace_id"] for record in records}) == 6
    assert all(
        "Sentinel 0" not in json.dumps(r) and "Sentinel 1" not in json.dumps(r) for r in records
    )
    record_property(
        "isolation",
        {
            "requests": 8,
            "graphs": 8,
            "states": 8,
            "executors": 8,
            "deferred_adapters": 8,
            "agents": 8,
            "sessions": 8,
            "http_calls": len(calls),
            "completed": 6,
            "refused": 1,
            "cancelled": 1,
        },
    )
    assert_closed(model_transport)
    assert_owned_lifecycle_drained(observed)


@pytest.mark.parametrize("failure", ["serialization", "revalidation"])
def test_mandatory_graph_adapter_failure_is_closed(monkeypatch, model_transport, exported, failure):
    name = "model_dump_json" if failure == "serialization" else "model_validate_json"

    def fail(*args, **kwargs):
        raise ValueError("PRIVATE_GRAPH_ADAPTER")

    monkeypatch.setattr(GenerateCardAgentResponse, name, fail)
    with pytest.raises(RuntimeFailure):
        asyncio.run(CardOrchestrator(settings()).generate(GenerateCardAgentRequest(query="drake")))
    assert len(model_transport[0]) == 1
    assert not content(exported)
    assert_closed(model_transport)
    assert_capacity_recovered()


@pytest.mark.parametrize(
    "enabled,decision",
    [(False, Decision.RECORD_AND_SAMPLE), (True, Decision.DROP), (True, Decision.RECORD_ONLY)],
)
def test_real_engine_no_capture_work_when_disabled_or_unsampled(
    monkeypatch, model_transport, enabled, decision
):
    provider = TracerProvider(sampler=StaticSampler(decision))
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "_enabled", True)
    monkeypatch.setattr(telemetry, "_tracer", provider.get_tracer("baseline"))
    monkeypatch.setattr(detail, "_tracer", lambda: provider.get_tracer(detail.SCOPE))

    def forbidden(*args, **kwargs):
        pytest.fail("disabled/unsampled graph allocated or projected capture content")

    for name in ("candidate", "projection_view", "_contracts", "parse_envelope"):
        monkeypatch.setattr(detail, name, forbidden)
    monkeypatch.setattr(detail.Capture, "__init__", forbidden)
    monkeypatch.setattr(detail._capacity, "acquire", forbidden)
    try:
        result = asyncio.run(
            CardOrchestrator(settings(agent_trace_enabled=enabled)).generate(
                GenerateCardAgentRequest(query="drake")
            )
        )
        assert result.status == "completed"
        assert len(model_transport[0]) == 1
        assert not content(exporter)
        assert_closed(model_transport)
    finally:
        provider.shutdown()


def test_framework_canary_surfaces_and_host_storage(monkeypatch, model_transport, exported, caplog):
    from hosted_agents.card_orchestrator import server

    marker = "PRIVATE_FRAMEWORK_CANARY"
    store = NoResponseStore()
    monkeypatch.setattr(server, "NoResponseStore", lambda: store)
    host = create_host(settings())
    tracer = detail._tracer()
    provider = TracerProvider()
    provider.add_span_processor(telemetry.PrivacySpanProcessor())
    provider.add_span_processor(SimpleSpanProcessor(exported))
    sdk = provider.get_tracer("agent_framework")
    parent = SpanContext(123, 456, False, TraceFlags(1))
    try:
        with sdk.start_as_current_span(
            f"execute {marker}",
            attributes={"gen_ai.input.messages": marker, "private": marker},
            links=[Link(parent, {"private": marker})],
        ) as span:
            span.add_event(marker, {"private": marker})
            span.set_status(Status(StatusCode.ERROR, marker))
            logging.getLogger("agent_framework").error(marker)
        with provider.get_tracer("application.owned").start_as_current_span(
            "application stable name"
        ):
            pass
        result = asyncio.run(post(host, wire(marker)))
        assert result.status_code == 200
        body = result.json()
        assert len(body["output"]) == len(body["output"][0]["content"]) == 1
        assert json.loads(body["output"][0]["content"][0]["text"])["status"] == "completed"
        surfaces = [
            {
                "name": span.name,
                "attributes": dict(span.attributes),
                "events": [(e.name, dict(e.attributes)) for e in span.events],
                "links": [dict(link.attributes or {}) for link in span.links],
                "status": span.status.description,
            }
            for span in exported.get_finished_spans()
            if span.instrumentation_scope.name != detail.SCOPE
        ]
        assert marker not in json.dumps(surfaces) + caplog.text + result.text
        assert any(s["name"] == "application stable name" for s in surfaces)
        assert not store._entries and not store._item_store
        assert not store._conversation_responses and not store._stream_events
        assert tracer is detail._tracer()
    finally:
        provider.shutdown()
    assert_closed(model_transport)
