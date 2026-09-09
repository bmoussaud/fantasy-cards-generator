from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import suppress
from typing import Any

from agent_framework.observability import disable_instrumentation
from azure.ai.agentserver.responses import (
    InMemoryResponseProvider,
    ResponseEventStream,
    ResponsesAgentServerHost,
    ResponsesServerOptions,
    TextResponse,
)
from pydantic import ValidationError
from starlette.responses import JSONResponse

from app.foundry_agent_client import GenerateCardAgentRequest
from hosted_agents.card_orchestrator.orchestrator import CardOrchestrator, RuntimeFailure
from hosted_agents.card_orchestrator.settings import RuntimeSettings

MAX_BODY_BYTES = 8192
SAFE_TOP_LEVEL_FIELDS = {
    "agent",
    "agent_reference",
    "background",
    "conversation",
    "instructions",
    "max_output_tokens",
    "metadata",
    "model",
    "previous_response_id",
    "prompt",
    "response_id",
    "store",
    "stream",
    "text",
    "tool_choice",
    "tools",
}


class WireValidationError(ValueError):
    def __init__(self, reason: str, field: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.field = field


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _validate_agent_reference(value: Any) -> None:
    if (
        not isinstance(value, dict)
        or set(value) - {"type", "name", "version"}
        or value.get("type") != "agent_reference"
        or not isinstance(value.get("name"), str)
        or not value["name"].strip()
        or ("version" in value and not isinstance(value["version"], str))
    ):
        raise WireValidationError("invalid_value", "agent_reference")


def validate_wire(body: bytes) -> GenerateCardAgentRequest:
    try:
        data = json.loads(body, object_pairs_hook=_unique_object)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise WireValidationError("invalid_json", "body") from None
    if not isinstance(data, dict):
        raise WireValidationError("invalid_object", "body")
    unexpected = set(data) - {
        "store",
        "stream",
        "input",
        "metadata",
        "agent_reference",
        "agent_session_id",
    }
    if unexpected:
        field = next(iter(unexpected)) if len(unexpected) == 1 else "top_level"
        if field not in SAFE_TOP_LEVEL_FIELDS:
            field = "top_level"
        raise WireValidationError("unsupported_field", field)
    if data.get("store") is not False:
        raise WireValidationError("not_false", "store")
    if data.get("stream") is not False:
        raise WireValidationError("not_false", "stream")
    if "agent_session_id" in data and (
        not isinstance(data["agent_session_id"], str)
        or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", data["agent_session_id"])
    ):
        raise WireValidationError("invalid_value", "agent_session_id")
    if "agent_reference" in data:
        _validate_agent_reference(data["agent_reference"])
    metadata = data.get("metadata", {})
    if (
        not isinstance(metadata, dict)
        or set(metadata) - {"request_id", "trace_id"}
        or any(
            not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", v)
            for v in metadata.values()
        )
    ):
        raise WireValidationError("invalid_value", "metadata")
    items = data.get("input")
    if not isinstance(items, list) or len(items) != 1:
        raise WireValidationError("not_single_item_list", "input")
    message = items[0]
    if (
        not isinstance(message, dict)
        or set(message) - {"role", "content", "type"}
        or message.get("role") != "user"
        or message.get("type", "message") != "message"
    ):
        raise WireValidationError("invalid_user_message", "input")
    content = message.get("content")
    if not isinstance(content, list) or len(content) != 1:
        raise WireValidationError("not_single_item_list", "content")
    part = content[0]
    if (
        not isinstance(part, dict)
        or set(part) != {"type", "text"}
        or part["type"] != "input_text"
        or not isinstance(part["text"], str)
    ):
        raise WireValidationError("invalid_input_text", "content")
    try:
        request = json.loads(part["text"], object_pairs_hook=_unique_object)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise WireValidationError("invalid_json", "domain") from None
    if not isinstance(request, dict) or request.get("schemaVersion") != 1:
        raise WireValidationError("invalid_schema", "domain")
    if type(request["schemaVersion"]) is not int:
        raise WireValidationError("invalid_schema", "domain")
    try:
        return GenerateCardAgentRequest.model_validate(request, strict=True)
    except ValidationError:
        raise WireValidationError("invalid_schema", "domain") from None


class StatelessBoundary:
    """Validate before SDK normalization or any persistence/history dispatch."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"] != "/responses" or scope["method"] != "POST":
            if scope["path"] == "/readiness" and scope["method"] == "GET":
                await JSONResponse({"status": "ready", "cloudProbe": False})(scope, receive, send)
            else:
                await JSONResponse({"error": {"code": "not_found"}}, status_code=404)(
                    scope, receive, send
                )
            return
        body = bytearray()
        try:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > MAX_BODY_BYTES:
                    raise WireValidationError("invalid_value", "body")
                if not message.get("more_body", False):
                    break
            request = validate_wire(bytes(body))
        except WireValidationError as error:
            await JSONResponse(
                {
                    "error": {
                        "code": "card_boundary_invalid_request",
                        "message": "Invalid card request.",
                        "reason": error.reason,
                        "param": error.field,
                    }
                },
                status_code=400,
            )(scope, receive, send)
            return
        except (TypeError, UnicodeError, RecursionError):
            await JSONResponse(
                {
                    "error": {
                        "code": "card_boundary_invalid_request",
                        "message": "Invalid card request.",
                        "reason": "invalid_value",
                        "param": "body",
                    }
                },
                status_code=400,
            )(scope, receive, send)
            return

        # Routing metadata never reaches the model or enables SDK conversation state.
        canonical = json.dumps(
            {
                "store": False,
                "stream": False,
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": request.model_dump_json()}],
                    }
                ],
            }
        ).encode()
        delivered = False

        async def replay() -> dict[str, Any]:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": canonical, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class NoResponseStore(InMemoryResponseProvider):
    """Fail closed if an SDK upgrade attempts response persistence."""

    async def create_response(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("persistence_disabled")

    async def update_response(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("persistence_disabled")


def _disable_payload_telemetry() -> None:
    disable_instrumentation()
    # SDK error/debug paths may include model text. This standalone process exports no SDK logs.
    for name in (
        "azure",
        "agent_framework",
        "agent_framework_foundry",
        "agent_framework_openai",
        "openai",
        "httpx",
        "httpcore",
        "msal",
    ):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False


async def _with_cancellation(operation: Any, *signals: asyncio.Event) -> Any:
    task = asyncio.create_task(operation)
    watchers = [asyncio.create_task(signal.wait()) for signal in signals]
    try:
        done, _ = await asyncio.wait([task, *watchers], return_when=asyncio.FIRST_COMPLETED)
        if any(watcher in done for watcher in watchers):
            raise asyncio.CancelledError
        return await task
    finally:
        for pending in [task, *watchers]:
            if not pending.done():
                pending.cancel()
        for pending in [task, *watchers]:
            with suppress(asyncio.CancelledError, RuntimeFailure):
                await pending


def create_host(
    settings: RuntimeSettings, *, orchestrator: CardOrchestrator | None = None
) -> ResponsesAgentServerHost:
    _disable_payload_telemetry()
    host = ResponsesAgentServerHost(
        store=NoResponseStore(),
        options=ResponsesServerOptions(
            default_model="card-orchestrator",
            resilient_background=False,
            steerable_conversations=False,
        ),
        configure_observability=None,
        access_log=None,
    )
    host.add_middleware(StatelessBoundary)
    runtime = orchestrator or CardOrchestrator(settings)

    @host.response_handler
    async def respond(request, context, cancellation_signal):
        # SDK CreateResponse normalizes message types; validate the domain object again.
        query = GenerateCardAgentRequest.model_validate_json(
            request["input"][0]["content"][0]["text"]
        )
        version = host.config.agent_version
        hosted_version = version if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", version) else None
        try:
            response = await _with_cancellation(
                runtime.generate(query, hosted_version=hosted_version),
                cancellation_signal,
                context.shutdown,
            )
        except RuntimeFailure:

            async def failed():
                stream = ResponseEventStream(response_id=context.response_id, request=request)
                yield stream.emit_created()
                yield stream.emit_failed(
                    code="server_error", message="Card generation unavailable."
                )

            return failed()
        return TextResponse(context, request, text=response.model_dump_json())

    return host
