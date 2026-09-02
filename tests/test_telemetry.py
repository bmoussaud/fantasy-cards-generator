from __future__ import annotations

import ast
import logging
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry.propagate import extract
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import telemetry
from app.generation import UsageAudit
from app.settings import SettingsError, TelemetrySettings, load_telemetry_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
SENSITIVE_SENTINEL = "NEVER-EXPORT-user@example.com-secret-token"


class CapturingInstrument:
    def __init__(self) -> None:
        self.measurements: list[tuple[float, dict[str, Any]]] = []

    def add(self, value: int, *, attributes: dict[str, Any]) -> None:
        self.measurements.append((value, attributes))

    def record(self, value: float, *, attributes: dict[str, Any]) -> None:
        self.measurements.append((value, attributes))


@pytest.fixture
def enabled_telemetry(monkeypatch: pytest.MonkeyPatch) -> dict[str, CapturingInstrument]:
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "_enabled", True)
    monkeypatch.setattr(telemetry, "_tracer", provider.get_tracer("telemetry-tests"))

    instruments = {
        name: CapturingInstrument()
        for name in (
            "_generation_counter",
            "_generation_duration",
            "_partial_counter",
            "_artwork_retry_counter",
            "_dependency_counter",
            "_dependency_duration",
            "_dependency_throttle_counter",
            "_dependency_timeout_counter",
            "_moderation_counter",
            "_persistence_counter",
            "_token_counter",
        )
    }
    for name, instrument in instruments.items():
        monkeypatch.setattr(telemetry, name, instrument)
    instruments["exporter"] = exporter  # type: ignore[assignment]
    return instruments


def telemetry_settings(**changes: Any) -> TelemetrySettings:
    defaults = TelemetrySettings(
        enabled=False,
        connection_string=None,
        sampling_ratio=0.2,
        service_name="fantasy-cards-generator",
        environment="test",
        service_version=None,
        container_revision=None,
        container_replica=None,
    )
    return replace(defaults, **changes)


@pytest.mark.parametrize(
    "settings",
    [
        telemetry_settings(enabled=False, connection_string="InstrumentationKey=fake"),
        telemetry_settings(enabled=True, connection_string=None),
    ],
)
def test_disabled_or_unconfigured_telemetry_is_network_free(
    monkeypatch: pytest.MonkeyPatch,
    settings: TelemetrySettings,
) -> None:
    monkeypatch.setattr(telemetry, "_configured", False)
    monkeypatch.setattr(telemetry, "_enabled", False)

    def fail_if_configured(**_: Any) -> None:
        pytest.fail("Azure Monitor configuration must not run without explicit complete opt-in")

    import azure.monitor.opentelemetry

    monkeypatch.setattr(
        azure.monitor.opentelemetry,
        "configure_azure_monitor",
        fail_if_configured,
    )

    assert telemetry.configure_telemetry(settings) is False
    assert telemetry.telemetry_enabled() is False


def test_disabled_telemetry_decorator_preserves_application_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telemetry, "_enabled", False)
    monkeypatch.setattr(telemetry, "_tracer", None)

    @telemetry.instrument_generation("generate")
    async def generate(*, request_id: str) -> str:
        return f"completed:{request_id}"

    import asyncio

    assert asyncio.run(generate(request_id="request-42")) == "completed:request-42"


def test_telemetry_settings_default_to_disabled_without_connection_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TELEMETRY_ENABLED", raising=False)
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)

    settings = load_telemetry_settings()

    assert settings.enabled is False
    assert settings.connection_string is None
    assert settings.sampling_ratio == 1.0


def test_production_startup_fails_open_for_malformed_telemetry_configuration(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import runpy

    from fastapi.testclient import TestClient

    malformed_value = f"invalid-{SENSITIVE_SENTINEL}"
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("TELEMETRY_ENABLED", "true")
    monkeypatch.setenv(
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=00000000-0000-0000-0000-000000000000",
    )
    monkeypatch.setenv("TELEMETRY_SAMPLING_RATIO", malformed_value)
    monkeypatch.setattr(telemetry, "_configured", False)
    monkeypatch.setattr(telemetry, "_enabled", False)
    monkeypatch.setattr(telemetry, "_tracer", None)

    import azure.monitor.opentelemetry

    def fail_if_configured(**_: Any) -> None:
        pytest.fail("Malformed settings must not initialize an exporter")

    monkeypatch.setattr(
        azure.monitor.opentelemetry,
        "configure_azure_monitor",
        fail_if_configured,
    )

    with caplog.at_level(logging.WARNING, logger=telemetry.LOGGER_NAME):
        namespace = runpy.run_path(str(REPO_ROOT / "app" / "entrypoint.py"))

    assert telemetry.telemetry_enabled() is False
    telemetry_messages = [
        record.getMessage() for record in caplog.records if record.name == telemetry.LOGGER_NAME
    ]
    assert telemetry_messages == ["telemetry.configuration_failed"]
    assert malformed_value not in caplog.text
    with TestClient(namespace["app"], base_url="https://testserver") as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "status": "ok",
        "dependencies": {
            "cosmos": {
                "status": "not_applicable",
                "durationMs": 0,
                "errorCategory": "none",
            },
            "blob": {
                "status": "not_applicable",
                "durationMs": 0,
                "errorCategory": "none",
            },
        },
    }


def test_configured_telemetry_passes_bounded_resource_and_sampling_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    import azure.monitor.opentelemetry

    monkeypatch.setattr(telemetry, "_configured", False)
    monkeypatch.setattr(telemetry, "_enabled", False)
    monkeypatch.setattr(
        azure.monitor.opentelemetry,
        "configure_azure_monitor",
        lambda **kwargs: captured.update(kwargs),
    )
    httpx_instrumented = []
    monkeypatch.setattr(telemetry, "_instrument_httpx", lambda: httpx_instrumented.append(True))
    monkeypatch.setattr(telemetry, "_initialize_metrics", lambda: None)
    settings = telemetry_settings(
        enabled=True,
        connection_string="InstrumentationKey=00000000-0000-0000-0000-000000000000",
        sampling_ratio=0.25,
        environment="production",
        service_version="release-42",
        container_revision="revision-7",
        container_replica="replica-2",
    )

    assert telemetry.configure_telemetry(settings) is True
    assert captured["connection_string"] == settings.connection_string
    assert "sampling_ratio" not in captured
    assert os.environ["OTEL_TRACES_SAMPLER"] == "parentbased_trace_id_ratio"
    assert os.environ["OTEL_TRACES_SAMPLER_ARG"] == "0.25"
    assert captured["enable_live_metrics"] is False
    resource = captured["resource"].attributes
    assert resource["service.name"] == "fantasy-cards-generator"
    assert "service.namespace" not in resource
    assert resource["deployment.environment.name"] == "production"
    assert resource["cloud.platform"] == "azure_container_apps"
    assert resource["service.version"] == "release-42"
    assert resource["service.instance.revision"] == "revision-7"
    assert resource["service.instance.id"] == "replica-2"
    assert captured["span_processors"]
    assert httpx_instrumented == [True]
    from azure.monitor.opentelemetry.exporter import _utils

    part_a = _utils._populate_part_a_fields(captured["resource"])
    assert part_a["ai.cloud.role"] == "fantasy-cards-generator"


def test_locked_distro_sampler_respects_remote_parent_sampling_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from azure.monitor.opentelemetry._configure import _get_configurations
    from azure.monitor.opentelemetry._utils.configurations import _get_sampler_from_name
    from opentelemetry.sdk.trace.sampling import Decision
    from opentelemetry.trace import (
        NonRecordingSpan,
        SpanContext,
        TraceFlags,
        TraceState,
        set_span_in_context,
    )

    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "parentbased_trace_id_ratio")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "0.000001")
    configurations = _get_configurations(
        connection_string="InstrumentationKey=00000000-0000-0000-0000-000000000000"
    )
    assert configurations["sampler_type"] == "parentbased_trace_id_ratio"
    sampler = _get_sampler_from_name(
        configurations["sampler_type"],
        configurations["sampling_arg"],
    )
    decisions = {}
    for sampled in (False, True):
        parent = SpanContext(
            trace_id=1,
            span_id=2,
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED if sampled else 0),
            trace_state=TraceState(),
        )
        context = set_span_in_context(NonRecordingSpan(parent))
        decisions[sampled] = sampler.should_sample(context, 3, "child").decision

    assert decisions[False] is Decision.DROP
    assert decisions[True] is Decision.RECORD_AND_SAMPLE


def test_privacy_processor_sanitizes_real_sdk_span_before_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from azure.monitor.opentelemetry.exporter.export.trace._exporter import (
        _convert_span_events_to_envelopes,
    )
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON
    from opentelemetry.trace import SpanKind, Status, StatusCode

    class SensitiveTimeout(RuntimeError):
        pass

    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(telemetry.PrivacySpanProcessor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("privacy-integration-test")

    with tracer.start_as_current_span(
        f"GET /cards/{SENSITIVE_SENTINEL}/image",
        kind=SpanKind.SERVER,
        attributes={
            "http.request.method": "GET",
            "http.route": f"/cards/{SENSITIVE_SENTINEL}/image",
            "url.full": f"https://example.test/?token={SENSITIVE_SENTINEL}",
        },
    ) as span:
        span.add_event(
            "generation.failed",
            {"fcg.error_code": "secret-code", "prompt": SENSITIVE_SENTINEL},
        )
        span.record_exception(SensitiveTimeout(SENSITIVE_SENTINEL))
        span.set_status(Status(StatusCode.ERROR, SENSITIVE_SENTINEL))

    finished = exporter.get_finished_spans()
    assert len(finished) == 1
    exported_span = finished[0]
    assert exported_span.name == "GET unknown"
    assert exported_span.attributes == {
        "http.request.method": "get",
        "http.route": "unknown",
    }
    assert exported_span.status.description is None
    assert [event.name for event in exported_span.events] == [
        "generation.failed",
        "exception",
    ]
    assert exported_span.events[1].attributes == {"exception.type": "TimeoutError"}
    exception_envelopes = [
        envelope
        for envelope in _convert_span_events_to_envelopes(exported_span)
        if envelope.name.endswith("Exception")
    ]
    assert len(exception_envelopes) == 1
    assert SENSITIVE_SENTINEL not in repr(exported_span)
    assert SENSITIVE_SENTINEL not in repr(exception_envelopes)


@pytest.mark.parametrize("sampling_ratio", ["0", "-0.1", "1.1"])
def test_sampling_ratio_must_be_greater_than_zero_and_at_most_one(
    monkeypatch: pytest.MonkeyPatch,
    sampling_ratio: str,
) -> None:
    monkeypatch.setenv("TELEMETRY_SAMPLING_RATIO", sampling_ratio)

    with pytest.raises(SettingsError, match="TELEMETRY_SAMPLING_RATIO"):
        load_telemetry_settings()


def test_entrypoint_configures_telemetry_before_importing_application() -> None:
    source = (REPO_ROOT / "app" / "entrypoint.py").read_text()
    tree = ast.parse(source)

    configure_line = next(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "configure_telemetry"
    )
    app_import_line = next(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "app.main"
    )

    assert configure_line < app_import_line
    assert "fastapi" not in source
    assert "httpx" not in source
    assert "azure.cosmos" not in source
    assert "azure.storage" not in source


@pytest.mark.parametrize(
    "request_id",
    [None, "", "contains spaces", "non-ascii-\N{SNOWMAN}", "x" * 65],
)
def test_request_id_validation_rejects_unbounded_or_non_ascii_values(
    request_id: str | None,
) -> None:
    assert telemetry.valid_request_id(request_id) is False


def test_w3c_parent_is_shared_by_request_generation_and_dependency_spans(
    enabled_telemetry: dict[str, CapturingInstrument],
) -> None:
    trace_id = "1234567890abcdef1234567890abcdef"
    context = extract(
        {
            "traceparent": f"00-{trace_id}-1234567890abcdef-01",
        }
    )
    tracer = telemetry._tracer

    @telemetry.instrument_generation("generate")
    async def generate(*, request_id: str) -> SimpleNamespace:
        with telemetry.telemetry_span(
            "fcg.dependency",
            request_id=request_id,
            attributes={"fcg.dependency": "foundry_text"},
        ):
            pass
        return SimpleNamespace(status="completed")

    import asyncio

    with tracer.start_as_current_span("GET /api/v1/cards/generate", context=context):
        telemetry.enrich_request_span(
            request_id="safe-request-42",
            route="/api/v1/cards/generate",
            status_code=200,
        )
        asyncio.run(generate(request_id="safe-request-42"))

    spans = enabled_telemetry["exporter"].get_finished_spans()  # type: ignore[attr-defined]
    relevant = [
        span for span in spans if span.name.startswith("fcg.") or span.name.startswith("GET ")
    ]
    assert {f"{span.context.trace_id:032x}" for span in relevant} == {trace_id}
    assert any(span.attributes.get("app.request_id") == "safe-request-42" for span in relevant)


def test_httpx_dependency_propagates_w3c_context_without_exporting_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    import httpx
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON

    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    trace_id = "1234567890abcdef1234567890abcdef"
    received_headers: dict[str, str] = {}
    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(telemetry.PrivacySpanProcessor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    async def handler(
        _transport: httpx.AsyncHTTPTransport,
        inbound: httpx.Request,
    ) -> httpx.Response:
        received_headers.update(inbound.headers)
        return httpx.Response(200, request=inbound)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handler)
    instrumentor = HTTPXClientInstrumentor()
    instrumentor.instrument(tracer_provider=provider)

    async def request() -> None:
        context = extract({"traceparent": f"00-{trace_id}-1234567890abcdef-01"})
        tracer = provider.get_tracer("httpx-propagation-test")
        with tracer.start_as_current_span("request", context=context):
            async with httpx.AsyncClient() as client:
                await client.get(f"https://example.test/private?token={SENSITIVE_SENTINEL}")

    try:
        asyncio.run(request())
    finally:
        instrumentor.uninstrument()

    assert received_headers["traceparent"].startswith(f"00-{trace_id}-")
    dependency = next(span for span in exporter.get_finished_spans() if span.kind.name == "CLIENT")
    assert f"{dependency.context.trace_id:032x}" == trace_id
    assert SENSITIVE_SENTINEL not in repr(dependency)
    assert "url.full" not in dependency.attributes


def test_lifecycle_and_operational_signals_emit_bounded_events_and_metrics(
    enabled_telemetry: dict[str, CapturingInstrument],
) -> None:
    tracer = telemetry._tracer
    with tracer.start_as_current_span("request"):
        telemetry.record_dependency_attempt(
            dependency="foundry_text",
            attempt=1,
            outcome="throttled",
            duration_ms=12.5,
            request_id="request-42",
            error_code="arbitrary-upstream-code",
            retryable=True,
        )
        telemetry.record_retry(dependency="foundry_text", attempt=2, request_id="request-42")
        telemetry.record_dependency_attempt(
            dependency="foundry_image",
            attempt=3,
            outcome="timed_out",
            duration_ms=201,
            request_id="request-42",
            error_code="TimeoutError",
            retryable=True,
        )
        telemetry.record_partial("image_timeout")
        telemetry.record_moderation(
            stage="pre_prompt",
            allowed=False,
            reason="living-artist-imitation",
            policy="conservative-v1",
        )
        telemetry.record_persistence(
            store="cosmos",
            operation="save_completed",
            outcome="failed",
            request_id="request-42",
            error_code="secret-database-error",
        )
        telemetry.record_token_usage(
            "text",
            UsageAudit(inputTokens=11, outputTokens=17, totalTokens=28, latencyMs=5),
        )

    spans = enabled_telemetry["exporter"].get_finished_spans()  # type: ignore[attr-defined]
    event_names = {event.name for span in spans for event in span.events}
    assert {
        "dependency.failed",
        "dependency.retry",
        "dependency.timeout",
        "generation.partial",
        "moderation.decision",
        "persistence.failed",
    } <= event_names
    assert enabled_telemetry["_dependency_throttle_counter"].measurements
    assert enabled_telemetry["_dependency_timeout_counter"].measurements
    assert enabled_telemetry["_partial_counter"].measurements
    assert enabled_telemetry["_moderation_counter"].measurements
    assert enabled_telemetry["_persistence_counter"].measurements
    assert enabled_telemetry["_token_counter"].measurements == [
        (11, {"fcg.operation": "text", "fcg.token_type": "input"}),
        (17, {"fcg.operation": "text", "fcg.token_type": "output"}),
        (28, {"fcg.operation": "text", "fcg.token_type": "total"}),
    ]


def test_failed_lifecycle_emits_normalized_failure_without_exception_message(
    enabled_telemetry: dict[str, CapturingInstrument],
) -> None:
    class SensitiveTimeout(RuntimeError):
        error_code = "upstream_timeout"

    @telemetry.instrument_generation("generate")
    async def generate(*, request_id: str) -> None:
        raise SensitiveTimeout(SENSITIVE_SENTINEL)

    import asyncio

    with pytest.raises(SensitiveTimeout):
        asyncio.run(generate(request_id="request-42"))

    spans = enabled_telemetry["exporter"].get_finished_spans()  # type: ignore[attr-defined]
    generation_span = next(span for span in spans if span.name == "fcg.generation")
    assert generation_span.attributes["fcg.outcome"] == "timed_out"
    assert generation_span.attributes["fcg.error_code"] == "upstream_timeout"
    assert [event.name for event in generation_span.events] == [
        "generation.started",
        "generation.failed",
    ]
    assert SENSITIVE_SENTINEL not in repr(generation_span)


def test_sensitive_values_are_removed_from_spans_events_logs_and_metrics(
    enabled_telemetry: dict[str, CapturingInstrument],
    caplog: pytest.LogCaptureFixture,
) -> None:
    event = SimpleNamespace(
        name="generation.failed",
        _attributes={
            "prompt": SENSITIVE_SENTINEL,
            "fcg.error_code": SENSITIVE_SENTINEL,
            "app.request_id": SENSITIVE_SENTINEL,
        },
    )
    exception_event = SimpleNamespace(
        name="exception",
        _attributes={"exception.message": SENSITIVE_SENTINEL},
    )
    span = SimpleNamespace(
        _attributes={
            "url.full": f"https://example.test/callback?code={SENSITIVE_SENTINEL}",
            "http.route": f"/cards/{SENSITIVE_SENTINEL}/image",
            "authorization": SENSITIVE_SENTINEL,
            "fcg.error_code": SENSITIVE_SENTINEL,
        },
        _events=[event, exception_event],
        status=SimpleNamespace(description=None),
    )

    telemetry.PrivacySpanProcessor()._on_ending(span)
    with caplog.at_level(logging.INFO, logger=telemetry.LOGGER_NAME):
        telemetry.safe_log(
            "generation.failed",
            request_id=SENSITIVE_SENTINEL,
            attributes={
                "prompt": SENSITIVE_SENTINEL,
                "fcg.error_code": SENSITIVE_SENTINEL,
            },
        )
    telemetry.record_persistence(
        store=SENSITIVE_SENTINEL,
        operation=SENSITIVE_SENTINEL,
        outcome=SENSITIVE_SENTINEL,
        request_id=SENSITIVE_SENTINEL,
        error_code=SENSITIVE_SENTINEL,
    )

    exported = (
        repr(span) + caplog.text + repr(enabled_telemetry["_persistence_counter"].measurements)
    )
    assert SENSITIVE_SENTINEL not in exported
    assert span._attributes == {
        "http.route": "unknown",
        "fcg.error_code": "internal_error",
    }
    assert [kept.name for kept in span._events] == ["generation.failed", "exception"]
    assert span._events[1].attributes == {"exception.type": "Exception"}


def test_attribute_and_metric_dimensions_are_allowlisted_and_bounded(
    enabled_telemetry: dict[str, CapturingInstrument],
) -> None:
    attributes = telemetry.safe_attributes(
        {
            "app.request_id": "request-42",
            "fcg.operation": SENSITIVE_SENTINEL,
            "fcg.outcome": SENSITIVE_SENTINEL,
            "fcg.stage": SENSITIVE_SENTINEL,
            "fcg.error_code": SENSITIVE_SENTINEL,
            "prompt": SENSITIVE_SENTINEL,
            "user.email": SENSITIVE_SENTINEL,
            "card.id": SENSITIVE_SENTINEL,
        }
    )

    assert attributes == {
        "app.request_id": "request-42",
        "fcg.operation": "generate",
        "fcg.outcome": "failed",
        "fcg.stage": "reserved",
        "fcg.error_code": "internal_error",
    }

    telemetry.record_dependency_attempt(
        dependency="foundry_text",
        attempt=999,
        outcome="failed",
        duration_ms=-1,
        request_id="request-42",
    )
    measurements = [
        measurement
        for instrument in enabled_telemetry.values()
        if isinstance(instrument, CapturingInstrument)
        for measurement in instrument.measurements
    ]
    assert measurements
    assert all("app.request_id" not in dimensions for _, dimensions in measurements)
    assert all(SENSITIVE_SENTINEL not in repr(dimensions) for _, dimensions in measurements)
