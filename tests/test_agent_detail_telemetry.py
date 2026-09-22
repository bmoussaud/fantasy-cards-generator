from __future__ import annotations

# ruff: noqa: E402
import asyncio
import hashlib
import json
import threading
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

pytest.importorskip("azure.ai.agentserver.responses")
pytest.importorskip("agent_framework.foundry")

from azure.monitor.opentelemetry.exporter.export.trace._exporter import _convert_span_to_envelope
from openai import APIStatusError
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from app import agent_detail as detail
from app import telemetry
from app.foundry_agent_client import (
    FoundryAgentClient,
    FoundryAgentInvocationResult,
    GenerateCardAgentRequest,
    GenerateCardAgentResponse,
)
from app.generation import (
    AuthenticatedOwner,
    CardGenerationService,
    UpstreamServiceError,
)
from app.problems import ProblemDetails
from app.settings import SettingsError, load_app_settings, parse_agent_trace_enabled
from hosted_agents.card_orchestrator import specialists
from hosted_agents.card_orchestrator.orchestrator import CardOrchestrator
from hosted_agents.card_orchestrator.server import create_host
from hosted_agents.card_orchestrator.settings import RuntimeSettings
from hosted_agents.card_orchestrator.workflow import StageBoundary
from tests.conftest import extract_hidden_value
from tests.test_agentic_generation import StageModeration, _client, _generate, _services
from tests.test_card_orchestrator import ART, CARD, LORE, settings
from tests.workflow_fakes import OfflineAgent

OWNER = AuthenticatedOwner(
    owner_id="test-owner",
    tenant_id=None,
    object_id=None,
    subject="test",
    display_name=None,
    email=None,
)
PROMPT = "A mountain guardian with an amber shield"


@pytest.fixture
def exported(monkeypatch):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(telemetry.PrivacySpanProcessor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "_enabled", True)
    monkeypatch.setattr(telemetry, "_tracer", provider.get_tracer("baseline"))
    monkeypatch.setattr(detail, "_tracer", lambda: provider.get_tracer(detail.SCOPE))
    monkeypatch.setattr(detail, "_capacity", threading.BoundedSemaphore(16))
    yield exporter
    provider.shutdown()


def content(exporter):
    return [
        json.loads(span.attributes["fcg.detail.record"])
        for span in exporter.get_finished_spans()
        if "fcg.detail.record" in span.attributes
    ]


def new_spans(exporter):
    return [
        span
        for span in exporter.get_finished_spans()
        if span.instrumentation_scope.name == detail.SCOPE
    ]


class LocalHostedTransport(httpx.AsyncBaseTransport):
    def __init__(self, host, *, legacy=False):
        self.host = host
        self.legacy = legacy
        self.headers = []
        self.responses = []

    async def handle_async_request(self, request):
        self.headers.append(dict(request.headers))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.host), base_url="http://offline"
        ) as client:
            response = await client.post(
                "/responses", content=request.content, headers=request.headers
            )
        body = response.json()
        if self.legacy and response.status_code == 200:
            part = body["output"][0]["content"][0]
            payload = json.loads(part["text"])
            if payload["status"] == "completed":
                payload["metadata"]["agentDetail"] = legacy_envelope(
                    request.headers["traceparent"].split("-")[1],
                    payload["metadata"].get("hostedVersion") or payload["metadata"]["agentVersion"],
                )
                part["text"] = json.dumps(payload)
        self.responses.append(body)
        return httpx.Response(response.status_code, json=body, request=request)


def stack(monkeypatch, *, web=True, hosted=True, outputs=None, legacy=False):
    calls = []
    outputs = outputs or [CARD, LORE, ART]

    class Agent(OfflineAgent):
        async def respond(self, stage, payload):
            index = {"concept": 0, "lore": 1, "art_direction": 2}[stage]
            calls.append((self.name, payload, self.instructions, self.default_options))
            output = outputs[index]
            if isinstance(output, BaseException):
                raise output
            return output

        def __init__(self, client, **kwargs):
            super().__init__(self.respond, **kwargs)

    monkeypatch.setattr(specialists, "Agent", Agent)

    @asynccontextmanager
    async def factory(_settings):
        yield specialists.FoundrySpecialists(None)

    hosted_settings = settings(agent_trace_enabled=hosted and not legacy)
    runtime = CardOrchestrator(hosted_settings, specialist_factory=factory)
    transport = LocalHostedTransport(
        create_host(hosted_settings, orchestrator=runtime), legacy=legacy and hosted
    )
    services = _services(monkeypatch, None)
    services.settings = replace(
        services.settings,
        agent_trace_enabled=web,
        foundry_agent_timeout_seconds=5,
        retry=replace(services.settings.retry, overall_timeout_seconds=10),
    )
    services.agent_client = FoundryAgentClient(
        services.settings,
        credential=SimpleNamespace(get_token=lambda *_: "offline-token"),
        transport=transport,
    )
    return services, runtime, transport, calls


async def generate(service, key="fresh", prompt=PROMPT):
    return await service.generate_card(
        owner=OWNER,
        prompt=prompt,
        idempotency_key=key,
        request_id="request",
        client_ip="127.0.0.1",
    )


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, True),
        ("true", True),
        (" TRUE ", True),
        ("false", False),
        ("\tFaLsE\n", False),
    ],
)
def test_startup_flag_parity(value, expected):
    env = {
        "FOUNDRY_PROJECT_ENDPOINT": "https://example.services.ai.azure.com/api/projects/test",
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": "test",
        "CARD_ORCHESTRATOR_VERSION": "test",
        "AGENT_TRACE_ENABLED": "platform-owned-value",
    }
    if value is not None:
        env["FCG_AGENT_TRACE_ENABLED"] = value
    assert parse_agent_trace_enabled(env) is expected
    assert RuntimeSettings.from_env(env).agent_trace_enabled is expected


@pytest.mark.parametrize("value", ["", " ", "1", "0", "yes", "off", "truthy"])
def test_invalid_flag_is_safe_startup_error(value):
    with pytest.raises(SettingsError, match="FCG_AGENT_TRACE_ENABLED must be true or false"):
        parse_agent_trace_enabled({"FCG_AGENT_TRACE_ENABLED": value})
    with pytest.raises(SettingsError):
        RuntimeSettings.from_env({"FCG_AGENT_TRACE_ENABLED": value})


@pytest.mark.parametrize("environment", ["development", "production", "test"])
def test_default_on_in_every_environment_is_startup_scoped(monkeypatch, environment):
    monkeypatch.setenv("APP_ENV", environment)
    monkeypatch.setenv("TELEMETRY_ENABLED", "true")
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=offline")
    monkeypatch.delenv("FCG_AGENT_TRACE_ENABLED", raising=False)
    original = load_app_settings()
    assert original.agent_trace_enabled is True
    monkeypatch.setenv("FCG_AGENT_TRACE_ENABLED", "false")
    assert original.agent_trace_enabled is True
    assert load_app_settings().agent_trace_enabled is False
    monkeypatch.setenv("FCG_AGENT_TRACE_ENABLED", "")
    with pytest.raises(SettingsError, match="FCG_AGENT_TRACE_ENABLED"):
        load_app_settings()


@pytest.mark.parametrize("web,hosted", [(True, True), (True, False), (False, True), (False, False)])
def test_full_entrypoints_mixed_flags_and_private_terminal_release(
    monkeypatch, exported, web, hosted
):
    services, _, transport, calls = stack(monkeypatch, web=web, hosted=hosted)
    client = _client(monkeypatch, services)
    response = _generate(client)
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    records = content(exported)
    assert len(records) == (3 if hosted else 0) + (2 if web else 0)
    assert "agentDetail" not in json.dumps(transport.responses)
    assert "agentDetail" not in response.text
    assert "capture_version" not in response.text
    assert len(calls) == 3
    execution = [s for s in new_spans(exported) if s.name != "fcg.agent.detail"]
    assert {s.name for s in execution} == (
        ({"card_concept", "card_lore", "card_art_direction"} if hosted else set())
        | ({"fcg.agent.invoke", "fcg.agent.image"} if web else set())
    )
    assert all(
        ("fcg.detail.record" in s.attributes) == s.name.startswith("card_") for s in execution
    )
    assert all(s.status.description is None for s in new_spans(exported))
    if web and hosted:
        hosted_records = {r["stage"]: r for r in records if r["source_runtime"] == "hosted"}
        assert json.loads(hosted_records["concept"]["input"]["text"]) == calls[0][1]
        assert json.loads(hosted_records["lore"]["input"]["text"]) == CARD
        assert json.loads(hosted_records["art_direction"]["input"]["text"]) == CARD | LORE
        assert json.loads(hosted_records["lore"]["output"]["text"]) == LORE
        assert hosted_records["lore"]["output_kind"] == "refinement"
        for name, _, instructions, _ in calls:
            record = hosted_records[name.removeprefix("card_")]
            assert (
                record["instruction"]["text"] == detail.text_view(instructions, trusted=True).text
            )
            assert "Schema:" in instructions
        invocation = next(s for s in execution if s.name == "fcg.agent.invoke")
        assert (
            transport.headers[0]["traceparent"].split("-")[1]
            == f"{invocation.context.trace_id:032x}"
        )
        assert "baggage" not in transport.headers[0]
        sources = {s.context.span_id: s for s in execution}
        for span in new_spans(exported):
            if span.name == "fcg.agent.detail":
                assert len(span.links) == 1
                assert span.links[0].context.span_id in sources
                assert span.links[0].attributes == {}
        for span in execution:
            assert span.attributes["fcg.detail.result"] == "completed"
        # Exercise the actual Azure exporter conversion, not only helper spies.
        envelopes = [_convert_span_to_envelope(s).as_dict() for s in new_spans(exported)]
        assert "Mountain Drake" in json.dumps(envelopes)
    before = len(new_spans(exported))
    replay = _generate(client)
    assert replay.json() == response.json()
    assert len(new_spans(exported)) == before
    assert "agentDetail" not in repr(services.card_repository.__dict__)
    assert "agentDetail" not in repr(services.audit_repository.__dict__)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("stage", ["post_text", "post_art_prompt", "post_image"])
def test_late_web_denial_preserves_only_new_hosted_content(monkeypatch, exported, stage, legacy):
    services, _, _, _ = stack(monkeypatch, legacy=legacy)
    services.moderation_service = StageModeration(stage)
    with pytest.raises(ProblemDetails):
        asyncio.run(generate(CardGenerationService(services)))
    assert len(content(exported)) == (0 if legacy else 3)
    assert all(r["source_runtime"] == "hosted" for r in content(exported))


@pytest.mark.parametrize(
    "failure",
    [
        "partial",
        "partial_timeout",
        "persistence",
        "card_write",
        "response",
        "cancel",
        "unknown_safety",
    ],
)
@pytest.mark.parametrize("legacy", [False, True])
def test_late_non_success_suppresses_web_content(monkeypatch, exported, failure, legacy):
    services, _, _, _ = stack(monkeypatch, legacy=legacy)
    if failure == "partial":

        async def image(*args, **kwargs):
            raise UpstreamServiceError("foundry-image", "PRIVATE-ERROR", retryable=False)

        services.ai_client.generate_image = image
    elif failure == "partial_timeout":

        async def image(*args, **kwargs):
            raise TimeoutError("PRIVATE-ERROR")

        services.ai_client.generate_image = image
    elif failure == "persistence":

        async def save(*args, **kwargs):
            raise RuntimeError("PRIVATE-ERROR")

        services.asset_store.upload = save
    elif failure == "card_write":

        async def save(*args, **kwargs):
            raise RuntimeError("PRIVATE-ERROR")

        services.card_repository.save = save
    elif failure == "response":
        monkeypatch.setattr(
            CardGenerationService,
            "_as_response",
            lambda *args: (_ for _ in ()).throw(RuntimeError("PRIVATE-ERROR")),
        )
    elif failure == "cancel":

        async def image(*args, **kwargs):
            raise asyncio.CancelledError

        services.ai_client.generate_image = image
    else:

        class Unknown(StageModeration):
            async def moderate_text(self, text, *, stage):
                return (await super().moderate_text(text, stage=stage)).model_copy(
                    update={"reasonCode": "unknown"}
                )

        services.moderation_service = Unknown()
    try:
        result = asyncio.run(generate(CardGenerationService(services)))
        assert result.status == (
            "awaiting_artwork_retry" if failure in {"partial", "partial_timeout"} else "completed"
        )
    except (ProblemDetails, RuntimeError, asyncio.CancelledError):
        assert failure in {"persistence", "card_write", "response", "cancel"}
    assert len(content(exported)) == (0 if legacy else 3)
    assert all(r["source_runtime"] == "hosted" for r in content(exported))
    assert "PRIVATE-ERROR" not in repr(exported.get_finished_spans())


@pytest.mark.parametrize("legacy", [False, True])
def test_real_http_serialization_failure_does_not_release_web(monkeypatch, exported, legacy):
    services, _, _, _ = stack(monkeypatch, legacy=legacy)
    client = _client(monkeypatch, services)
    import fastapi.routing

    original = fastapi.routing.serialize_response

    async def fail_card(*args, **kwargs):
        response = kwargs.get("response_content")
        if getattr(response, "status", None) == "completed":
            raise RuntimeError("serialization failure")
        return await original(*args, **kwargs)

    monkeypatch.setattr(fastapi.routing, "serialize_response", fail_card)
    with pytest.raises(RuntimeError):
        _generate(client)
    assert len(content(exported)) == (0 if legacy else 3)


def test_real_image_retry_records_attempts_not_failed_content(monkeypatch, exported):
    services, _, _, _ = stack(monkeypatch)
    services.settings = replace(
        services.settings, retry=replace(services.settings.retry, image_max_retries=1)
    )
    original = services.ai_client.generate_image
    attempts = []

    async def image(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise UpstreamServiceError("foundry-image", "PRIVATE-ERROR", retryable=True)
        return await original(*args, **kwargs)

    services.ai_client.generate_image = image
    assert asyncio.run(generate(CardGenerationService(services))).status == "completed"
    spans = [s for s in new_spans(exported) if s.name == "fcg.agent.image"]
    assert [s.attributes["fcg.detail.result"] for s in spans] == ["failed", "completed"]
    assert len([r for r in content(exported) if r["stage"] == "image"]) == 1
    assert "PRIVATE-ERROR" not in repr(exported.get_finished_spans())


def test_concurrent_single_flight_does_not_duplicate_content(monkeypatch, exported):
    services, _, _, calls = stack(monkeypatch)
    service = CardGenerationService(services)

    async def scenario():
        return await asyncio.gather(generate(service), generate(service))

    a, b = asyncio.run(scenario())
    assert a == b
    assert len(calls) == 3
    assert len(content(exported)) == 5


@pytest.mark.parametrize("delta", [-1, 0, 1])
@pytest.mark.parametrize("unit", ["a ", "é", "e\u0301", '"', "😀"])
def test_utf8_exact_field_boundaries(delta, unit):
    n = detail.FIELD_BYTES + delta
    width = len(unit.encode())
    text = unit * (n // width) + " " * (n % width)
    view = detail.text_view(text)
    assert len(view.text.encode()) <= detail.FIELD_BYTES
    assert ("truncated" in view.flags) is (delta == 1)
    assert json.loads(detail.encoded(view.model_dump(mode="json")))["text"] == view.text


@pytest.mark.parametrize(
    "sentinel",
    [
        "user@example.test",
        "Bearer canary-secret",
        "Cookie: private-cookie",
        "api_key=private-canary",
        "password=private-canary",
        "owner_id=private-owner",
        "https://storage.test/image?sig=private",
        "data:image/png;base64,PRIVATE",
        "a" * 100,
        "12345678-1234-1234-1234-123456789012",
        "+1 (555) 123-4567",
    ],
)
def test_sanitize_before_truncate_withheld_canaries(sentinel):
    value = "safe " * 420 + sentinel
    view = detail.text_view(value)
    assert view.text == ""
    assert view.flags == ("redacted",)


def record(runtime="web", *, stage=None, text="safe", source=None):
    stage = stage or ("image" if runtime == "web" else "concept")
    kind = {
        "image": "image_outcome",
        "hosted_invocation": "final_card",
        "concept": "card",
        "lore": "refinement",
        "art_direction": "refinement",
    }[stage]
    instruction, _, _ = detail._contracts(stage)
    inputs = {
        "concept": {"query": PROMPT},
        "lore": CARD,
        "art_direction": CARD | LORE,
        "hosted_invocation": {"query": PROMPT},
        "image": {"artPrompt": PROMPT, "quality": "low", "mode": "generate"},
    }
    outputs = {
        "concept": CARD,
        "lore": LORE,
        "art_direction": ART,
        "hosted_invocation": CARD | LORE | ART,
        "image": {"outcome": "completed"},
    }
    views = [
        detail.text_view(instruction, trusted=True),
        detail.projection_view(inputs[stage], fields=set(inputs[stage])),
        detail.projection_view(outputs[stage], fields=set(outputs[stage])),
    ]
    result = detail.Record(
        source_runtime=runtime,
        stage=stage,
        agent_name=detail.AGENTS[stage],
        source=source or detail.Source(trace_id="1" * 32, span_id="2" * 16),
        duration_ms=1,
        instruction_digest=hashlib.sha256(instruction.encode()).hexdigest(),
        deployment_version="test",
        output_kind=kind,
        instruction=views[0],
        input=views[1],
        output=views[2],
        flags=detail._record_flags(*views),
    )
    if text != "safe":
        # Budget-only fixtures deliberately bypass the content contract.
        result = result.model_copy(
            update={name: detail.View(text=text) for name in ("instruction", "input", "output")}
        )
    return result


def legacy_envelope(trace_id="1" * 32, deployment_version="test"):
    """The unchanged v1 carrier from pre-162 producers, not new HOSTED export."""
    return detail.Envelope(
        records=tuple(
            record(
                "hosted",
                stage=stage,
                source=detail.Source(trace_id=trace_id, span_id=f"{index:016x}"),
            ).model_copy(update={"deployment_version": deployment_version})
            for index, stage in enumerate(("concept", "lore", "art_direction"), start=1)
        )
    ).model_dump(mode="json")


@contextmanager
def capture(runtime="web"):
    assert detail._capacity.acquire(blocking=False)
    state = detail.Capture(runtime)
    token = detail._current.set(state)
    try:
        yield state
    finally:
        detail._current.reset(token)
        state.close()


def test_record_and_runtime_shares_include_json_overhead(exported):
    with capture() as state:
        for stage in ("concept", "lore", "art_direction"):
            state.append(record("hosted", stage=stage, text=" " * 2048))
        for _ in range(6):
            state.append(record(text=" " * 2048))
        for runtime in ("web", "hosted"):
            records = [
                r.model_dump(mode="json") for r in state.records if r.source_runtime == runtime
            ]
            assert len(detail.encoded({"version": 1, "records": records})) <= 24576
        assert len(state.records) <= 8
        assert sum(len(detail.encoded(r.model_dump(mode="json"))) for r in state.records) <= 49152
        escaped = record(text='"' * 2048)
        assert len(detail.encoded(escaped.model_dump(mode="json"))) > 8192
        before = len(state.records)
        state.append(escaped)
        assert len(state.records) == before


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_exact_runtime_serialized_limit(monkeypatch, exported, delta):
    item = record()
    n = len(detail.encoded({"version": 1, "records": [item.model_dump(mode="json")]}))
    monkeypatch.setattr(detail, "RUNTIME_BYTES", n + delta)
    with capture() as state:
        state.append(item)
        assert len(state.records) == (0 if delta == -1 else 1)


def test_count_and_span_budgets(exported):
    with capture() as state:
        for _ in range(10):
            state.append(record())
            state.append(record("hosted"))
        assert len(state.records) == 8
        for _ in range(40):
            with detail.execution("image"):
                pass
        state.release()
        assert len(new_spans(exported)) == 16


def test_off_never_allocates_or_sanitizes(monkeypatch, exported):
    services, _, _, _ = stack(monkeypatch, web=False, hosted=False)

    def forbidden(*args, **kwargs):
        pytest.fail("OFF performed capture-only work")

    for name in ("Capture", "text_view", "projection_view", "parse_envelope", "invocation_outcome"):
        monkeypatch.setattr(detail, name, forbidden)
    assert asyncio.run(generate(CardGenerationService(services))).status == "completed"
    assert not new_spans(exported)


def test_sixteen_active_buffers_without_waiting_and_cleanup(exported):
    active = []

    class Runner:
        settings = SimpleNamespace(agent_trace_enabled=True, version="test")

        @detail.operation("hosted")
        async def run(self, gate, all_held):
            assert detail.current() is None
            with detail.execution("concept") as execution:
                if execution is not None:
                    assert execution.span.is_recording()
                    assert execution.span.get_span_context().trace_flags.sampled
            active.append(detail.current())
            if len(active) == 17:
                all_held.set()
            await gate.wait()
            return GenerateCardAgentResponse(schemaVersion=1, status="held")

    async def scenario():
        gate, all_held = asyncio.Event(), asyncio.Event()
        tasks = [asyncio.create_task(Runner().run(gate, all_held)) for _ in range(17)]
        try:
            # Hang watchdog only; admission counts, not throughput, are the contract.
            async with asyncio.timeout(2 * settings().timeout_seconds):
                await all_held.wait()
            assert len(active) == 17
            admitted = [state for state in active if state is not None]
            assert len(admitted) == len({id(state) for state in admitted}) == 16
            assert active[-1] is None
            assert all(
                not state.closed and len(state.pending) == state.spans == 1 for state in admitted
            )
            assert [span.name for span in new_spans(exported)] == ["card_concept"]
            assert not content(exported)
            assert not detail._capacity.acquire(blocking=False)
        finally:
            gate.set()
            await asyncio.gather(*tasks)
        assert all(
            state.closed and not state.pending and not state.records and state.execution is None
            for state in admitted
        )
        assert len(new_spans(exported)) == 17
        assert not content(exported)
        permits = []
        try:
            permits = [detail._capacity.acquire(blocking=False) for _ in range(17)]
            assert permits == [True] * 16 + [False]
        finally:
            for acquired in permits:
                if acquired:
                    detail._capacity.release()
        assert detail.current() is None
        assert detail._admission.get() is None
        assert detail._ending.get() is None

    asyncio.run(scenario())


def test_capture_is_immutable_and_record_count_bounded(exported):
    with capture() as state:
        for _ in range(8):
            state.append(record(text=" " * 2048))
        assert len(state.records) <= 5
        with pytest.raises(ValueError):
            state.records[0].input.text = "mutated"


@pytest.mark.parametrize("mutation", ["version", "extra", "context", "overflow", "safety"])
def test_malformed_private_extension_preserves_business_success(monkeypatch, exported, mutation):
    services, runtime, _, _ = stack(monkeypatch)
    result = asyncio.run(runtime.generate(GenerateCardAgentRequest(query=PROMPT)))
    assert result.status == "completed"
    assert len(content(exported)) == 3
    exported.clear()
    value = legacy_envelope()
    result.metadata["agentDetail"] = value
    evidence = result.metadata["safetyEvidence"]
    if mutation == "version":
        value["version"] = 99
    elif mutation == "extra":
        value["unknown"] = "PRIVATE"
    elif mutation == "context":
        value["records"][0]["source"]["span_id"] = "model-supplied"
    elif mutation == "overflow":
        value["records"][0]["input"]["text"] = " " * 100000
    else:
        evidence[0]["decision"] = "unavailable"
    assert detail.parse_envelope(value, evidence) is None
    from app.foundry_agent_client import _parse_success_envelope

    parsed = _parse_success_envelope(
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": result.model_dump_json()}],
                }
            ],
        },
        request_id=None,
        expected_version=None,
        capture_details=True,
    )
    assert parsed.success
    assert parsed.agent_detail is None


def test_sdk_attributes_cannot_opt_in_to_dedicated_content(monkeypatch, exported):
    from opentelemetry.sdk.trace import TracerProvider

    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(telemetry.PrivacySpanProcessor())
    provider.add_span_processor(SimpleSpanProcessor(exported))
    with provider.get_tracer("sdk").start_as_current_span("fcg.agent.detail") as span:
        span.set_attribute(
            "fcg.detail.record", detail.encoded(record().model_dump(mode="json")).decode()
        )
        span.add_event("fcg.agent.detail", {"fcg.detail.record": "PRIVATE"})
    assert not content(exported)
    assert not exported.get_finished_spans()[-1].events
    provider.shutdown()


@pytest.mark.parametrize(
    "outcome", ["refused", "held", "routing_defer", "invalid", "exception", "timeout", "cancel"]
)
def test_late_hosted_failure_never_transports_earlier_content(monkeypatch, exported, outcome):
    services, _, transport, _ = stack(monkeypatch)
    original = StageBoundary.invoke

    async def run(self, context, call_next):
        if self.stage == "art_direction":
            if outcome == "invalid":
                return specialists.SpecialistResult("completed", text='{"unexpected":"PRIVATE"}')
            if outcome == "exception":
                raise RuntimeError("PRIVATE-EXCEPTION")
            if outcome == "timeout":
                raise TimeoutError("PRIVATE-TIMEOUT")
            if outcome == "cancel":
                raise asyncio.CancelledError
            return specialists.SpecialistResult(outcome)
        return await original(self, context, call_next)

    monkeypatch.setattr(StageBoundary, "invoke", run)
    with pytest.raises((ProblemDetails, asyncio.CancelledError)):
        asyncio.run(generate(CardGenerationService(services)))
    assert not content(exported)
    assert "agentDetail" not in json.dumps(transport.responses)
    assert "PRIVATE" not in repr(exported.get_finished_spans())
    spans = new_spans(exported)
    assert any(s.name == "card_concept" for s in spans)
    art = next(s for s in spans if s.name == "card_art_direction")
    assert art.attributes["fcg.detail.validation"] == "unvalidated"


def test_capture_failures_never_fail_generation(monkeypatch, exported, caplog):
    services, _, _, _ = stack(monkeypatch)

    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE-DIAGNOSTIC-ERROR")

    monkeypatch.setattr(detail, "projection_view", fail)
    result = asyncio.run(generate(CardGenerationService(services)))
    assert result.status == "completed"
    assert not content(exported)
    assert "agent.detail.omitted" in caplog.text
    assert "PRIVATE" not in caplog.text
    assert all(r.exc_info is None for r in caplog.records)


def test_export_failure_does_not_break_completed_http_response(monkeypatch, exported, caplog):
    services, _, _, _ = stack(monkeypatch)
    client = _client(monkeypatch, services)
    original = detail.Capture._release

    def fail_release(self):
        if self.records:
            raise RuntimeError("PRIVATE-EXPORT-ERROR")
        return original(self)

    monkeypatch.setattr(detail.Capture, "_release", fail_release)
    response = _generate(client)
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert not content(exported)
    assert "agent.detail.omitted" in caplog.text
    assert "PRIVATE" not in caplog.text


@pytest.mark.parametrize("audit_failure", [False, True])
def test_artwork_retry_is_web_only_and_replay_has_no_details(monkeypatch, exported, audit_failure):
    services, _, _, calls = stack(monkeypatch)
    original = services.ai_client.generate_image

    async def fail(*args, **kwargs):
        raise UpstreamServiceError("foundry-image", "unavailable", retryable=False)

    services.ai_client.generate_image = fail
    service = CardGenerationService(services)

    async def scenario():
        partial = await generate(service)
        assert partial.status == "awaiting_artwork_retry"
        assert len(content(exported)) == 3
        services.ai_client.generate_image = original
        if audit_failure:

            async def fail_audit(*args, **kwargs):
                raise RuntimeError("audit unavailable")

            services.audit_repository.save_audit = fail_audit
        completed = await service.retry_artwork(
            owner=OWNER,
            card_id=partial.cardId,
            idempotency_key="retry",
            request_id="retry-request",
            client_ip="127.0.0.1",
        )
        before = len(new_spans(exported))
        replay = await generate(service)
        assert replay == completed
        assert len(new_spans(exported)) == before
        return completed

    if audit_failure:
        with pytest.raises(RuntimeError, match="audit unavailable"):
            asyncio.run(scenario())
        assert len(content(exported)) == 3
        return
    result = asyncio.run(scenario())
    assert result.status == "completed"
    records = [r for r in content(exported) if r["source_runtime"] == "web"]
    assert len(records) == 1
    assert records[0]["stage"] == "image"
    assert records[0]["source_runtime"] == "web"
    assert records[0]["operation"] == "artwork_retry"
    assert len(calls) == 3


def test_concurrent_distinct_requests_do_not_share_candidates(monkeypatch, exported):
    services, _, _, _ = stack(monkeypatch)
    service = CardGenerationService(services)

    async def scenario():
        return await asyncio.gather(
            generate(service, key="one", prompt="An amber guardian"),
            generate(service, key="two", prompt="An emerald guardian"),
        )

    results = asyncio.run(scenario())
    assert results[0].cardId != results[1].cardId
    records = content(exported)
    assert len(records) == 10
    queries = [json.loads(r["input"]["text"])["query"] for r in records if r["stage"] == "concept"]
    assert sorted(queries) == ["An amber guardian", "An emerald guardian"]
    concept_ids = [r["source"]["span_id"] for r in records if r["stage"] == "concept"]
    assert len(set(concept_ids)) == 2


def test_independent_baseline_telemetry_gate(monkeypatch, exported):
    services, _, _, _ = stack(monkeypatch)
    monkeypatch.setattr(telemetry, "_enabled", False)
    result = asyncio.run(generate(CardGenerationService(services)))
    assert result.status == "completed"
    assert not new_spans(exported)
    assert not content(exported)


@pytest.mark.parametrize(
    "canary",
    [
        "user@example.test",
        "Cookie: PRIVATE",
        "https://x.test/?sig=PRIVATE",
        "ghp_" + "Ab3d" * 9,
        "card_id=synthetic-private-card",
        "192.0.2.123",
        "192.0.2.123:443",
        "2001:db8::123",
        "[2001:db8::123]:443",
        "gho_" + "Ab3d" * 9,
        "ghu_" + "Ab3d" * 9,
        "ghs_" + "Ab3d" * 9,
        "ghr_" + "Ab3d" * 9,
        "github_pat_" + "Ab3d" * 8,
        "resource_id=synthetic-private-resource",
    ],
)
def test_canaries_absent_from_exported_spans_envelopes_logs_and_carrier(
    monkeypatch, exported, caplog, canary
):
    services, _, transport, _ = stack(monkeypatch)
    result = asyncio.run(generate(CardGenerationService(services), prompt=f"A guardian {canary}"))
    assert result.status == "completed"
    assert canary not in repr(exported.get_finished_spans())
    envelopes = [_convert_span_to_envelope(s).as_dict() for s in new_spans(exported)]
    assert canary not in json.dumps(envelopes)
    assert canary not in caplog.text
    assert canary not in json.dumps(transport.responses)
    assert any("redacted" in r["input"]["flags"] for r in content(exported))


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_exact_record_serialized_limit(monkeypatch, exported, delta):
    item = record(text='" é ' * 100)
    n = len(detail.encoded(item.model_dump(mode="json")))
    monkeypatch.setattr(detail, "RECORD_BYTES", n + delta)
    with capture() as state:
        state.append(item)
        assert len(state.records) == (0 if delta == -1 else 1)


def test_candidate_budget_preserves_priority_and_coexisting_flags(monkeypatch, exported):
    item = record("hosted")
    omitted = detail.View(flags=("omitted_budget",))
    reduced = item.model_copy(
        update={
            "output": omitted,
            "flags": detail._record_flags(item.instruction, item.input, omitted),
            "validation": "modified",
        }
    )
    ceiling = len(detail.encoded(reduced.model_dump(mode="json"))) + 10
    monkeypatch.setattr(detail, "RECORD_BYTES", ceiling)

    async def scenario():
        with capture("hosted") as state:
            with detail.execution("concept") as execution:
                execution.outcome = "completed"
            await detail.candidate(
                "concept",
                execution,
                instruction=specialists.effective_instructions("concept"),
                input_payload={"query": PROMPT},
                output_payload=CARD,
                input_fields={"query"},
                output_fields=detail.CARD_FIELDS,
                output_kind="card",
                deployment_version="test",
            )
            assert len(state.records) == 1
            candidate = state.records[0]
            assert candidate.instruction == item.instruction
            assert candidate.input == item.input
            assert "omitted_budget" in candidate.flags
            assert candidate.output.flags == ("omitted_budget",)
            assert candidate.validation == "modified"
            assert len(detail.encoded(candidate.model_dump(mode="json"))) <= ceiling
            detail.Record.model_validate_json(candidate.model_dump_json(), strict=True)
        view = detail.projection_view(
            {"a": "Cookie: PRIVATE", "b": "safe " * 500}, fields={"a", "b"}
        )
        assert view.text == ""
        assert set(view.flags) == {"redacted", "truncated", "omitted_budget"}

    asyncio.run(scenario())


def test_snapshot_precedes_specialist_mutation(monkeypatch, exported):
    services, _, _, _ = stack(monkeypatch)
    original = StageBoundary.invoke

    async def mutate(self, context, call_next):
        if self.stage == "lore":
            payload = json.loads(context.messages[0].text)
            payload["card"]["name"] = "A changed runtime input"
            context.messages[0].contents[0].text = json.dumps(payload)
        return await original(self, context, call_next)

    monkeypatch.setattr(StageBoundary, "invoke", mutate)
    asyncio.run(generate(CardGenerationService(services)))
    lore = next(r for r in content(exported) if r["stage"] == "lore")
    assert json.loads(lore["input"]["text"])["name"] == CARD["name"]


@pytest.mark.parametrize("version", [True, 1.0, "1", 2])
def test_record_versions_are_independently_strict(version):
    value = record().model_dump(mode="json")
    value["capture_version"] = version
    with pytest.raises(ValueError):
        detail.Record.model_validate_json(detail.encoded(value), strict=True)


@pytest.mark.parametrize("corruption", ["unknown_version", "unsafe_content", "private_content"])
def test_invalid_or_unsafe_carrier_degrades_entire_web_operation_content_free(
    monkeypatch, exported, corruption
):
    services, _, transport, _ = stack(monkeypatch, legacy=True)
    original = transport.handle_async_request

    async def tamper(request):
        response = await original(request)
        body = response.json()
        part = body["output"][0]["content"][0]
        payload = json.loads(part["text"])
        extension = payload["metadata"]["agentDetail"]
        if corruption == "unknown_version":
            extension["version"] = 2
        else:
            extension["records"][0]["output"]["text"] = (
                "graphic gore" if corruption == "unsafe_content" else "user@example.test"
            )
        part["text"] = json.dumps(payload)
        return httpx.Response(200, json=body, request=request)

    transport.handle_async_request = tamper
    result = asyncio.run(generate(CardGenerationService(services)))
    assert result.status == "completed"
    assert not content(exported)


def test_terminal_release_is_single_use(exported):
    with capture() as state:
        state.append(record())
        state.release()
        assert len(content(exported)) == 1
        state.release()
        assert len(content(exported)) == 1


@pytest.mark.parametrize(
    "corruption",
    [
        "instruction_cookie",
        "instruction_self_signed",
        "instruction_digest",
        "instruction_version",
        "instruction_flags",
        "record_suppressed",
        "output_suppressed",
        "unknown_projection",
        "missing_projection",
        "wrong_projection_type",
        "invalid_projection_enum",
        "truncated_projection",
        "duplicate_projection_key",
        "escaped_private",
        "escaped_policy",
        "source_trace",
        "duplicate_source",
        "deployment_version",
    ],
)
def test_untrusted_carrier_is_withheld_through_terminal_export(
    monkeypatch, exported, caplog, corruption
):
    services, _, transport, _ = stack(monkeypatch, legacy=True)
    original = transport.handle_async_request
    canary = "Cookie: SYNTHETIC-CARRIER-CANARY"

    async def tamper(request):
        response = await original(request)
        body = response.json()
        part = body["output"][0]["content"][0]
        payload = json.loads(part["text"])
        records = payload["metadata"]["agentDetail"]["records"]
        record = records[1]  # Reject after an earlier candidate has been considered.
        if corruption.startswith("instruction"):
            if corruption in {"instruction_cookie", "instruction_self_signed"}:
                record["instruction"]["text"] = canary
                record["instruction"]["flags"] = (
                    ["suppressed_policy"] if corruption == "instruction_cookie" else []
                )
                record["instruction_digest"] = hashlib.sha256(canary.encode()).hexdigest()
            elif corruption == "instruction_digest":
                record["instruction_digest"] = "f" * 64
            elif corruption == "instruction_version":
                record["instruction_version"] = "unsupported-v2"
            else:
                record["instruction"]["flags"] = ["unvalidated"]
        elif corruption == "record_suppressed":
            record["flags"] = ["suppressed_policy"]
        elif corruption == "output_suppressed":
            record["output"]["flags"] = ["suppressed_policy"]
        elif corruption == "source_trace":
            record["source"]["trace_id"] = "f" * 32
        elif corruption == "duplicate_source":
            record["source"] = records[0]["source"]
        elif corruption == "deployment_version":
            record["deployment_version"] = "ghp_" + "Ab3d" * 9
        else:
            record = records[0]
            projection = json.loads(record["output"]["text"])
            if corruption == "unknown_projection":
                projection = {"raw_error": "SYNTHETIC-RAW-ERROR"}
            elif corruption == "missing_projection":
                del projection["name"]
            elif corruption == "wrong_projection_type":
                projection["attack"] = True
            elif corruption == "invalid_projection_enum":
                projection["rarity"] = "SYNTHETIC-RAW-ERROR"
            elif corruption == "escaped_private":
                projection["name"] = "user@example.test"
            elif corruption == "escaped_policy":
                projection["name"] = "graphic gore"
            record["output"]["text"] = json.dumps(projection)
            if corruption.startswith("escaped"):
                name = projection["name"]
                record["output"]["text"] = record["output"]["text"].replace(
                    name, "".join(f"\\u{ord(c):04x}" for c in name)
                )
            if corruption == "truncated_projection":
                record["output"]["text"] = '{"raw_error":"SYNTHETIC-RAW-ERROR'
                record["output"]["flags"] = ["truncated"]
            elif corruption == "duplicate_projection_key":
                record["output"]["text"] = (
                    '{"name":"SYNTHETIC-RAW-ERROR",' + record["output"]["text"][1:]
                )
        part["text"] = json.dumps(payload)
        return httpx.Response(200, json=body, request=request)

    transport.handle_async_request = tamper
    response = _generate(_client(monkeypatch, services))
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert not content(exported)
    converted = json.dumps(
        [_convert_span_to_envelope(s).as_dict() for s in exported.get_finished_spans()]
    )
    assert canary not in converted + caplog.text + response.text
    assert "SYNTHETIC-RAW-ERROR" not in converted + caplog.text + response.text
    assert "agent.detail.omitted" in caplog.text
    assert all(r.exc_info is None for r in caplog.records)


def test_oversized_projection_has_no_partial_json_or_full_validation_claim(monkeypatch, exported):
    large = CARD | {
        "name": "é" * 80,
        "rulesText": "é" * 400,
        "flavorText": "é" * 280,
        "artBrief": "é" * 300,
    }
    services, _, transport, _ = stack(monkeypatch, outputs=[large, LORE, ART])
    response = _generate(_client(monkeypatch, services))
    assert response.json()["status"] == "completed"
    records = content(exported)
    assert len(records) == 5
    concept = next(r for r in records if r["stage"] == "concept")
    assert concept["output"] == {
        "text": "",
        "flags": ["omitted_budget", "truncated"],
    }
    assert concept["validation"] == "modified"
    assert "agentDetail" not in json.dumps(transport.responses)
    converted = [_convert_span_to_envelope(s).as_dict() for s in new_spans(exported)]
    assert converted
    for record_value in records:
        detail.Record.model_validate_json(detail.encoded(record_value), strict=True)
        for field in ("input", "output"):
            view = record_value[field]
            if "truncated" in view["flags"]:
                assert view["text"] == ""
                assert record_value["validation"] == "modified"


def test_model_output_cannot_supply_carrier_or_source_context(monkeypatch, exported):
    model_output = CARD | {"metadata": {"agentDetail": {"source": {"trace_id": "f" * 32}}}}
    services, _, transport, _ = stack(monkeypatch, outputs=[model_output, LORE, ART])
    response = _generate(_client(monkeypatch, services))
    assert response.status_code != 200
    assert not content(exported)
    assert "agentDetail" not in json.dumps(transport.responses)
    assert "f" * 32 not in json.dumps(
        [_convert_span_to_envelope(s).as_dict() for s in exported.get_finished_spans()]
    )


@pytest.mark.parametrize("field", ["instruction", "input", "output"])
def test_export_processor_revalidates_each_view(exported, caplog, field):
    value = record("hosted").model_dump(mode="json")
    value[field] = {"text": "Cookie: SYNTHETIC-EXPORT-CANARY", "flags": []}
    with detail._tracer().start_as_current_span("fcg.agent.detail") as span:
        span.set_attribute("fcg.detail.record", detail.encoded(value).decode())
        span.add_event("unsafe", {"payload": "Cookie: SYNTHETIC-EXPORT-CANARY"})
    assert not content(exported)
    converted = json.dumps(
        [_convert_span_to_envelope(s).as_dict() for s in exported.get_finished_spans()]
    )
    assert "SYNTHETIC-EXPORT-CANARY" not in converted + caplog.text
    assert "invalid_export" in caplog.text


@pytest.mark.parametrize("field", ["name", "flavorText", "artBrief"])
def test_output_credentials_withheld_before_hosted_transport(monkeypatch, exported, caplog, field):
    canary = "ghp_" + "Cd4e" * 9
    services, _, transport, _ = stack(monkeypatch, outputs=[CARD | {field: canary}, LORE, ART])
    response = _generate(_client(monkeypatch, services))
    assert response.json()["status"] == "completed"
    assert len(content(exported)) == 5
    converted = json.dumps(
        [_convert_span_to_envelope(s).as_dict() for s in exported.get_finished_spans()]
    )
    assert canary not in converted + json.dumps(transport.responses) + caplog.text
    concept = next(r for r in content(exported) if r["stage"] == "concept")
    assert concept["output"]["flags"] == ["redacted"]
    assert concept["validation"] == "modified"
    assert json.loads(concept["output"]["text"])[field] == ""


PRIVATE_LABELS = [
    "refresh_token",
    "access_token",
    "session_token",
    "id_token",
    "csrf_token",
    "credential",
    "client_secret",
    "password",
    "api_key",
    "private_key",
    "connection_string",
    "cookie",
    "set_cookie",
    "authorization",
    "proxy_authorization",
    "request_header",
    "blob_id",
    "card_id",
    "email_id",
    "user_id",
    "tenant_id",
    "session_id",
    "business_identifier",
    "customer_id",
    "idempotency_key",
    "client_ip",
    "ip_address",
]


@pytest.mark.parametrize("label", PRIVATE_LABELS)
@pytest.mark.parametrize("separator", ["_", "-", " "])
@pytest.mark.parametrize("location", ["query", "output"])
def test_private_label_categories_withheld_before_transport_and_export(
    monkeypatch, exported, caplog, label, separator, location
):
    label = label.replace("_", separator).swapcase()
    canary = f"{label}=SYNTHETIC-PRIVATE-VALUE"
    lore = LORE | {"flavorText": canary} if location == "output" else LORE
    services, _, transport, calls = stack(monkeypatch, outputs=[CARD, lore, ART])
    prompt = f"A mountain guardian {canary}" if location == "query" else PROMPT
    result = asyncio.run(generate(CardGenerationService(services), prompt=prompt))
    assert result.status == "completed"
    assert calls[0][1]["query"] == prompt
    records = content(exported)
    assert len(records) == 5
    hosted = json.loads(transport.responses[0]["output"][0]["content"][0]["text"])
    assert "agentDetail" not in hosted["metadata"]
    converted = json.dumps(
        [_convert_span_to_envelope(s).as_dict() for s in exported.get_finished_spans()]
    )
    assert "SYNTHETIC-PRIVATE-VALUE" not in converted + caplog.text
    affected = (
        [r for r in records if r["stage"] in {"concept", "hosted_invocation"}]
        if location == "query"
        else [r for r in records if r["stage"] == "lore"]
    )
    field = "input" if location == "query" else "output"
    for value in affected:
        assert value[field]["flags"] == ["redacted"]
        assert value["validation"] == "modified"
    if location == "output":
        assert hosted["card"]["flavorText"] == canary


@pytest.mark.parametrize(
    "label",
    PRIVATE_LABELS + ["refreshToken", "blobId", "SetCookie", "requestHeader"],
)
@pytest.mark.parametrize("boundary", ["import", "export"])
def test_escaped_private_labels_rejected_at_strict_carrier_boundaries(
    monkeypatch, exported, caplog, label, boundary
):
    canary = f"{label}=SYNTHETIC-PRIVATE-VALUE"

    def corrupt(value):
        projection = json.loads(value["output"]["text"])
        projection["flavorText"] = canary
        value["output"]["text"] = json.dumps(projection).replace(
            canary, "".join(f"\\u{ord(c):04x}" for c in canary)
        )
        assert value["validation"] == "validated"

    if boundary == "export":
        value = record("hosted", stage="lore").model_dump(mode="json")
        corrupt(value)
        with detail._tracer().start_as_current_span("fcg.agent.detail") as span:
            span.set_attribute("fcg.detail.record", detail.encoded(value).decode())
    else:
        services, _, transport, _ = stack(monkeypatch, legacy=True)
        original = transport.handle_async_request

        async def tamper(request):
            response = await original(request)
            body = response.json()
            part = body["output"][0]["content"][0]
            payload = json.loads(part["text"])
            corrupt(payload["metadata"]["agentDetail"]["records"][1])
            part["text"] = json.dumps(payload)
            return httpx.Response(200, json=body, request=request)

        transport.handle_async_request = tamper
        response = _generate(_client(monkeypatch, services))
        assert response.json()["status"] == "completed"
    assert not content(exported)
    converted = json.dumps(
        [_convert_span_to_envelope(s).as_dict() for s in exported.get_finished_spans()]
    )
    assert "SYNTHETIC-PRIVATE-VALUE" not in converted + caplog.text
    assert "agent.detail.omitted" in caplog.text


def test_cross_instance_artwork_retry_loser_discards_candidate(monkeypatch, exported):
    services, _, _, calls = stack(monkeypatch)
    original_image = services.ai_client.generate_image
    original_reserve = services.audit_repository.reserve_audit
    reservations = []
    image_calls = []

    async def fail(*args, **kwargs):
        raise UpstreamServiceError("foundry-image", "unavailable", retryable=False)

    async def reserve(**kwargs):
        reservation, created = await original_reserve(**kwargs)
        reservations.append(created)
        return reservation, created

    async def scenario():
        services.ai_client.generate_image = fail
        partial = await generate(CardGenerationService(services))
        assert partial.status == "awaiting_artwork_retry"
        assert len(content(exported)) == 3
        barrier = asyncio.Barrier(2)

        async def image(*args, **kwargs):
            image_calls.append(kwargs["request_id"])
            await barrier.wait()
            return await original_image(*args, **kwargs)

        services.ai_client.generate_image = image
        services.audit_repository.reserve_audit = reserve
        results = await asyncio.gather(
            *(
                CardGenerationService(services).retry_artwork(
                    owner=OWNER,
                    card_id=partial.cardId,
                    idempotency_key="same-retry",
                    request_id=f"retry-{i}",
                    client_ip="127.0.0.1",
                )
                for i in range(2)
            )
        )
        assert results[0] == results[1]
        assert results[0].status == "completed"
        before = len(new_spans(exported))
        assert await generate(CardGenerationService(services)) == results[0]
        assert len(new_spans(exported)) == before

    asyncio.run(scenario())
    assert sorted(reservations) == [False, True]
    assert len(image_calls) == 2
    assert len(calls) == 3
    attempts = [
        s
        for s in new_spans(exported)
        if s.name == "fcg.agent.image" and s.attributes["fcg.detail.operation"] == "artwork_retry"
    ]
    assert len({s.context.span_id for s in attempts}) == 2
    records = [r for r in content(exported) if r["source_runtime"] == "web"]
    assert len(records) == 1
    assert records[0]["operation"] == "artwork_retry"
    assert records[0]["stage"] == "image"
    converted = [_convert_span_to_envelope(s).as_dict() for s in new_spans(exported)]
    assert json.dumps(converted).count("fcg.detail.record") == 4


@pytest.mark.parametrize(
    "failure,outcome,reason,http_status",
    [
        ("refused", "refused", "model_refusal", 422),
        ("held", "held", "stage_incomplete", 502),
        ("routing_defer", "routing_defer", "stage_incomplete", 503),
        ("timeout", "failed", "timeout", 504),
        ("authentication", "failed", "authentication", 503),
        ("authorization", "failed", "authorization", 503),
        ("runtime_authentication", "failed", "authentication", 503),
        ("runtime_authorization", "failed", "authorization", 503),
        ("schema", "failed", "schema_invalid", 502),
    ],
)
def test_web_invocation_preserves_typed_non_success_through_export(
    monkeypatch, exported, caplog, failure, outcome, reason, http_status
):
    services, runtime, transport, _ = stack(monkeypatch)
    original_run = StageBoundary.invoke
    original_transport = transport.handle_async_request

    async def run(self, context, call_next):
        if self.stage == "lore":
            if failure in {"held", "routing_defer"}:
                return specialists.SpecialistResult(failure, reason="model_incomplete")
            if failure == "timeout":
                raise TimeoutError("SYNTHETIC-PRIVATE-ERROR")
            if failure in {"runtime_authentication", "runtime_authorization"}:
                raise APIStatusError(
                    "SYNTHETIC-PRIVATE-ERROR",
                    response=httpx.Response(
                        401 if failure == "runtime_authentication" else 403,
                        request=httpx.Request("POST", "https://offline.test"),
                    ),
                    body={"message": "SYNTHETIC-PRIVATE-ERROR"},
                )
        return await original_run(self, context, call_next)

    monkeypatch.setattr(StageBoundary, "invoke", run)
    if failure == "refused":
        from agent_framework import Agent

        from tests.test_card_orchestrator_models import model_transport

        calls, responses, *_ = model_transport.__wrapped__(monkeypatch)
        responses[1] = httpx.Response(
            400, json={"error": {"code": "content_filter", "message": "SYNTHETIC-PRIVATE-ERROR"}}
        )
        monkeypatch.setattr(specialists, "Agent", Agent)
        runtime.specialist_factory = specialists.create_specialists

    async def transport_failure(request):
        if failure in {"authentication", "authorization"}:
            status = 401 if failure == "authentication" else 403
            return httpx.Response(
                status,
                json={"error": {"message": "SYNTHETIC-PRIVATE-ERROR"}},
                request=request,
            )
        response = await original_transport(request)
        if failure == "schema":
            body = response.json()
            body["output"][0]["content"][0]["text"] = '{"unexpected":"SYNTHETIC-PRIVATE-ERROR"}'
            return httpx.Response(200, json=body, request=request)
        return response

    transport.handle_async_request = transport_failure
    response = _generate(_client(monkeypatch, services))
    if failure == "refused":
        assert len(calls) == 2
    assert response.status_code == http_status
    if failure == "refused":
        assert response.json()["errorCode"] == "prompt_rejected"
        lore = next(s for s in new_spans(exported) if s.name == "card_lore")
        assert lore.attributes["fcg.detail.result"] == "refused"
    assert len(content(exported)) == (3 if failure == "schema" else 0)
    invocation = next(s for s in new_spans(exported) if s.name == "fcg.agent.invoke")
    assert invocation.attributes["fcg.detail.result"] == outcome
    assert invocation.attributes["fcg.detail.reason"] == reason
    converted = _convert_span_to_envelope(invocation).as_dict()
    properties = converted["data"]["baseData"]["properties"]
    assert properties["fcg.detail.result"] == outcome
    assert properties["fcg.detail.reason"] == reason
    assert "SYNTHETIC-PRIVATE-ERROR" not in json.dumps(converted) + caplog.text


@pytest.mark.parametrize(
    "status,error,runtime_reason",
    [
        ("SYNTHETIC-PRIVATE-STATUS", "SYNTHETIC-PRIVATE-ERROR", None),
        ("failed", "SYNTHETIC-PRIVATE-ERROR", "SYNTHETIC-PRIVATE-REASON"),
    ],
)
def test_invocation_outcomes_never_echo_arbitrary_result_values(
    monkeypatch, exported, status, error, runtime_reason
):
    services, _, _, _ = stack(monkeypatch)

    async def invoke(query):
        return FoundryAgentInvocationResult(
            status=status, error_code=error, runtime_failure_reason=runtime_reason
        )

    services.agent_client.invoke = invoke
    response = _generate(_client(monkeypatch, services))
    assert response.status_code == 502
    assert not content(exported)
    invocation = next(s for s in new_spans(exported) if s.name == "fcg.agent.invoke")
    assert invocation.attributes["fcg.detail.result"] == "failed"
    assert invocation.attributes["fcg.detail.reason"] == "dependency_error"
    converted = json.dumps(_convert_span_to_envelope(invocation).as_dict())
    assert "SYNTHETIC-PRIVATE" not in converted


@pytest.mark.parametrize("legacy", [False, True])
def test_response_send_failure_discards_pending_web_content(monkeypatch, exported, legacy):
    services, _, _, _ = stack(monkeypatch, legacy=legacy)

    async def app(scope, receive, send):
        await generate(CardGenerationService(services))
        assert len(content(exported)) == (0 if legacy else 3)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"complete"})

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        if message["type"] == "http.response.body":
            raise asyncio.CancelledError

    middleware = detail.DetailReleaseMiddleware(app)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(middleware({"type": "http"}, receive, send))
    assert len(content(exported)) == (0 if legacy else 3)
    with capture():
        pass


def test_uninspectable_values_and_bounded_traversal_do_not_stringify():
    class Uninspectable:
        def __str__(self):
            raise AssertionError("must not stringify arbitrary values")

    assert detail.projection_view({"query": Uninspectable()}, fields={"query"}).flags == (
        "unvalidated",
    )
    assert not detail._bounded_tree({"records": [Uninspectable()]})
    assert not detail._bounded_tree({"records": [[[[[[[[[[1]]]]]]]]]]})
    assert detail.projection_view({"image": b"PRIVATE"}, fields={"query"}).flags == ("unvalidated",)


@pytest.mark.parametrize("render_failure", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
def test_html_render_is_part_of_web_boundary(monkeypatch, exported, render_failure, legacy):
    from starlette.templating import Jinja2Templates

    services, _, _, _ = stack(monkeypatch, legacy=legacy)
    client = _client(monkeypatch, services)
    csrf = extract_hidden_value(client.get("/app").text, "csrf_token")
    original = Jinja2Templates.TemplateResponse

    def render(self, *args, **kwargs):
        if "partials/card_result.html" in args:
            assert len(content(exported)) == (0 if legacy else 3)
            if render_failure:
                raise RuntimeError("render failed")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Jinja2Templates, "TemplateResponse", render)
    if render_failure:
        with pytest.raises(RuntimeError):
            client.post(
                "/ui/cards/generate",
                data={
                    "prompt": PROMPT,
                    "idempotency_key": "html-response-test",
                    "csrf_token": csrf,
                },
            )
        assert len(content(exported)) == (0 if legacy else 3)
    else:
        response = client.post(
            "/ui/cards/generate",
            data={
                "prompt": PROMPT,
                "idempotency_key": "html-response-test",
                "csrf_token": csrf,
            },
        )
        assert response.status_code == 200
        assert "agentDetail" not in response.text
        assert "capture_version" not in response.text
        assert len(content(exported)) == 5
