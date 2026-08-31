from __future__ import annotations

import logging
import os
import re
import time
from contextlib import contextmanager
from functools import wraps
from typing import Any, Awaitable, Callable, ParamSpec, TypeVar

from app.settings import TelemetrySettings, load_telemetry_settings

LOGGER_NAME = "fantasy_cards_generator.telemetry"
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$", re.ASCII)
SAFE_ROUTES = {
    "/",
    "/app",
    "/auth/login",
    "/auth/callback",
    "/auth/logout",
    "/healthz",
    "/partials/ping",
    "/api/v1/cards/generate",
    "/api/v1/cards/{card_id}/artwork/retry",
    "/ui/cards/generate",
    "/ui/cards/{card_id}/artwork/retry",
    "/cards/{card_id}/image",
}
SAFE_EVENTS = {
    "exception",
    "request.completed",
    "request.failed",
    "auth.callback_failed",
    "generation.started",
    "generation.completed",
    "generation.failed",
    "generation.partial",
    "dependency.started",
    "dependency.completed",
    "dependency.failed",
    "dependency.timeout",
    "dependency.retry",
    "moderation.decision",
    "persistence.completed",
    "persistence.failed",
    "compensation.completed",
    "compensation.failed",
}
SAFE_OPERATIONS = {"generate", "artwork_retry", "fetch_image", "text", "image"}
SAFE_OUTCOMES = {
    "started",
    "completed",
    "partial",
    "allowed",
    "blocked",
    "throttled",
    "timed_out",
    "failed",
    "replayed",
}
SAFE_STAGES = {
    "reserved",
    "rate_limit",
    "pre_moderation",
    "foundry_text",
    "post_text_moderation",
    "art_prompt_moderation",
    "foundry_image",
    "post_image_moderation",
    "persistence",
    "blob_upload",
    "cosmos_write",
    "audit_write",
    "compensation_delete",
    "pre_prompt",
    "post_text",
    "post_art_prompt",
    "post_image",
}
SAFE_DEPENDENCIES = {"foundry_text", "foundry_image", "cosmos", "blob", "entra", "other"}
SAFE_STORES = {"card", "audit", "blob", "cosmos", "memory"}
SAFE_PERSISTENCE_OPERATIONS = {
    "reserve",
    "read",
    "save_partial",
    "save_completed",
    "save_failure",
    "upload",
    "download",
    "delete",
    "compensate",
}
SAFE_PARTIAL_REASONS = {"image_failure", "image_timeout", "moderation_rejection"}
SAFE_MODERATION_REASONS = {
    "allowed",
    "living_artist_imitation",
    "copyrighted_logo",
    "trademark_request",
    "copyrighted_character",
    "graphic_violence",
    "sexual_content_minor",
    "self_harm",
    "unsafe_generated_image",
    "invalid_image_payload",
    "other",
}
SAFE_ERROR_CODES = {
    "none",
    "artwork_retry_available",
    "card_not_found",
    "configuration_error",
    "csrf_failed",
    "generated_art_rejected",
    "generated_text_rejected",
    "idempotency_conflict",
    "image_not_found",
    "invalid_model_output",
    "persistence_failure",
    "prompt_rejected",
    "rate_limit_exceeded",
    "request_replay_timeout",
    "retry_conflict",
    "unauthorized",
    "upstream_failure",
    "upstream_timeout",
    "validation_error",
    "dependency_error",
    "internal_error",
}
SAFE_ATTRIBUTE_KEYS = {
    "app.request_id",
    "fcg.operation",
    "fcg.outcome",
    "fcg.stage",
    "fcg.error_code",
    "fcg.retryable",
    "fcg.dependency",
    "fcg.attempt",
    "fcg.partial_reason",
    "fcg.moderation_reason",
    "fcg.policy",
    "fcg.store",
    "fcg.persistence_operation",
    "fcg.token_type",
    "http.route",
    "http.response.status_code",
}
SAFE_AUTO_ATTRIBUTE_KEYS = {
    "http.request.method",
    "http.method",
    "http.response.status_code",
    "http.status_code",
    "http.route",
    "network.protocol.name",
    "network.protocol.version",
    "db.system",
    "db.system.name",
    "db.operation",
    "db.operation.name",
    "rpc.system",
    "rpc.service",
    "rpc.method",
    "azure.namespace",
}

_logger = logging.getLogger(LOGGER_NAME)
_configured = False
_enabled = False
_tracer: Any = None
_generation_counter: Any = None
_generation_duration: Any = None
_partial_counter: Any = None
_artwork_retry_counter: Any = None
_dependency_counter: Any = None
_dependency_duration: Any = None
_dependency_throttle_counter: Any = None
_dependency_timeout_counter: Any = None
_moderation_counter: Any = None
_persistence_counter: Any = None
_token_counter: Any = None

P = ParamSpec("P")
R = TypeVar("R")


class PrivacySpanProcessor:
    """Last-mile removal of sensitive auto-instrumentation attributes."""

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        del parent_context
        _sanitize_span_attributes(span)

    def _on_ending(self, span: Any) -> None:
        """Sanitize the mutable SDK span before its immutable export snapshot."""
        _replace_span_attributes(span)
        _replace_span_events(span)
        _replace_span_links(span)
        _sanitize_span_name(span)
        status = getattr(span, "status", None)
        if getattr(status, "description", None):
            try:
                from opentelemetry.trace import Status

                span._status = Status(status.status_code)
            except (AttributeError, ImportError):
                pass

    def on_end(self, span: Any) -> None:
        # OpenTelemetry 1.43 ReadableSpan collections are immutable by this point.
        del span

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        del timeout_millis
        return True


class PrivacyLogRecordProcessor:
    def on_emit(self, log_data: Any) -> None:
        log_record = getattr(log_data, "log_record", log_data)
        attributes = getattr(log_record, "attributes", None)
        if attributes is None or not hasattr(attributes, "clear"):
            return
        log_key_map = {key.replace(".", "_"): key for key in SAFE_ATTRIBUTE_KEYS}
        dotted = {
            log_key_map[key]: value for key, value in attributes.items() if key in log_key_map
        }
        retained = safe_attributes(dotted)
        attributes.clear()
        attributes.update(retained)

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        del timeout_millis
        return True


def configure_telemetry(settings: TelemetrySettings | None = None) -> bool:
    global _configured, _enabled, _tracer
    if _configured:
        return _enabled
    _configured = True

    try:
        telemetry_settings = settings or load_telemetry_settings()
        if not telemetry_settings.enabled or not telemetry_settings.connection_string:
            return False

        _disable_sensitive_http_capture()
        from azure.monitor.opentelemetry import configure_azure_monitor
        from opentelemetry.sdk.resources import Resource

        resource_attributes: dict[str, str] = {
            "service.name": telemetry_settings.service_name,
            "deployment.environment.name": telemetry_settings.environment,
            "cloud.platform": "azure_container_apps",
        }
        if telemetry_settings.service_version:
            resource_attributes["service.version"] = telemetry_settings.service_version
        if telemetry_settings.container_revision:
            resource_attributes["service.instance.revision"] = telemetry_settings.container_revision
        if telemetry_settings.container_replica:
            resource_attributes["service.instance.id"] = telemetry_settings.container_replica

        _configure_parent_based_sampling(telemetry_settings.sampling_ratio)
        configure_azure_monitor(
            connection_string=telemetry_settings.connection_string,
            logger_name=LOGGER_NAME,
            resource=Resource.create(resource_attributes),
            enable_live_metrics=False,
            enable_performance_counters=False,
            disable_offline_storage=True,
            span_processors=[PrivacySpanProcessor()],
            log_record_processors=[PrivacyLogRecordProcessor()],
        )
        _instrument_httpx()
        from opentelemetry import trace

        _tracer = trace.get_tracer("fantasy_cards_generator", "1")
        _initialize_metrics()
        _enabled = True
        return True
    except Exception:
        # Telemetry must never prevent the application from starting.
        _logger.warning("telemetry.configuration_failed")
        return False


def telemetry_enabled() -> bool:
    return _enabled


def valid_request_id(value: str | None) -> bool:
    return isinstance(value, str) and REQUEST_ID_PATTERN.fullmatch(value) is not None


def normalize_route(value: str | None) -> str:
    return value if value in SAFE_ROUTES else "unknown"


def normalize_error_code(value: Any) -> str:
    normalized = _normalize_token(value)
    moderation_code = normalize_moderation_reason(value)
    if normalized in SAFE_ERROR_CODES:
        return normalized
    if moderation_code != "other":
        return moderation_code
    if normalized in {"timeout_error", "timeouterror"}:
        return "upstream_timeout"
    return "internal_error"


def normalize_stage(value: Any) -> str:
    return _bounded_value(value, SAFE_STAGES, "reserved")


def normalize_dependency(value: Any) -> str:
    return _bounded_value(value, SAFE_DEPENDENCIES, "other")


def normalize_moderation_reason(value: Any) -> str:
    return _bounded_value(value, SAFE_MODERATION_REASONS, "other")


def enrich_request_span(*, request_id: str, route: str | None, status_code: int) -> None:
    if not _enabled:
        return
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if not span.is_recording():
            return
        span.set_attribute("app.request_id", request_id)
        span.set_attribute("http.route", normalize_route(route))
        span.set_attribute("http.response.status_code", int(status_code))
    except (AttributeError, ImportError):
        return


@contextmanager
def telemetry_span(
    name: str,
    *,
    request_id: str | None = None,
    attributes: dict[str, Any] | None = None,
):
    if not _enabled or _tracer is None:
        yield None
        return
    safe = safe_attributes(attributes or {})
    if valid_request_id(request_id):
        safe["app.request_id"] = request_id
    with _tracer.start_as_current_span(
        name,
        attributes=safe,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        yield span


def add_event(name: str, attributes: dict[str, Any] | None = None) -> None:
    if not _enabled or name not in SAFE_EVENTS:
        return
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if span.is_recording():
            span.add_event(name, safe_attributes(attributes or {}))
    except (AttributeError, ImportError):
        return


def safe_log(
    name: str,
    *,
    level: int = logging.INFO,
    request_id: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> None:
    if not _enabled:
        return
    if name not in SAFE_EVENTS:
        name = "generation.failed"
    extra = {
        key.replace(".", "_"): value for key, value in safe_attributes(attributes or {}).items()
    }
    if valid_request_id(request_id):
        extra["app_request_id"] = request_id
    _logger.log(level, name, extra=extra)


def safe_persistence_log(
    *,
    event: str,
    request_id: str,
    stage: str,
    status_code: int | None,
    azure_error_code: Any,
) -> None:
    normalized_event = (
        event
        if event in {"persistence-failed", "compensation-failed", "compensation-succeeded"}
        else "persistence-failed"
    )
    normalized_stage = _bounded_value(
        stage,
        {"blob_upload", "cosmos_write", "audit_write", "compensation_delete", "cosmos_delete"},
        "cosmos_write",
    ).replace("_", "-")
    normalized_status = (
        status_code if isinstance(status_code, int) and 100 <= status_code <= 599 else None
    )
    normalized_azure_code = _normalize_azure_error_code(azure_error_code)
    diagnostic_id = request_id if valid_request_id(request_id) else "invalid"
    _logger.log(
        logging.ERROR if normalized_event.endswith("failed") else logging.WARNING,
        (
            f"{normalized_event} request_id={diagnostic_id} stage={normalized_stage} "
            f"azure_status={normalized_status} azure_error_code={normalized_azure_code}"
        ),
        extra={
            "app_request_id": diagnostic_id,
            "fcg_stage": normalize_stage(normalized_stage),
            "fcg_error_code": normalize_error_code(normalized_azure_code),
        },
    )


def instrument_generation(
    operation: str,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    normalized_operation = _bounded_value(operation, SAFE_OPERATIONS, "generate")

    def decorator(function: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(function)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            request_id = kwargs.get("request_id")
            diagnostic_id = request_id if isinstance(request_id, str) else None
            started = time.perf_counter()
            base = {"fcg.operation": normalized_operation}
            with telemetry_span(
                "fcg.generation",
                request_id=diagnostic_id,
                attributes=base,
            ) as span:
                add_event("generation.started", base)
                safe_log("generation.started", request_id=diagnostic_id, attributes=base)
                try:
                    result = await function(*args, **kwargs)
                except BaseException as exc:
                    error_code = normalize_error_code(
                        getattr(exc, "error_code", type(exc).__name__)
                    )
                    outcome = _outcome_for_error(error_code)
                    attributes = {**base, "fcg.outcome": outcome, "fcg.error_code": error_code}
                    _set_span_attributes(span, attributes)
                    add_event("generation.failed", attributes)
                    _record_generation(
                        normalized_operation,
                        outcome,
                        (time.perf_counter() - started) * 1000,
                    )
                    if normalized_operation == "artwork_retry":
                        _metric_add(
                            _artwork_retry_counter,
                            1,
                            {"fcg.outcome": outcome, "fcg.error_code": error_code},
                        )
                    safe_log(
                        "generation.failed",
                        level=logging.WARNING,
                        request_id=diagnostic_id,
                        attributes=attributes,
                    )
                    raise
                outcome = (
                    "partial"
                    if getattr(result, "status", None) == "awaiting_artwork_retry"
                    else "completed"
                )
                attributes = {**base, "fcg.outcome": outcome, "fcg.error_code": "none"}
                _set_span_attributes(span, attributes)
                add_event(
                    "generation.partial" if outcome == "partial" else "generation.completed",
                    attributes,
                )
                _record_generation(
                    normalized_operation,
                    outcome,
                    (time.perf_counter() - started) * 1000,
                )
                if normalized_operation == "artwork_retry":
                    _metric_add(
                        _artwork_retry_counter,
                        1,
                        {"fcg.outcome": outcome, "fcg.error_code": "none"},
                    )
                safe_log(
                    "generation.partial" if outcome == "partial" else "generation.completed",
                    request_id=diagnostic_id,
                    attributes=attributes,
                )
                return result

        return wrapped

    return decorator


def record_partial(reason: str) -> None:
    normalized = _bounded_value(reason, SAFE_PARTIAL_REASONS, "image_failure")
    attributes = {"fcg.partial_reason": normalized}
    _metric_add(_partial_counter, 1, attributes)
    add_event("generation.partial", attributes)


def record_dependency_attempt(
    *,
    dependency: str,
    attempt: int,
    outcome: str,
    duration_ms: float,
    request_id: str | None,
    error_code: Any = "none",
    retryable: bool = False,
) -> None:
    normalized_dependency = normalize_dependency(dependency)
    normalized_outcome = _bounded_value(outcome, SAFE_OUTCOMES, "failed")
    normalized_error = normalize_error_code(error_code) if error_code != "none" else "none"
    attributes = {
        "fcg.dependency": normalized_dependency,
        "fcg.attempt": _attempt_bucket(attempt),
        "fcg.outcome": normalized_outcome,
        "fcg.error_code": normalized_error,
        "fcg.retryable": bool(retryable),
    }
    _metric_add(_dependency_counter, 1, attributes)
    _metric_record(_dependency_duration, duration_ms, attributes)
    if normalized_outcome == "throttled":
        _metric_add(
            _dependency_throttle_counter,
            1,
            {"fcg.dependency": normalized_dependency},
        )
    if normalized_outcome == "timed_out":
        _metric_add(
            _dependency_timeout_counter,
            1,
            {"fcg.dependency": normalized_dependency},
        )
    event = {
        "completed": "dependency.completed",
        "timed_out": "dependency.timeout",
    }.get(normalized_outcome, "dependency.failed")
    add_event(event, attributes)
    safe_log(
        event,
        level=logging.INFO if normalized_outcome == "completed" else logging.WARNING,
        request_id=request_id,
        attributes=attributes,
    )


def record_retry(*, dependency: str, attempt: int, request_id: str | None) -> None:
    attributes = {
        "fcg.dependency": normalize_dependency(dependency),
        "fcg.attempt": _attempt_bucket(attempt),
    }
    add_event("dependency.retry", attributes)
    safe_log("dependency.retry", request_id=request_id, attributes=attributes)


def record_moderation(*, stage: str, allowed: bool, reason: str, policy: str) -> None:
    attributes = {
        "fcg.stage": normalize_stage(stage),
        "fcg.outcome": "allowed" if allowed else "blocked",
        "fcg.moderation_reason": normalize_moderation_reason(reason),
        "fcg.policy": _normalize_policy(policy),
    }
    _metric_add(_moderation_counter, 1, attributes)
    add_event("moderation.decision", attributes)


def record_persistence(
    *,
    store: str,
    operation: str,
    outcome: str,
    request_id: str | None,
    error_code: Any = "none",
) -> None:
    attributes = {
        "fcg.store": _bounded_value(store, SAFE_STORES, "card"),
        "fcg.persistence_operation": _bounded_value(
            operation,
            SAFE_PERSISTENCE_OPERATIONS,
            "save_failure",
        ),
        "fcg.outcome": _bounded_value(outcome, SAFE_OUTCOMES, "failed"),
        "fcg.error_code": (normalize_error_code(error_code) if error_code != "none" else "none"),
    }
    _metric_add(_persistence_counter, 1, attributes)
    event = (
        "persistence.completed"
        if attributes["fcg.outcome"] == "completed"
        else "persistence.failed"
    )
    add_event(event, attributes)
    safe_log(
        event,
        level=logging.INFO if attributes["fcg.outcome"] == "completed" else logging.ERROR,
        request_id=request_id,
        attributes=attributes,
    )


def record_token_usage(operation: str, usage: Any) -> None:
    normalized_operation = _bounded_value(operation, {"text", "image"}, "text")
    for token_type, field_name in (
        ("input", "inputTokens"),
        ("output", "outputTokens"),
        ("total", "totalTokens"),
    ):
        value = getattr(usage, field_name, None)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            _metric_add(
                _token_counter,
                value,
                {"fcg.operation": normalized_operation, "fcg.token_type": token_type},
            )


def safe_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in attributes.items():
        if key not in SAFE_ATTRIBUTE_KEYS:
            continue
        if key == "app.request_id":
            if valid_request_id(value):
                safe[key] = value
        elif key == "fcg.operation":
            safe[key] = _bounded_value(value, SAFE_OPERATIONS, "generate")
        elif key == "fcg.outcome":
            safe[key] = _bounded_value(value, SAFE_OUTCOMES, "failed")
        elif key == "fcg.stage":
            safe[key] = normalize_stage(value)
        elif key == "fcg.error_code":
            safe[key] = normalize_error_code(value)
        elif key == "fcg.retryable":
            safe[key] = bool(value)
        elif key == "fcg.dependency":
            safe[key] = normalize_dependency(value)
        elif key == "fcg.attempt":
            safe[key] = _bounded_value(
                value,
                {"first", "retry_1", "retry_2", "retry_many"},
                "retry_many",
            )
        elif key == "fcg.partial_reason":
            safe[key] = _bounded_value(value, SAFE_PARTIAL_REASONS, "image_failure")
        elif key == "fcg.moderation_reason":
            safe[key] = normalize_moderation_reason(value)
        elif key == "fcg.policy":
            safe[key] = _normalize_policy(value)
        elif key == "fcg.store":
            safe[key] = _bounded_value(value, SAFE_STORES, "card")
        elif key == "fcg.persistence_operation":
            safe[key] = _bounded_value(
                value,
                SAFE_PERSISTENCE_OPERATIONS,
                "save_failure",
            )
        elif key == "fcg.token_type":
            safe[key] = _bounded_value(value, {"input", "output", "total"}, "total")
        elif key == "http.route":
            safe[key] = normalize_route(str(value))
        elif key == "http.response.status_code" and isinstance(value, int):
            safe[key] = value
    return safe


def _initialize_metrics() -> None:
    global _generation_counter, _generation_duration, _partial_counter
    global _artwork_retry_counter, _dependency_counter, _dependency_duration
    global _dependency_throttle_counter, _dependency_timeout_counter
    global _moderation_counter, _persistence_counter, _token_counter
    from opentelemetry import metrics

    meter = metrics.get_meter("fantasy_cards_generator", "1")
    _generation_counter = meter.create_counter("fcg.generation.requests")
    _generation_duration = meter.create_histogram("fcg.generation.duration", unit="ms")
    _partial_counter = meter.create_counter("fcg.generation.partial_results")
    _artwork_retry_counter = meter.create_counter("fcg.artwork.retries")
    _dependency_counter = meter.create_counter("fcg.dependency.attempts")
    _dependency_duration = meter.create_histogram("fcg.dependency.duration", unit="ms")
    _dependency_throttle_counter = meter.create_counter("fcg.dependency.throttles")
    _dependency_timeout_counter = meter.create_counter("fcg.dependency.timeouts")
    _moderation_counter = meter.create_counter("fcg.moderation.decisions")
    _persistence_counter = meter.create_counter("fcg.persistence.operations")
    _token_counter = meter.create_counter("fcg.ai.tokens", unit="{token}")


def _record_generation(operation: str, outcome: str, duration_ms: float) -> None:
    attributes = {"fcg.operation": operation, "fcg.outcome": outcome}
    _metric_add(_generation_counter, 1, attributes)
    _metric_record(_generation_duration, duration_ms, attributes)


def _metric_add(instrument: Any, value: int, attributes: dict[str, Any]) -> None:
    if _enabled and instrument is not None:
        instrument.add(value, attributes=safe_attributes(attributes))


def _metric_record(instrument: Any, value: float, attributes: dict[str, Any]) -> None:
    if _enabled and instrument is not None:
        instrument.record(max(0.0, float(value)), attributes=safe_attributes(attributes))


def _set_span_attributes(span: Any, attributes: dict[str, Any]) -> None:
    if span is None:
        return
    for key, value in safe_attributes(attributes).items():
        span.set_attribute(key, value)


def _outcome_for_error(error_code: str) -> str:
    if error_code == "rate_limit_exceeded":
        return "throttled"
    if error_code in {"upstream_timeout", "request_replay_timeout"}:
        return "timed_out"
    if error_code in {
        "prompt_rejected",
        "generated_text_rejected",
        "generated_art_rejected",
    } or error_code in SAFE_MODERATION_REASONS - {"allowed", "other"}:
        return "blocked"
    return "failed"


def _attempt_bucket(attempt: int) -> str:
    if attempt <= 1:
        return "first"
    if attempt == 2:
        return "retry_1"
    if attempt == 3:
        return "retry_2"
    return "retry_many"


def _normalize_token(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return re.sub(r"[^a-z0-9_]", "", text)[:64]


def _bounded_value(value: Any, allowed: set[str], fallback: str) -> str:
    normalized = _normalize_token(value)
    return normalized if normalized in allowed else fallback


def _normalize_policy(value: Any) -> str:
    normalized = _normalize_token(value)
    return normalized if normalized == "conservative_v1" else "other"


def _normalize_azure_error_code(value: Any) -> str:
    normalized = _normalize_token(value)
    return {
        "authorizationfailure": "AuthorizationFailure",
        "authorization_failure": "AuthorizationFailure",
        "forbidden": "Forbidden",
        "notfound": "NotFound",
        "not_found": "NotFound",
        "requesttimeout": "RequestTimeout",
        "request_timeout": "RequestTimeout",
        "serviceunavailable": "ServiceUnavailable",
        "service_unavailable": "ServiceUnavailable",
        "throttled": "Throttled",
        "toomanyrequests": "TooManyRequests",
        "too_many_requests": "TooManyRequests",
    }.get(normalized, "Other")


def _disable_sensitive_http_capture() -> None:
    for name in (
        "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST",
        "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_RESPONSE",
        "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_REQUEST",
        "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_RESPONSE",
    ):
        os.environ[name] = ""


def _configure_parent_based_sampling(sampling_ratio: float) -> None:
    # These are the selector names supported by azure-monitor-opentelemetry 1.8.9.
    os.environ["OTEL_TRACES_SAMPLER"] = "parentbased_trace_id_ratio"
    os.environ["OTEL_TRACES_SAMPLER_ARG"] = format(sampling_ratio, ".15g")


def _instrument_httpx() -> None:
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    HTTPXClientInstrumentor().instrument()


def _safe_span_attributes(attributes: Any) -> dict[str, Any]:
    retained: dict[str, Any] = {}
    for key, value in dict(attributes or {}).items():
        if key in SAFE_ATTRIBUTE_KEYS:
            retained.update(safe_attributes({key: value}))
        elif key in SAFE_AUTO_ATTRIBUTE_KEYS:
            if key == "http.route":
                retained[key] = normalize_route(str(value))
            elif isinstance(value, (bool, int, float)):
                retained[key] = value
            else:
                retained[key] = _normalize_token(value)[:64] or "other"
    return retained


def _sanitize_span_attributes(span: Any) -> None:
    attributes = getattr(span, "_attributes", None)
    if attributes is None or not hasattr(attributes, "clear"):
        return
    retained = _safe_span_attributes(attributes)
    attributes.clear()
    attributes.update(retained)


def _replace_span_attributes(span: Any) -> None:
    attributes = getattr(span, "_attributes", None)
    if attributes is None:
        return
    try:
        from opentelemetry.attributes import BoundedAttributes

        limits = getattr(span, "_limits", None)
        span._attributes = BoundedAttributes(
            maxlen=getattr(limits, "max_span_attributes", 128),
            attributes=_safe_span_attributes(attributes),
            immutable=True,
            max_value_len=getattr(limits, "max_span_attribute_length", None),
        )
    except (AttributeError, ImportError):
        span._attributes = _safe_span_attributes(attributes)


def _replace_span_events(span: Any) -> None:
    events = getattr(span, "_events", None)
    if events is None:
        return
    try:
        from opentelemetry.sdk.trace import BoundedList, Event

        limits = getattr(span, "_limits", None)
        kept = []
        for event in list(events):
            name = getattr(event, "name", None)
            if name not in SAFE_EVENTS:
                continue
            attributes = getattr(event, "attributes", None)
            if attributes is None:
                attributes = getattr(event, "_attributes", None)
            if name == "exception":
                retained = {
                    "exception.type": _normalize_exception_type(
                        dict(attributes or {}).get("exception.type")
                    )
                }
            else:
                retained = safe_attributes(dict(attributes or {}))
            kept.append(
                Event(
                    name,
                    attributes=retained,
                    timestamp=getattr(event, "timestamp", None),
                    limit=getattr(limits, "max_event_attributes", 128),
                )
            )
        span._events = BoundedList.from_seq(getattr(limits, "max_events", 128), kept)
    except (AttributeError, ImportError, TypeError):
        span._events = []


def _replace_span_links(span: Any) -> None:
    links = getattr(span, "_links", None)
    if links is None:
        return
    try:
        from opentelemetry.sdk.trace import BoundedList
        from opentelemetry.trace import Link

        limits = getattr(span, "_limits", None)
        kept = [
            Link(link.context, attributes=_safe_span_attributes(getattr(link, "attributes", None)))
            for link in list(links)
        ]
        span._links = BoundedList.from_seq(getattr(limits, "max_links", 128), kept)
    except (AttributeError, ImportError, TypeError):
        span._links = []


def _normalize_exception_type(value: Any) -> str:
    text = str(value or "").lower()
    bounded = f"{text[:128]}{text[-128:]}"
    if "timeout" in bounded:
        return "TimeoutError"
    if "connection" in bounded:
        return "ConnectionError"
    if "permission" in bounded or "authorization" in bounded:
        return "PermissionError"
    if "value" in bounded or "validation" in bounded:
        return "ValueError"
    return "Exception"


def _sanitize_span_name(span: Any) -> None:
    kind_name = str(getattr(getattr(span, "kind", None), "name", "")).upper()
    attributes = getattr(span, "_attributes", {}) or {}
    if kind_name == "SERVER":
        method = attributes.get("http.request.method") or attributes.get("http.method") or "HTTP"
        span._name = f"{_normalize_token(method).upper()} {attributes.get('http.route', 'unknown')}"
    elif kind_name == "CLIENT":
        method = attributes.get("http.request.method") or attributes.get("http.method")
        if method:
            span._name = f"HTTP {_normalize_token(method).upper()}"
        elif attributes.get("azure.namespace"):
            span._name = "Azure dependency"
        elif attributes.get("db.system") or attributes.get("db.system.name"):
            span._name = "Database dependency"
        else:
            span._name = "Dependency"
