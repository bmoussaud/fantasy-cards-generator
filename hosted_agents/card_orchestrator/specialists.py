from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from agent_framework import Agent
from agent_framework.exceptions import ChatClientContentFilterException
from agent_framework.foundry import FoundryChatClient
from azure.ai.projects.aio import AIProjectClient
from azure.identity.aio import DefaultAzureCredential
from openai import APIStatusError

from app.agent_detail import set_instruction
from app.specialist_contract import SCHEMAS, Stage, effective_instructions
from hosted_agents.card_orchestrator.settings import RuntimeSettings


@dataclass(frozen=True)
class SpecialistResult:
    status: Literal["completed", "refused", "held", "routing_defer"]
    text: str = ""
    reason: str = "model_output"


class Specialists(Protocol):
    async def run(self, stage: Stage, payload: dict[str, Any]) -> SpecialistResult: ...


class FoundrySpecialists:
    def __init__(self, client: FoundryChatClient) -> None:
        self.client = client

    async def run(self, stage: Stage, payload: dict[str, Any]) -> SpecialistResult:
        schema = SCHEMAS[stage]
        instructions = effective_instructions(stage)
        set_instruction(instructions)
        agent = Agent(
            self.client,
            name=f"card_{stage}",
            instructions=instructions,
        )
        try:
            response = await agent.run(
                json.dumps(payload),
                session=agent.create_session(),
                stream=False,
                options={
                    "store": False,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": schema.__name__,
                            "strict": True,
                            "schema": schema.model_json_schema(),
                        },
                    },
                    "max_output_tokens": 1800,
                },
            )
        except Exception as exc:
            if _is_policy_error(exc):
                return SpecialistResult("refused", reason="model_content_filter")
            raise

        # Framework preserves authoritative refusal parts independently of output JSON.
        raw_response = getattr(response.raw_representation, "raw_representation", None)
        error = getattr(raw_response, "error", None)
        if getattr(error, "code", None) in {"content_filter", "ResponsibleAIPolicyViolation"}:
            return SpecialistResult("refused", reason="model_content_filter")
        if str(response.finish_reason) == "content_filter":
            return SpecialistResult("refused", reason="model_content_filter")
        for message in response.messages:
            for content in message.contents:
                raw = content.raw_representation
                if getattr(raw, "type", None) == "refusal":
                    return SpecialistResult("refused", reason="model_refusal")
        if error is not None or getattr(raw_response, "status", None) == "failed":
            raise RuntimeError("model_failed")
        if raw_response is None:
            return SpecialistResult("held", reason="model_incomplete")
        if str(response.finish_reason) != "stop":
            return SpecialistResult("held", reason="model_incomplete")
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
