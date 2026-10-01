"""Bounded, ephemeral diagnostics with runtime-specific release boundaries.

HOSTED releases on its original specialist spans after business acceptance.
WEB's terminal gate still owns WEB diagnostics and legacy private carriers.
Modified views are labelled; oversized projections omit unverifiable partial JSON.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import ipaddress
import json
import logging
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from typing import TYPE_CHECKING, Any, Literal, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

if TYPE_CHECKING:
    from opentelemetry.context import Context
    from opentelemetry.trace import SpanContext

    from app.foundry_agent_client import FoundryAgentInvocationResult

SCOPE = "fantasy_cards_generator.agent_detail"
FIELD_BYTES = 2048
RECORD_BYTES = 8192
RUNTIME_BYTES = 24576
MAX_BUFFERS = 16
MAX_SPANS = 16
MAX_HOSTED_SPANS = 1
MAX_TEXT_CHARS = 16384
STAGES = {"generation", "hosted_invocation", "image"}
NAMES = {
    "generation": "card_generation",
    "hosted_invocation": "fcg.agent.invoke",
    "image": "fcg.agent.image",
}
AGENTS = {
    "generation": "card_generation",
    "hosted_invocation": "card_orchestrator",
    "image": "image_generation",
}
CARD_FIELDS = {
    "schemaVersion",
    "name",
    "cardType",
    "rarity",
    "manaCost",
    "attack",
    "health",
    "rulesText",
    "flavorText",
    "artBrief",
}
FLAGS = Literal["redacted", "truncated", "suppressed_policy", "unvalidated", "omitted_budget"]
Runtime = Literal["web", "hosted"]
Stage = Literal["generation", "hosted_invocation", "image"]
ExecutionOutcome = Literal["completed", "failed", "refused", "held", "routing_defer", "cancelled"]
ExecutionReason = Literal[
    "none",
    "dependency_error",
    "schema_invalid",
    "suppressed_policy",
    "model_refusal",
    "stage_incomplete",
    "timeout",
    "cancelled",
    "authentication",
    "authorization",
    "resource_not_found",
    "invalid_request",
    "rate_limited",
    "service_error",
    "transport_error",
    "invalid_response",
]
_capacity = threading.BoundedSemaphore(MAX_BUFFERS)
_current: contextvars.ContextVar[Capture | None] = contextvars.ContextVar(
    "agent_detail_capture", default=None
)
_admission: contextvars.ContextVar[Admission | None] = contextvars.ContextVar(
    "agent_detail_admission", default=None
)
_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "agent_detail_deadline", default=None
)
_boundary: contextvars.ContextVar[ReleaseBoundary | None] = contextvars.ContextVar(
    "agent_detail_boundary", default=None
)
_ending: contextvars.ContextVar[dict[int, tuple[PendingSpan, str | None, bytes | None]] | None] = (
    contextvars.ContextVar("agent_detail_ending", default=None)
)


def diagnostic(reason: str) -> None:
    # Never log the exception, input, identifiers or diagnostic carrier.
    from app.telemetry import safe_log

    capture = current()
    if capture is not None:
        if reason in capture.omissions:
            return
        capture.omissions.add(reason)
    safe_log(
        "agent.detail.omitted", level=logging.WARNING, attributes={"fcg.detail.omission": reason}
    )


def encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class View(Closed):
    text: str = ""
    flags: tuple[FLAGS, ...] = ()

    @model_validator(mode="after")
    def bounded(self) -> View:
        if len(self.text.encode("utf-8")) > FIELD_BYTES or len(self.flags) > 5:
            raise ValueError("invalid_detail_field")
        return self


class Source(Closed):
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    span_id: str = Field(pattern=r"^[0-9a-f]{16}$")

    @model_validator(mode="after")
    def nonzero(self) -> Source:
        if int(self.trace_id, 16) == 0 or int(self.span_id, 16) == 0:
            raise ValueError("invalid_detail_context")
        return self


class Record(Closed):
    capture_version: Literal[1] = 1
    redaction_version: Literal[1] = 1
    source_runtime: Runtime
    stage: Stage
    agent_name: Literal["card_generation", "card_orchestrator", "image_generation"]
    operation: Literal["generate", "artwork_retry"] = "generate"
    attempt: int = Field(default=1, ge=1, le=16)
    source: Source
    duration_ms: int = Field(ge=0, le=3600000)
    instruction_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    instruction_version: Literal["application-v1"] = "application-v1"
    deployment_version: str = Field(max_length=64, pattern=r"^[A-Za-z0-9._-]*$")
    result: Literal["completed"] = "completed"
    reason: Literal["allowed"] = "allowed"
    validation: Literal["validated", "modified"] = "validated"
    moderation: Literal["allowed"] = "allowed"
    output_kind: Literal["card", "refinement", "image_outcome", "final_card"]
    instruction: View
    input: View
    output: View
    flags: tuple[FLAGS, ...] = Field(default=(), max_length=5)

    @field_validator("capture_version", "redaction_version", mode="before")
    @classmethod
    def integer_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("invalid_detail_version")
        return value

    @model_validator(mode="after")
    def stage_contract(self) -> Record:
        expected = {
            "generation": ("hosted", "card"),
            "hosted_invocation": ("web", "final_card"),
            "image": ("web", "image_outcome"),
        }
        if (
            (self.source_runtime, self.output_kind) != expected[self.stage]
            or self.agent_name != AGENTS[self.stage]
            or (self.source_runtime == "hosted" and self.operation != "generate")
        ):
            raise ValueError("invalid_detail_stage")
        validate_record_content(self)
        return self


class Envelope(Closed):
    version: Literal[1] = 1
    records: tuple[Record, ...] = Field(max_length=3)


def _validate_safety_evidence(value: Any) -> None:
    required = ("pre_prompt", "generation", "final_text", "final_art_prompt")
    expected_sequence = (*required, "hosted_guardrails", "post_image")
    if type(value) is not list or len(value) != len(expected_sequence) or not _bounded_tree(value):
        raise ValueError("invalid_safety")
    for index, item in enumerate(value):
        if type(item) is not dict or set(item) != {"stage", "policy", "decision", "reason"}:
            raise ValueError("invalid_safety")
        stage = item["stage"]
        if type(stage) is not str or stage != expected_sequence[index]:
            raise ValueError("invalid_safety")
        expected = (
            {
                "stage": stage,
                "policy": "original-fantasy-v1",
                "decision": "allowed",
                "reason": "allowed",
            }
            if stage in required
            else (
                {
                    "stage": "hosted_guardrails",
                    "policy": "hosted_guardrails",
                    "decision": "unavailable",
                    "reason": "not_observed",
                }
                if stage == "hosted_guardrails"
                else {
                    "stage": "post_image",
                    "policy": "post_image",
                    "decision": "not_applicable",
                    "reason": "text_only",
                }
            )
        )
        if item != expected:
            raise ValueError("invalid_safety")


def _bounded_tree(value: Any, depth: int = 0, budget: list[int] | None = None) -> bool:
    if budget is None:
        budget = [512, 4 * RUNTIME_BYTES]
    budget[0] -= 1
    budget[1] -= 2
    if budget[0] < 0 or budget[1] < 0 or depth > 8:
        return False
    if type(value) is str:
        if len(value) > RECORD_BYTES:
            return False
        budget[1] -= len(encoded(value))
        return budget[1] >= 0
    if value is None or type(value) is bool:
        return True
    if type(value) is int:
        return -3600000 <= value <= 3600000
    if type(value) in (list, tuple):
        return len(value) <= 20 and all(_bounded_tree(v, depth + 1, budget) for v in value)
    if type(value) is dict:
        return len(value) <= 32 and all(
            type(k) is str
            and len(k) <= 64
            and _bounded_tree(k, depth + 1, budget)
            and _bounded_tree(v, depth + 1, budget)
            for k, v in value.items()
        )
    return False


def parse_envelope(
    value: Any,
    safety_evidence: Any = None,
    *,
    expected_source: Source | None = None,
    deployment_version: str | None = None,
) -> Envelope | None:
    if value is None:
        return None
    try:
        if not _bounded_tree(value) or len(encoded(value)) > RUNTIME_BYTES:
            diagnostic("omitted_budget")
            return None
        try:
            _validate_safety_evidence(safety_evidence)
        except ValueError:
            diagnostic("invalid_safety")
            return None
        if type(value) is not dict or type(value.get("version")) is not int:
            raise ValueError("invalid_detail_version")
        # JSON validation accepts wire arrays for immutable tuple fields.
        envelope = Envelope.model_validate_json(encoded(value), strict=True)
        seen = set()
        sources = set()
        for record in envelope.records:
            if (
                record.source_runtime != "hosted"
                or record.stage != "generation"
                or record.stage in seen
                or len(encoded(record.model_dump(mode="json"))) > RECORD_BYTES
                or len(record.flags) > 5
                or expected_source is None
                or record.source.trace_id != expected_source.trace_id
                or record.source.span_id == expected_source.span_id
                or record.source.span_id in sources
                or record.deployment_version != deployment_version
                or record.attempt != 1
            ):
                raise ValueError("invalid_detail_record")
            seen.add(record.stage)
            sources.add(record.source.span_id)
        return envelope
    except (ValueError, TypeError, UnicodeError, RecursionError, ValidationError):
        diagnostic("invalid_extension")
        return None


# Match label categories, including suffixes after underscores (unlike \b).
# Compact forms cover conventional labels too; arbitrary prose is not guaranteed PII-free.
_PRIVATE = re.compile(
    r"https?://|data:|[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|"
    r"(?<![A-Z0-9])(?:bearer|(?:proxy[_ -]?)?authorization|authentication|"
    r"(?:client[_ -]?)?(?:credentials?|secrets?|passwords?)|"
    r"(?:(?:access|refresh|session|id|csrf|xsrf|auth|identity|sas|bearer)[_ -]?)?tokens?|"
    r"(?:set[_ -]?)?cookies?|(?:(?:request|response|http|auth)[_ -]?)?headers?|"
    r"(?:api|private|public|access|secret|account|storage|client|subscription|"
    r"signing|encryption|idempotency)[_ -]?keys?|connection[_ -]?strings?|"
    r"(?:(?:email|owner|user|tenant|object|account|card|blob|session|request|resource|"
    r"subscription|client|business|personal|customer|correlation)[_ -]?)?(?:ids?|identifiers?)|"
    r"e[_ -]?mail|client[_ -]?ip|ip[_ -]?address)(?![A-Z0-9])|"
    r"\b(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]+|"
    r"sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|"
    r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b|"
    r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])|"
    r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b|"
    r"\b(?:\+?\d[\d ()-]{7,}\d)\b|[A-Za-z0-9+/=_-]{48,}",
    re.IGNORECASE,
)


def _private_text(text: str) -> bool:
    if _PRIVATE.search(text):
        return True
    for match in re.finditer(r"(?<![\w:])[\da-f:.]+(?:%[\w.-]+)?(?![\w:])", text, re.I):
        literal = match.group().strip(".")
        if "." not in literal and ":" not in literal:
            continue
        try:
            ipaddress.ip_address(literal)
        except ValueError:
            continue
        return True
    return False


def text_view(text: str, *, trusted: bool = False) -> View:
    if type(text) is not str:
        return View(flags=("unvalidated",))
    if len(text) > MAX_TEXT_CHARS:
        return View(flags=("omitted_budget",))
    # Inspect the entire permitted field before truncating: a secret after the
    # preview boundary must not leave a misleading unredacted prefix.
    if not trusted and _private_text(text):
        return View(flags=("redacted",))
    if any(ord(c) < 32 and c not in "\n\t" or 0xD800 <= ord(c) <= 0xDFFF for c in text):
        return View(flags=("redacted",))
    data = text.encode("utf-8")
    if len(data) > FIELD_BYTES:
        return View(text=data[:FIELD_BYTES].decode("utf-8", errors="ignore"), flags=("truncated",))
    return View(text=text)


def projection_view(payload: dict[str, Any], *, fields: set[str]) -> View:
    if type(payload) is not dict or len(payload) > 16 or payload.keys() - fields:
        return View(flags=("unvalidated",))
    clean: dict[str, Any] = {}
    flags: set[str] = set()
    for key in sorted(payload):
        value = payload[key]
        if type(value) is str:
            view = text_view(value)
            flags.update(view.flags)
            clean[key] = view.text
        elif type(value) is int and -1000 <= value <= 1000:
            clean[key] = value
        else:
            return View(flags=("unvalidated",))
    view = text_view(encoded(clean).decode("utf-8"), trusted=True)
    flags.update(view.flags)
    if "truncated" in flags:
        # Partial JSON cannot be independently schema-validated by the receiver.
        flags.add("omitted_budget")
        return View(flags=tuple(sorted(flags)))
    return View(text=view.text, flags=tuple(sorted(flags)))


class QueryProjection(Closed):
    query: str = Field(min_length=1, max_length=400)


class ImageInputProjection(Closed):
    artPrompt: str = Field(min_length=1, max_length=1000)
    quality: Literal["low", "medium", "high"]
    mode: Literal["generate", "edit"]


class ImageOutputProjection(Closed):
    outcome: Literal["completed"]


def _contracts(stage: Stage) -> tuple[str, type[BaseModel], type[BaseModel]]:
    from app.generation import GeneratedCardModel
    from app.specialist_contract import SCHEMAS, effective_instructions

    if stage == "generation":
        return (
            effective_instructions(stage),
            QueryProjection,
            SCHEMAS[stage],
        )
    if stage == "hosted_invocation":
        return "", QueryProjection, GeneratedCardModel
    return "", ImageInputProjection, ImageOutputProjection


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("invalid_detail_projection")
        result[key] = value
    return result


def _validate_projection(view: View, schema: type[BaseModel]) -> None:
    flags = set(view.flags)
    if len(flags) != len(view.flags) or flags - {"redacted", "truncated", "omitted_budget"}:
        raise ValueError("invalid_detail_projection")
    if "omitted_budget" in flags:
        if view.text:
            raise ValueError("invalid_detail_projection")
        return
    if "truncated" in flags:
        raise ValueError("invalid_detail_projection")
    payload = json.loads(view.text, object_pairs_hook=_unique_object)
    if type(payload) is not dict or payload.keys() != schema.model_fields.keys():
        raise ValueError("invalid_detail_projection")
    redacted = False
    for name, field_info in schema.model_fields.items():
        value = payload[name]
        expected_types = (
            {type(item) for item in get_args(field_info.annotation)}
            if get_args(field_info.annotation)
            else {field_info.annotation}
        )
        if type(value) not in expected_types:
            raise ValueError("invalid_detail_projection")
        if field_info.annotation is str and value == "" and "redacted" in flags:
            redacted = True
            continue
        TypeAdapter(field_info.rebuild_annotation()).validate_python(value, strict=True)
        if type(value) is str and text_view(value) != View(text=value):
            raise ValueError("invalid_detail_projection")
    if "redacted" in flags and not redacted:
        raise ValueError("invalid_detail_projection")


def _record_flags(instruction: View, input_view: View, output: View) -> tuple[FLAGS, ...]:
    return tuple(sorted(set(instruction.flags + input_view.flags + output.flags)))


def validate_record_content(record: Record) -> None:
    instruction, input_schema, output_schema = _contracts(record.stage)
    expected = text_view(instruction, trusted=True)
    if (
        record.instruction_digest != hashlib.sha256(instruction.encode("utf-8")).hexdigest()
        or record.instruction not in (expected, View(flags=("omitted_budget",)))
        or record.flags != _record_flags(record.instruction, record.input, record.output)
        or record.validation
        != ("modified" if record.input.flags or record.output.flags else "validated")
        or text_view(record.deployment_version) != View(text=record.deployment_version)
    ):
        raise ValueError("invalid_detail_record")
    _validate_projection(record.input, input_schema)
    _validate_projection(record.output, output_schema)


def _source(span: Any) -> Source | None:
    if span is None or not span.is_recording():
        return None
    context = span.get_span_context() if span is not None else None
    if context is None or not context.is_valid or not context.trace_flags.sampled:
        return None
    return Source(trace_id=f"{context.trace_id:032x}", span_id=f"{context.span_id:016x}")


@dataclass(slots=True)
class Execution:
    span: Any = None
    source: Source | None = None
    identity: SpanIdentity | None = None
    started: float = field(default_factory=time.perf_counter)
    duration_ms: int = 0
    outcome: str = "failed"
    validation: str = "unvalidated"
    moderation: str = "unvalidated"
    reason: str = "dependency_error"
    attempt: int = 1
    instruction: str | None = None


@dataclass(frozen=True, slots=True)
class SpanIdentity:
    name: str
    context: tuple[Any, ...] | None
    parent: tuple[Any, ...] | None
    start_time: int
    scope: tuple[Any, ...]


def _context_identity(context: SpanContext | None) -> tuple[Any, ...] | None:
    if context is None:
        return None
    return (
        context.trace_id,
        context.span_id,
        context.is_remote,
        int(context.trace_flags),
        tuple(context.trace_state.items()),
    )


def _span_identity(span: Any) -> SpanIdentity:
    scope = span.instrumentation_scope
    return SpanIdentity(
        span.name,
        _context_identity(span.get_span_context()),
        _context_identity(span.parent),
        span.start_time,
        (
            scope.name,
            scope.version,
            scope.schema_url,
            tuple(sorted((scope.attributes or {}).items())),
        ),
    )


@dataclass(frozen=True, slots=True)
class PendingSpan:
    span: Any
    source: Source
    stage: Stage
    end_time: int
    attributes: tuple[tuple[str, str | int], ...]
    identity: SpanIdentity
    deadline: float | None = None


class DeadlineExceeded(TimeoutError):
    """The original operation budget expired, including synchronous acceptance work."""


def check_deadline(deadline: float | None) -> None:
    if deadline is not None:
        if asyncio.get_running_loop().time() >= deadline:
            raise DeadlineExceeded
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError


@contextmanager
def operation_deadline(deadline: float):
    token = _deadline.set(deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def current_deadline() -> float | None:
    return _deadline.get()


def _validate_pending(owned: PendingSpan, span: Any, *, ending: bool = False) -> None:
    if (
        span is not owned.span
        or _span_identity(span) != owned.identity
        or owned.identity.name != NAMES[owned.stage]
        or owned.identity.scope[0] != SCOPE
        or span.end_time != (owned.end_time if ending else None)
        or owned.end_time < owned.identity.start_time
    ):
        raise ValueError("invalid_detail_context")
    for key, expected in owned.attributes:
        actual = span.attributes.get(key)
        if type(actual) is not type(expected) or actual != expected:
            raise ValueError("invalid_detail_context")


def _end_span(span: Any, end_time: int) -> None:
    try:
        span.end(end_time=end_time)
    except Exception:
        diagnostic("instrumentation_failure")


def invocation_outcome(
    result: FoundryAgentInvocationResult,
) -> tuple[ExecutionOutcome, ExecutionReason]:
    if result.success:
        return "completed", "none"
    if result.status in {"policy_refusal", "refused"}:
        return "refused", "model_refusal"
    if result.status == "held":
        return "held", "stage_incomplete"
    if result.status == "routing_defer":
        return "routing_defer", "stage_incomplete"

    reasons: dict[str, ExecutionReason] = {
        "timeout": "timeout",
        "authentication": "authentication",
        "authorization": "authorization",
        "resource_not_found": "resource_not_found",
        "invalid_request": "invalid_request",
        "rate_limited": "rate_limited",
        "service_error": "service_error",
        "transport_error": "transport_error",
        "invalid_response": "invalid_response",
        "dependency_error": "dependency_error",
    }
    if result.runtime_failure_reason is not None and result.runtime_failure_reason in reasons:
        return "failed", reasons[result.runtime_failure_reason]
    errors: dict[str, ExecutionReason] = {
        "timeout": "timeout",
        "transport_error": "transport_error",
        "credential_unavailable": "authentication",
        "credential_service_unavailable": "service_error",
        "http_401": "authentication",
        "http_403": "authorization",
        "http_404": "resource_not_found",
        "http_429": "rate_limited",
        "schema_validation_failed": "schema_invalid",
    }
    if result.error_code is not None and result.error_code in errors:
        return "failed", errors[result.error_code]
    statuses: dict[str, ExecutionReason] = {
        "auth_error": "authentication",
        "configuration_error": "invalid_request",
        "version_mismatch": "invalid_response",
        "invalid_response": "invalid_response",
        "incomplete": "stage_incomplete",
        "transient_error": "service_error",
    }
    return "failed", statuses.get(result.status, "dependency_error")


@dataclass(slots=True)
class Capture:
    runtime: Runtime
    operation: Literal["generate", "artwork_retry"] = "generate"
    records: list[Record] = field(default_factory=list)
    spans: int = 0
    closed: bool = False
    released: bool = False
    eligible: bool = True
    execution: Execution | None = None
    omissions: set[str] = field(default_factory=set)
    pending: list[PendingSpan] = field(default_factory=list)
    deployment_version: str = ""
    deadline: float | None = None
    _approved_records: dict[str, bytes] = field(default_factory=dict)

    def deny(self) -> None:
        self.eligible = False
        self.records.clear()
        self._approved_records.clear()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.eligible = False
            self.records.clear()
            self._approved_records.clear()
            try:
                self.finish_hosted()
            finally:
                self.execution = None
                _capacity.release()

    def finish_hosted(self, batch: dict[str, str] | None = None) -> None:
        pending, self.pending = self.pending, []
        cancelled = None
        expired = None
        for owned in pending:
            try:
                if batch is not None:
                    check_deadline(self.deadline)
            except DeadlineExceeded as exc:
                expired, batch = exc, None
            except asyncio.CancelledError as exc:
                cancelled, batch = exc, None
            value = batch.get(owned.source.span_id) if batch is not None else None
            # The SDK calls _on_ending with this exact object, before ReadableSpan creation.
            authorization = {
                id(owned.span): (owned, value, self._approved_records.get(owned.source.span_id))
            }
            token = _ending.set(authorization)
            try:
                if value is not None:
                    owned.span.set_attribute("fcg.detail.record", value)
                    check_deadline(self.deadline)
            except DeadlineExceeded as exc:
                expired, batch = exc, None
                authorization[id(owned.span)] = (owned, None, None)
            except asyncio.CancelledError as exc:
                cancelled, batch = exc, None
                authorization[id(owned.span)] = (owned, None, None)
            except Exception:
                diagnostic("instrumentation_failure")
            finally:
                try:
                    _end_span(owned.span, owned.end_time)
                except asyncio.CancelledError as exc:
                    cancelled, batch = exc, None
                finally:
                    # Clear the shared holder too: copied contexts must not retain authorization.
                    authorization.clear()
                    _ending.reset(token)
        # Cancellation still propagates, but cannot orphan later owned spans.
        if cancelled is not None:
            raise cancelled
        if expired is not None:
            raise expired

    def append(self, record: Record) -> None:
        if self.closed:
            return
        if len(encoded(record.model_dump(mode="json"))) > RECORD_BYTES:
            diagnostic("omitted_budget")
            return
        own = [r for r in self.records if r.source_runtime == record.source_runtime]
        limit = 3 if record.source_runtime == "hosted" else 5
        if not self.eligible or len(own) >= limit:
            diagnostic("omitted_budget")
            return
        # Include the serialized version, array separators and context/status overhead.
        size = len(
            encoded({"version": 1, "records": [r.model_dump(mode="json") for r in [*own, record]]})
        )
        if size > RUNTIME_BYTES:
            diagnostic("omitted_budget")
            return
        self.records.append(record)

    def release(self) -> None:
        try:
            self._release()
        except DeadlineExceeded:
            raise
        except Exception:
            diagnostic("export_failure")

    def _release(self) -> None:
        if self.closed or self.released or not self.eligible:
            return
        self.released = True
        if self.runtime == "hosted":
            check_deadline(self.deadline)
            # Nothing is attached until every candidate and original identity passes.
            batch = {}
            stages = set()
            owned = {p.source.span_id: p for p in self.pending}
            if len(owned) != len(self.pending) or len(owned) > MAX_HOSTED_SPANS:
                raise ValueError("invalid_detail_context")
            for pending in self.pending:
                _validate_pending(pending, pending.span)
                check_deadline(self.deadline)
            for record in self.records:
                wire = encoded(record.model_dump(mode="json"))
                if (
                    len(wire) > RECORD_BYTES
                    or self._approved_records.get(record.source.span_id)
                    != hashlib.sha256(wire).digest()
                ):
                    raise ValueError("invalid_detail_record")
                checked = Record.model_validate_json(wire, strict=True)
                pending = owned[checked.source.span_id]
                if checked.stage in stages or _source(pending.span) != pending.source:
                    raise ValueError("invalid_detail_context")
                _validate_original(
                    checked, pending.span.name, pending.source, pending.span.attributes
                )
                stages.add(checked.stage)
                batch[checked.source.span_id] = wire.decode("utf-8")
                check_deadline(self.deadline)
            if (
                len(
                    encoded(
                        {"version": 1, "records": [r.model_dump(mode="json") for r in self.records]}
                    )
                )
                > RUNTIME_BYTES
            ):
                raise ValueError("invalid_detail_record")
            check_deadline(self.deadline)
            self.finish_hosted(batch)
            self.records.clear()
            self._approved_records.clear()
            check_deadline(self.deadline)
            return
        from opentelemetry.trace import Link, SpanContext, TraceFlags

        from app import telemetry

        if not telemetry._enabled or telemetry._tracer is None:
            return
        # Recheck the complete batch before any irreversible content export.
        for record in self.records:
            Record.model_validate_json(encoded(record.model_dump(mode="json")), strict=True)
        tracer = _tracer()
        for record in self.records:
            if self.spans >= MAX_SPANS:
                diagnostic("omitted_budget")
                break
            self.spans += 1
            source = SpanContext(
                int(record.source.trace_id, 16),
                int(record.source.span_id, 16),
                is_remote=record.source_runtime == "hosted",
                trace_flags=TraceFlags(1),
            )
            try:
                with tracer.start_as_current_span(
                    "fcg.agent.detail",
                    links=[Link(source)],
                    record_exception=False,
                    set_status_on_exception=False,
                ) as span:
                    span.set_attribute(
                        "fcg.detail.record", encoded(record.model_dump(mode="json")).decode("utf-8")
                    )
            except Exception:
                diagnostic("export_failure")
                break


def _tracer() -> Any:
    from opentelemetry import trace

    return trace.get_tracer(SCOPE)


def current() -> Capture | None:
    capture = _current.get()
    if capture is None and (admission := _admission.get()) is not None:
        capture = admission.capture
    return capture if capture is not None and not capture.closed else None


@dataclass(slots=True)
class Admission:
    runtime: Runtime
    operation: Literal["generate", "artwork_retry"]
    deployment_version: str
    capture: Capture | None = None
    disabled: bool = False
    failed: bool = False
    spans: int = 0

    def admit(self) -> Capture | None:
        if self.disabled:
            return None
        if self.capture is None:
            if not _capacity.acquire(blocking=False):
                self.disabled = True
                diagnostic("capture_capacity")
                return None
            try:
                self.capture = Capture(
                    self.runtime,
                    operation=self.operation,
                    deployment_version=self.deployment_version,
                    deadline=_deadline.get() if self.runtime == "hosted" else None,
                    eligible=not self.failed,
                    spans=self.spans,
                )
            except BaseException:
                _capacity.release()
                raise
        return self.capture


@contextmanager
def execution(stage: Stage, *, attempt: int = 1, parent_context: Context | None = None):
    capture = current()
    admission = _admission.get()
    owner = capture if capture is not None else admission
    if owner is None or (capture is None and admission is not None and admission.disabled):
        yield None
        return
    if owner.spans >= (MAX_HOSTED_SPANS if owner.runtime == "hosted" else MAX_SPANS):
        if capture is not None:
            capture.deny()
        elif admission is not None:
            admission.disabled = True
        diagnostic("omitted_budget")
        yield None
        return
    from app import telemetry

    if not telemetry._enabled or telemetry._tracer is None:
        yield None
        return
    from opentelemetry import context as otel_context
    from opentelemetry.trace import use_span

    span = None
    state = None
    manager = None
    started = time.perf_counter()
    previous = capture.execution if capture is not None else None
    # This outer token also restores context if instrumentation fails after attaching.
    context_token = None
    try:
        try:
            context_token = otel_context.attach(otel_context.get_current())
            owner.spans += 1
            span = _tracer().start_span(NAMES[stage], context=parent_context)
            source = _source(span)
            if source is not None:
                if capture is None and admission is not None:
                    capture = admission.admit()
                if capture is not None:
                    state = Execution(
                        span=span,
                        source=source,
                        identity=_span_identity(span),
                        attempt=attempt,
                        started=started,
                    )
            manager = use_span(
                span, end_on_exit=False, record_exception=False, set_status_on_exception=False
            )
            manager.__enter__()
        except Exception:
            if admission is not None:
                admission.failed = True
            if capture is not None:
                capture.deny()
            diagnostic("instrumentation_failure")
            state = None
        if capture is not None:
            capture.execution = state
        yield state
    except asyncio.CancelledError:
        if capture is not None:
            capture.deny()
        if state is not None:
            state.outcome = "cancelled"
            state.reason = "cancelled"
        raise
    except TimeoutError:
        if state is not None:
            state.reason = "timeout"
        raise
    finally:
        end_time = time.time_ns()
        attributes = {}
        try:
            if capture is not None:
                capture.execution = previous
            if state is not None and capture is not None:
                state.duration_ms = min(3600000, int((time.perf_counter() - state.started) * 1000))
                attributes = {
                    "runtime": capture.runtime,
                    "stage": stage,
                    "agent": AGENTS[stage],
                    "operation": capture.operation,
                    "attempt": attempt,
                    "reason": state.reason,
                    "result": state.outcome,
                    "validation": state.validation,
                    "moderation": state.moderation,
                    "duration_ms": state.duration_ms,
                    "deployment_version": capture.deployment_version,
                }
                attributes = {f"fcg.detail.{key}": value for key, value in attributes.items()}
                span.set_attributes(attributes)
        except asyncio.CancelledError:
            if capture is not None:
                capture.deny()
            raise
        except Exception:
            if capture is not None:
                capture.deny()
            diagnostic("instrumentation_failure")
        finally:
            try:
                if manager is not None:
                    manager.__exit__(None, None, None)
            except asyncio.CancelledError:
                if capture is not None:
                    capture.deny()
                raise
            except Exception:
                if capture is not None:
                    capture.deny()
                diagnostic("instrumentation_failure")
            finally:
                try:
                    if context_token is not None:
                        try:
                            otel_context.detach(context_token)
                        except Exception:
                            if capture is not None:
                                capture.deny()
                            diagnostic("instrumentation_failure")
                finally:
                    if span is not None:
                        if (
                            capture is not None
                            and capture.runtime == "hosted"
                            and state is not None
                            and state.source is not None
                            and state.identity is not None
                        ):
                            capture.pending.append(
                                PendingSpan(
                                    span,
                                    state.source,
                                    stage,
                                    end_time,
                                    tuple(attributes.items()),
                                    state.identity,
                                    capture.deadline,
                                )
                            )
                        else:
                            _end_span(span, end_time)


def set_instruction(instruction: str) -> None:
    capture = current()
    if (
        capture is not None
        and capture.eligible
        and capture.execution is not None
        and capture.execution.source is not None
    ):
        capture.execution.instruction = instruction


async def candidate(
    stage: Stage,
    state: Execution | None,
    *,
    instruction: str | None,
    input_payload: dict[str, Any],
    output_payload: dict[str, Any],
    input_fields: set[str],
    output_fields: set[str],
    output_kind: str,
    deployment_version: str = "",
) -> None:
    capture = current()
    if capture is None or not capture.eligible or state is None:
        return
    if state.source is None:
        diagnostic("not_recording")
        return
    try:
        check_deadline(capture.deadline)
        if instruction is None:
            capture.deny()
            diagnostic("unvalidated_instruction")
            return
        if len(instruction) > MAX_TEXT_CHARS:
            capture.deny()
            diagnostic("omitted_budget")
            return
        trusted_instruction, input_schema, output_schema = _contracts(stage)
        if (
            instruction != trusted_instruction
            or input_fields != set(input_schema.model_fields)
            or output_fields != set(output_schema.model_fields)
            or type(input_payload) is not dict
            or type(output_payload) is not dict
            or input_payload.keys() != input_schema.model_fields.keys()
            or output_payload.keys() != output_schema.model_fields.keys()
        ):
            raise ValueError("invalid_detail_candidate")
        input_schema.model_validate(input_payload, strict=True)
        output_schema.model_validate(output_payload, strict=True)
        # Each candidate is checked independently, not authorized by the final card.
        from app.generation import HeuristicModerationService

        views = [
            text_view(instruction, trusted=True),
            projection_view(input_payload, fields=input_fields),
            projection_view(output_payload, fields=output_fields),
        ]
        check_deadline(capture.deadline)
        moderation = HeuristicModerationService("original-fantasy-v1", record_telemetry=False)
        for payload in (input_payload, output_payload):
            if type(payload) is not dict or len(payload) > 16:
                diagnostic("unvalidated")
                return
            for value in payload.values():
                if type(value) is str:
                    if len(value) > MAX_TEXT_CHARS:
                        diagnostic("omitted_budget")
                        return
                    decision = await moderation.moderate_text(value, stage="post_text")
                    check_deadline(capture.deadline)
                    if not decision.allowed or decision.reasonCode != "allowed":
                        diagnostic("suppressed_policy")
                        capture.deny()
                        return
        record = Record(
            source_runtime=capture.runtime,
            stage=stage,
            agent_name=AGENTS[stage],
            operation=capture.operation,
            attempt=state.attempt,
            source=state.source,
            duration_ms=state.duration_ms,
            instruction_digest=hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
            deployment_version=deployment_version,
            output_kind=output_kind,
            instruction=views[0],
            input=views[1],
            output=views[2],
            flags=_record_flags(*views),
            validation="modified" if views[1].flags or views[2].flags else "validated",
        )
        # Preserve metadata and instruction/input priority when escaping adds overhead.
        for name in ("output", "input", "instruction"):
            if len(encoded(record.model_dump(mode="json"))) <= RECORD_BYTES:
                break
            record = record.model_copy(update={name: View(flags=("omitted_budget",))})
            record = record.model_copy(
                update={
                    "flags": _record_flags(record.instruction, record.input, record.output),
                    "validation": (
                        "modified" if record.input.flags or record.output.flags else "validated"
                    ),
                }
            )
        if len(encoded(record.model_dump(mode="json"))) > RECORD_BYTES:
            diagnostic("omitted_budget")
            return
        check_deadline(capture.deadline)
        capture.append(record)
        if capture.runtime == "hosted" and any(item is record for item in capture.records):
            capture._approved_records[record.source.span_id] = hashlib.sha256(
                encoded(record.model_dump(mode="json"))
            ).digest()
        check_deadline(capture.deadline)
    except DeadlineExceeded:
        capture.deny()
        raise
    except (ValueError, TypeError):
        capture.deny()
        diagnostic("unvalidated")
    except Exception:
        capture.deny()
        diagnostic("capture_failure")


async def accept_hosted(envelope: Envelope | None, *, source: Source | None = None) -> None:
    capture = current()
    if capture is None or capture.runtime != "web" or envelope is None:
        return
    try:
        from app.generation import HeuristicModerationService

        moderation = HeuristicModerationService("original-fantasy-v1", record_telemetry=False)
        sources = set()
        stages = set()
        for record in envelope.records:
            Record.model_validate_json(encoded(record.model_dump(mode="json")), strict=True)
            if (
                source is None
                or record.source_runtime != "hosted"
                or record.source.trace_id != source.trace_id
                or record.source.span_id == source.span_id
                or record.source.span_id in sources
                or record.stage in stages
            ):
                raise ValueError("invalid_detail_context")
            sources.add(record.source.span_id)
            stages.add(record.stage)
            for view in (record.input, record.output):
                # Moderate decoded values, not JSON escapes supplied by the carrier.
                payload = json.loads(view.text) if view.text else {}
                for value in payload.values():
                    if type(value) is str:
                        decision = await moderation.moderate_text(value, stage="post_text")
                        if not decision.allowed or decision.reasonCode != "allowed":
                            diagnostic("suppressed_policy")
                            capture.deny()
                            return
            capture.append(record)
    except (ValueError, TypeError):
        capture.deny()
        diagnostic("invalid_extension")
    except Exception:
        capture.deny()
        diagnostic("capture_failure")


def require_allowed(decision: Any, stage: str) -> None:
    capture = current()
    if capture is not None and (
        getattr(decision, "stage", None) != stage
        or getattr(decision, "allowed", None) is not True
        or getattr(decision, "reasonCode", None) != "allowed"
    ):
        capture.deny()


def operation(runtime: Runtime):
    def decorate(function):
        @wraps(function)
        async def wrapped(self, *args, **kwargs):
            settings = self.settings if runtime == "hosted" else self.services.settings
            if not settings.agent_trace_enabled:
                # Also isolate OFF when an in-process test transport nests runtimes.
                token = _current.set(None)
                admission_token = _admission.set(None)
                try:
                    return await function(self, *args, **kwargs)
                finally:
                    _admission.reset(admission_token)
                    _current.reset(token)
            admission = Admission(
                runtime,
                operation="artwork_retry" if function.__name__ == "retry_artwork" else "generate",
                deployment_version=(
                    kwargs.get("hosted_version") or settings.version if runtime == "hosted" else ""
                ),
            )
            token = _current.set(None)
            admission_token = _admission.set(admission)
            handed_off = False
            try:
                result = await function(self, *args, **kwargs)
                capture = admission.capture
                if (
                    capture is not None
                    and getattr(result, "status", None) == "completed"
                    and capture.eligible
                    and capture.records
                ):
                    try:
                        # Validate and serialize the actual business result before release.
                        check_deadline(capture.deadline)
                        wire = result.model_dump_json()
                        check_deadline(capture.deadline)
                        validated = type(result).model_validate_json(wire)
                        check_deadline(capture.deadline)
                        if validated.status != "completed":
                            raise ValueError("response_unvalidated")
                        if runtime == "hosted":
                            _validate_safety_evidence(validated.metadata.get("safetyEvidence"))
                            check_deadline(capture.deadline)
                            capture.release()
                        elif (boundary := _boundary.get()) is not None:
                            if boundary.capture is None:
                                boundary.capture = capture
                                handed_off = True
                            else:
                                diagnostic("duplicate_operation")
                        else:
                            capture.release()
                    except DeadlineExceeded:
                        capture.deny()
                        raise
                    except Exception:
                        diagnostic("response_unvalidated")
                        check_deadline(capture.deadline)
                return result
            finally:
                try:
                    if not handed_off and admission.capture is not None:
                        admission.capture.close()
                finally:
                    admission.capture = None
                    admission.disabled = True
                    _admission.reset(admission_token)
                    _current.reset(token)

        return wrapped

    return decorate


@dataclass(slots=True)
class ReleaseBoundary:
    capture: Capture | None = None


class DetailReleaseMiddleware:
    """Release after response rendering/serialization and the final successful send."""

    def __init__(self, app: Any, *, enabled: bool = True) -> None:
        self.app = app
        self.enabled = enabled

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if not self.enabled or scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        boundary = ReleaseBoundary()
        token = _boundary.set(boundary)
        status = 0
        complete = False
        disconnected = False

        async def watched_receive():
            nonlocal disconnected
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
            return message

        async def watched_send(message):
            nonlocal status, complete
            await send(message)
            if message["type"] == "http.response.start":
                status = message["status"]
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                complete = True

        try:
            await self.app(scope, watched_receive, watched_send)
            if status == 200 and complete and not disconnected and boundary.capture is not None:
                boundary.capture.release()
        finally:
            try:
                if boundary.capture is not None:
                    boundary.capture.close()
            finally:
                _boundary.reset(token)


def _validate_original(record: Record, name: str, source: Source, attributes: Any) -> None:
    if (
        record.stage != "generation"
        or name != NAMES[record.stage]
        or record.source != source
        or record.source_runtime != "hosted"
        or attributes.get("fcg.detail.result") != "completed"
        or attributes.get("fcg.detail.reason") != "none"
        or attributes.get("fcg.detail.validation") != "validated"
    ):
        raise ValueError("invalid_detail_context")
    for key, expected in {
        "runtime": record.source_runtime,
        "stage": record.stage,
        "agent": record.agent_name,
        "operation": record.operation,
        "attempt": record.attempt,
        "duration_ms": record.duration_ms,
        "deployment_version": record.deployment_version,
    }.items():
        actual = attributes.get(f"fcg.detail.{key}")
        if type(actual) is not type(expected) or actual != expected:
            raise ValueError("invalid_detail_context")


def export_attributes(span: Any) -> dict[str, Any]:
    """Dedicated processor path; never authorizes SDK attributes, events or logs."""
    if getattr(getattr(span, "instrumentation_scope", None), "name", None) != SCOPE:
        return {}
    attributes = getattr(span, "_attributes", {}) or {}
    name = getattr(span, "name", "")
    authorization = (_ending.get() or {}).get(id(span))
    if name == "fcg.agent.detail" and authorization is None:
        value = attributes.get("fcg.detail.record")
        if type(value) is str and len(value) <= RECORD_BYTES:
            try:
                if len(value.encode("utf-8")) <= RECORD_BYTES:
                    record = Record.model_validate_json(value, strict=True)
                    return {"fcg.detail.record": encoded(record.model_dump(mode="json")).decode()}
            except (ValueError, TypeError, UnicodeError, RecursionError):
                diagnostic("invalid_export")
        return {}
    if name not in NAMES.values():
        return {}
    result = {}
    enums = {
        "runtime": {"web", "hosted"},
        "stage": STAGES,
        "result": set(get_args(ExecutionOutcome)),
        "validation": {"validated", "unvalidated"},
        "moderation": {"allowed", "blocked", "unvalidated"},
        "agent": set(AGENTS.values()),
        "operation": {"generate", "artwork_retry"},
        "reason": set(get_args(ExecutionReason)),
    }
    for key, values in enums.items():
        value = attributes.get(f"fcg.detail.{key}")
        if type(value) is str and value in values:
            result[f"fcg.detail.{key}"] = value
    duration = attributes.get("fcg.detail.duration_ms")
    if type(duration) is int and 0 <= duration <= 3600000:
        result["fcg.detail.duration_ms"] = duration
    attempt = attributes.get("fcg.detail.attempt")
    if type(attempt) is int and 1 <= attempt <= 16:
        result["fcg.detail.attempt"] = attempt
    version = attributes.get("fcg.detail.deployment_version")
    if (
        type(version) is str
        and len(version) <= 64
        and re.fullmatch(r"[A-Za-z0-9._-]*", version)
        and text_view(version) == View(text=version)
    ):
        result["fcg.detail.deployment_version"] = version
    value = attributes.get("fcg.detail.record")
    if value is not None:
        try:
            if type(value) is not str or len(value) > RECORD_BYTES:
                raise ValueError("invalid_detail_record")
            if len(value.encode("utf-8")) > RECORD_BYTES:
                raise ValueError("invalid_detail_record")
            if authorization is None:
                raise ValueError("invalid_detail_context")
            owned, accepted, approved_digest = authorization
            check_deadline(owned.deadline)
            if (
                accepted is None
                or value != accepted
                or hashlib.sha256(value.encode("utf-8")).digest() != approved_digest
            ):
                raise ValueError("invalid_detail_record")
            _validate_pending(owned, span, ending=True)
            record = Record.model_validate_json(value, strict=True)
            context = span.get_span_context()
            source = Source(trace_id=f"{context.trace_id:032x}", span_id=f"{context.span_id:016x}")
            _validate_original(record, name, source, attributes)
            wire = encoded(record.model_dump(mode="json")).decode("utf-8")
            check_deadline(owned.deadline)
            result["fcg.detail.record"] = wire
        except DeadlineExceeded:
            diagnostic("omitted_budget")
        except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
            diagnostic("invalid_export")
    return result
