from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Literal, Protocol

from agent_framework import Agent, AgentMiddleware, AgentResponse, ChatResponse, Content
from agent_framework.exceptions import ChatClientContentFilterException
from agent_framework.foundry import FoundryChatClient
from azure.ai.projects.aio import AIProjectClient
from azure.identity.aio import DefaultAzureCredential
from openai import APIStatusError
from openai.types.responses import Response
from openai.types.responses.response import IncompleteDetails

from app.completion_diagnostics import Checker, CompletionDiagnostics
from app.specialist_contract import SCHEMAS, Stage, effective_instructions, effective_schema
from hosted_agents.card_orchestrator.settings import RuntimeSettings


@dataclass(frozen=True)
class SpecialistResult:
    status: Literal["completed", "refused", "held", "routing_defer"]
    text: str = ""
    reason: str = "model_output"
    completion_diagnostics: CompletionDiagnostics | None = None


class Specialists(Protocol):
    def agent(self, stage: Stage, middleware: AgentMiddleware) -> Agent: ...


class FoundrySpecialists:
    def __init__(self, client: FoundryChatClient) -> None:
        self.client = client

    def agent(self, stage: Stage, middleware: AgentMiddleware) -> Agent:
        schema = SCHEMAS[stage]
        return Agent(
            self.client,
            name=f"card_{stage}",
            instructions=effective_instructions(stage),
            middleware=[middleware],
            default_options={
                "store": False,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema.__name__,
                        "strict": True,
                        "schema": effective_schema(stage),
                    },
                },
                "max_tokens": 1800,
            },
        )


def _incomplete_result(response: AgentResponse, stage: Stage, checker: Checker) -> SpecialistResult:
    value: dict[str, object] = {
        "stage": stage,
        "checker": checker,
        "finishReason": response.finish_reason,
    }
    usage = response.usage_details
    if type(usage) is dict:
        value["usage"] = {
            "inputTokens": usage.get("input_token_count"),
            "outputTokens": usage.get("output_token_count"),
            "totalTokens": usage.get("total_token_count"),
        }
    chat = response.raw_representation
    if isinstance(chat, ChatResponse) and isinstance(chat.raw_representation, Response):
        incomplete = chat.raw_representation.incomplete_details
        if isinstance(incomplete, IncompleteDetails):
            value["incompleteReason"] = incomplete.reason
    return SpecialistResult(
        "held",
        reason="model_incomplete",
        completion_diagnostics=CompletionDiagnostics.parse(value),
    )


def specialist_result(response: AgentResponse, stage: Stage) -> SpecialistResult:
    # Provider refusal remains authoritative even when valid JSON is also present.
    raw_response = getattr(response.raw_representation, "raw_representation", None)
    error = getattr(raw_response, "error", None)
    if getattr(error, "code", None) in {"content_filter", "ResponsibleAIPolicyViolation"}:
        return SpecialistResult("refused", reason="model_content_filter")
    if str(response.finish_reason) == "content_filter":
        return SpecialistResult("refused", reason="model_content_filter")
    for message in response.messages:
        for content in message.contents:
            if (
                type(content) is Content
                and getattr(content.raw_representation, "type", None) == "refusal"
            ):
                return SpecialistResult("refused", reason="model_refusal")
    if error is not None or getattr(raw_response, "status", None) == "failed":
        raise RuntimeError("model_failed")
    if raw_response is None:
        return _incomplete_result(response, stage, "missing_raw_response")
    if str(response.finish_reason) != "stop":
        return _incomplete_result(response, stage, "non_stop_finish")
    if any(
        type(content) is not Content or content.type not in {"text", "text_reasoning"}
        for message in response.messages
        for content in message.contents
    ):
        return _incomplete_result(response, stage, "non_text_content")
    return SpecialistResult("completed", text=response.text)


def _is_policy_error(exc: BaseException) -> bool:
    for _ in range(5):
        if isinstance(exc, ChatClientContentFilterException):
            return True
        if isinstance(exc, APIStatusError):
            body = exc.body if isinstance(exc.body, dict) else {}
            error = body.get("error", body)
            if isinstance(error, dict) and error.get("code") in {
                "content_filter",
                "ResponsibleAIPolicyViolation",
            }:
                return True
        if exc.__cause__ is None:
            break
        exc = exc.__cause__
    return False


@asynccontextmanager
async def create_specialists(settings: RuntimeSettings) -> AsyncIterator[FoundrySpecialists]:
    async with AsyncExitStack() as stack:
        credential = await stack.enter_async_context(
            DefaultAzureCredential(exclude_interactive_browser_credential=True)
        )
        project = await stack.enter_async_context(
            AIProjectClient(
                endpoint=settings.project_endpoint,
                credential=credential,
                retry_total=0,
                logging_enable=False,
            )
        )
        client = FoundryChatClient(
            project_client=project,
            model=settings.model_deployment,
            function_invocation_configuration={
                "enabled": False,
                "max_iterations": 1,
                "terminate_on_unknown_calls": True,
            },
        )
        # Public OpenAI client settings; no hidden SDK retries beyond the stage budget.
        client.client.max_retries = 0
        client.client.timeout = settings.stage_timeout_seconds
        stack.push_async_callback(client.client.close)
        yield FoundrySpecialists(client)
