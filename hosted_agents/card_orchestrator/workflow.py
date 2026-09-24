"""Request-local MAF graph; only the terminal node is output eligible."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agent_framework import (
    AgentContext,
    AgentExecutor,
    AgentExecutorRequest,
    AgentExecutorResponse,
    AgentMiddleware,
    AgentResponse,
    AgentSession,
    Executor,
    Message,
    Workflow,
    WorkflowBuilder,
    WorkflowContext,
    handler,
)
from opentelemetry import context as otel_context
from pydantic import ValidationError

from app import agent_detail
from app.completion_diagnostics import CompletionDiagnostics
from app.foundry_agent_client import GenerateCardAgentRequest, GenerateCardAgentResponse
from app.generation import GeneratedCardModel, derive_art_prompt
from app.specialist_contract import Stage
from hosted_agents.card_orchestrator.orchestrator import (
    RuntimeFailure,
    RuntimeFailureHttpStatus,
    RuntimeFailureHttpType,
    RuntimeFailureReason,
    RuntimeFailureStage,
    SafetyEvidence,
    _Stop,
    classify_runtime_failure,
)
from hosted_agents.card_orchestrator.specialists import (
    SpecialistResult,
    Specialists,
    _is_policy_error,
    specialist_result,
)

if TYPE_CHECKING:
    from hosted_agents.card_orchestrator.orchestrator import CardOrchestrator


@dataclass(frozen=True)
class ValidatedStage:
    stage: Stage
    card: GeneratedCardModel
    evidence: tuple[SafetyEvidence, ...]


@dataclass(frozen=True)
class FailureCode:
    stage: RuntimeFailureStage
    reason: RuntimeFailureReason
    http_type: RuntimeFailureHttpType
    http_status: RuntimeFailureHttpStatus

    def exception(self) -> RuntimeFailure:
        return RuntimeFailure(self.stage, self.reason, self.http_type, self.http_status)


@dataclass(frozen=True)
class TerminalOutcome:
    response: GenerateCardAgentResponse | None = None
    failure: FailureCode | None = None

    @classmethod
    def from_exception(cls, exc: Exception, state: RequestState) -> TerminalOutcome:
        if isinstance(exc, _Stop):
            return cls(
                response=GenerateCardAgentResponse(
                    schemaVersion=1, status=exc.status, safetyHints=[exc.reason]
                )
            )
        failure = classify_runtime_failure(exc, state.failure_stage)
        return cls(
            failure=FailureCode(
                failure.stage, failure.reason, failure.http_type, failure.http_status
            )
        )


@dataclass
class RequestState:
    runtime: CardOrchestrator
    version: str
    evidence: list[SafetyEvidence] = field(default_factory=list)
    card: GeneratedCardModel | None = None
    validated: ValidatedStage | None = None
    failure_stage: RuntimeFailureStage = RuntimeFailureStage.ORCHESTRATION
    acquire_requested: asyncio.Event = field(default_factory=asyncio.Event)
    resources_ready: asyncio.Event = field(default_factory=asyncio.Event)
    close_requested: asyncio.Event = field(default_factory=asyncio.Event)
    resources_closed: asyncio.Event = field(default_factory=asyncio.Event)
    specialists: Specialists | None = None
    resource_failure: RuntimeFailure | None = None
    terminal_failure: RuntimeFailure | None = None
    terminal_emitted: bool = False
    completion_diagnostics: CompletionDiagnostics | None = None
    parent_context: otel_context.Context = field(default_factory=otel_context.get_current)

    async def close_resources(self) -> None:
        self.close_requested.set()
        await self.resources_closed.wait()
        agent_detail.check_deadline(agent_detail.current_deadline())
        if self.resource_failure is not None:
            raise self.resource_failure


class DeferredSpecialist:
    """Public SupportsAgentRun adapter; SDK resources are acquired only after the gate."""

    description = None

    def __init__(self, state: RequestState, stage: Stage) -> None:
        self.state, self.stage = state, stage
        self.id = self.name = f"card_{stage}"

    def create_session(self, *, session_id: str | None = None) -> AgentSession:
        return AgentSession(session_id=session_id)

    def get_session(
        self, service_session_id: Any, *, session_id: str | None = None
    ) -> AgentSession:
        raise ValueError("history_not_supported")

    async def run(
        self,
        messages: list[Message],
        *,
        stream: bool = False,
        session: AgentSession | None = None,
        **kwargs: Any,
    ) -> AgentResponse:
        if stream or self.state.specialists is None or not self.state.resources_ready.is_set():
            raise RuntimeError("invalid_specialist_lifecycle")
        self.state.failure_stage = RuntimeFailureStage(self.stage)
        try:
            agent = self.state.specialists.agent(self.stage, StageBoundary(self.state, self.stage))
            return await agent.run(messages, stream=False, session=session, **kwargs)
        except Exception as exc:
            return AgentResponse(
                messages=[Message("assistant", ["stage_stopped"])],
                raw_representation=TerminalOutcome.from_exception(exc, self.state),
            )


class StageBoundary(AgentMiddleware):
    """Keep the original span and all stage checks in the Agent's invoking task."""

    def __init__(self, state: RequestState, stage: Stage) -> None:
        self.state, self.stage = state, stage

    async def process(
        self, context: AgentContext, call_next: Callable[[], Awaitable[None]]
    ) -> None:
        state, stage = self.state, self.stage
        state.failure_stage = RuntimeFailureStage(stage)
        try:
            if (
                context.stream
                or len(context.messages) != 1
                or context.messages[0].role != "user"
                or len(context.messages[0].contents) != 1
                or context.messages[0].contents[0].type != "text"
            ):
                raise _Stop("held", "invalid_evidence")
            try:
                payload = json.loads(context.messages[0].text)
                request = GenerateCardAgentRequest.model_validate(payload, strict=True)
                expected = {"query": request.query}
                if payload != expected:
                    raise _Stop("held", "invalid_evidence")
            except (ValueError, TypeError):
                raise _Stop("held", "schema_invalid") from None
            card = await state.runtime._run_stage(
                stage,
                payload,
                state.card,
                state.evidence,
                state.version,
                lambda: self.invoke(context, call_next),
                parent_context=state.parent_context,
            )
            value: ValidatedStage | TerminalOutcome = ValidatedStage(
                stage, card, tuple(state.evidence)
            )
            state.validated = value
        except Exception as exc:
            value = TerminalOutcome.from_exception(exc, state)
        context.result = AgentResponse(
            messages=[Message("assistant", ["stage_completed"])],
            raw_representation=value,
        )

    async def invoke(
        self, context: AgentContext, call_next: Callable[[], Awaitable[None]]
    ) -> SpecialistResult:
        try:
            await call_next()
        except Exception as exc:
            if _is_policy_error(exc):
                return SpecialistResult("refused", reason="model_content_filter")
            raise
        if not isinstance(context.result, AgentResponse):
            result = SpecialistResult(
                "held",
                reason="model_incomplete",
                completion_diagnostics=CompletionDiagnostics(
                    stage=self.stage, checker="unexpected_agent_response"
                ),
            )
        else:
            result = specialist_result(context.result, self.stage)
        self.state.completion_diagnostics = result.completion_diagnostics
        return result


class DecodeTypedRequest(Executor):
    def __init__(self, state: RequestState) -> None:
        super().__init__("decode")
        self.state = state

    @handler
    async def decode(
        self,
        messages: list[Message],
        ctx: WorkflowContext[GenerateCardAgentRequest | TerminalOutcome],
    ) -> None:
        if (
            len(messages) != 1
            or messages[0].role != "user"
            or len(messages[0].contents) != 1
            or messages[0].contents[0].type != "text"
        ):
            await ctx.send_message(
                TerminalOutcome.from_exception(_Stop("held", "invalid_evidence"), self.state)
            )
            return
        try:
            request = GenerateCardAgentRequest.model_validate_json(messages[0].text, strict=True)
        except ValidationError:
            await ctx.send_message(
                TerminalOutcome.from_exception(_Stop("held", "schema_invalid"), self.state)
            )
            return
        await ctx.send_message(request)


def specialist_request(payload: dict[str, Any]) -> AgentExecutorRequest:
    return AgentExecutorRequest(messages=[Message("user", [json.dumps(payload)])])


class PrePromptSafety(Executor):
    def __init__(self, state: RequestState) -> None:
        super().__init__("pre_prompt")
        self.state = state

    @handler
    async def gate(
        self,
        request: GenerateCardAgentRequest,
        ctx: WorkflowContext[AgentExecutorRequest | TerminalOutcome],
    ) -> None:
        state = self.state
        try:
            await state.runtime._gate(
                request.query, "pre_prompt", "pre_prompt", state.evidence, state.version
            )
            state.failure_stage = RuntimeFailureStage.SPECIALIST_SETUP
            state.acquire_requested.set()
            await state.resources_ready.wait()
            agent_detail.check_deadline(agent_detail.current_deadline())
            if state.resource_failure is not None:
                raise state.resource_failure
        except Exception as exc:
            await ctx.send_message(TerminalOutcome.from_exception(exc, state))
            return
        await ctx.send_message(specialist_request({"query": request.query}))


class MergeAndGate(Executor):
    def __init__(self, state: RequestState, stage: Stage) -> None:
        super().__init__(f"{stage}_merge")
        self.state, self.stage = state, stage

    @handler
    async def merge(
        self,
        response: AgentExecutorResponse,
        ctx: WorkflowContext[ValidatedStage | TerminalOutcome],
    ) -> None:
        state = self.state
        value = response.agent_response.raw_representation
        if isinstance(value, TerminalOutcome) and response.executor_id == self.stage:
            await ctx.send_message(value)
            return
        required = ("pre_prompt", "generation")
        if (
            response.executor_id != self.stage
            or not isinstance(value, ValidatedStage)
            or value != state.validated
            or value.stage != self.stage
            or value.evidence != tuple(state.evidence)
            or tuple(e.stage for e in value.evidence) != required
            or any(
                e.decision != "allowed"
                or e.reason != "allowed"
                or e.policy != "original-fantasy-v1"
                for e in value.evidence
            )
        ):
            await ctx.send_message(
                TerminalOutcome.from_exception(_Stop("held", "invalid_evidence"), state)
            )
            return
        try:
            state.card = GeneratedCardModel.model_validate(value.card.model_dump(), strict=True)
        except ValidationError:
            await ctx.send_message(
                TerminalOutcome.from_exception(_Stop("held", "schema_invalid"), state)
            )
            return
        state.validated = None
        await ctx.send_message(value)


class FinalTextAndArtSafety(Executor):
    def __init__(self, state: RequestState) -> None:
        super().__init__("final_safety")
        self.state = state

    @handler
    async def gate(self, value: ValidatedStage, ctx: WorkflowContext[TerminalOutcome]) -> None:
        state = self.state
        state.failure_stage = RuntimeFailureStage.ORCHESTRATION
        try:
            await state.close_resources()
            if value.stage != "generation" or value.card != state.card:
                raise _Stop("held", "invalid_evidence")
            art_prompt = derive_art_prompt(value.card)
            await state.runtime._gate(
                value.card.model_dump_json(),
                "post_text",
                "final_text",
                state.evidence,
                state.version,
            )
            await state.runtime._gate(
                art_prompt, "post_art_prompt", "final_art_prompt", state.evidence, state.version
            )
            required = ("pre_prompt", "generation", "final_text", "final_art_prompt")
            if tuple(e.stage for e in state.evidence) != required or any(
                e.decision != "allowed"
                or e.reason != "allowed"
                or e.policy != "original-fantasy-v1"
                for e in state.evidence
            ):
                raise _Stop("held", "invalid_evidence")
            outcome = TerminalOutcome(
                response=GenerateCardAgentResponse(
                    schemaVersion=1, status="completed", card=value.card, artPrompt=art_prompt
                )
            )
        except Exception as exc:
            outcome = TerminalOutcome.from_exception(exc, state)
        await ctx.send_message(outcome)


class TerminalEnvelope(Executor):
    def __init__(self, state: RequestState) -> None:
        super().__init__("terminal")
        self.state = state

    @handler
    async def emit(self, outcome: TerminalOutcome, ctx: WorkflowContext[None, str]) -> None:
        state = self.state
        try:
            await state.close_resources()
        except Exception as exc:
            outcome = TerminalOutcome.from_exception(exc, state)
        if state.terminal_emitted or (outcome.response is None) == (outcome.failure is None):
            raise ValueError("invalid_terminal_outcome")
        state.terminal_failure = outcome.failure.exception() if outcome.failure else None
        wire = (
            outcome.response.model_dump_json()
            if outcome.response is not None
            else '{"runtimeFailure":true}'
        )
        agent_detail.check_deadline(agent_detail.current_deadline())
        state.terminal_emitted = True
        await ctx.yield_output(wire)


def build_workflow(state: RequestState) -> Workflow:
    decode = DecodeTypedRequest(state)
    pre_prompt = PrePromptSafety(state)
    generation = AgentExecutor(DeferredSpecialist(state, "generation"), id="generation")
    generation_merge = MergeAndGate(state, "generation")
    final = FinalTextAndArtSafety(state)
    terminal = TerminalEnvelope(state)
    return (
        WorkflowBuilder(start_executor=decode, output_from=[terminal])
        .add_edge(
            decode, pre_prompt, condition=lambda value: isinstance(value, GenerateCardAgentRequest)
        )
        .add_edge(decode, terminal, condition=lambda value: isinstance(value, TerminalOutcome))
        .add_edge(
            pre_prompt,
            generation,
            condition=lambda value: isinstance(value, AgentExecutorRequest),
        )
        .add_edge(pre_prompt, terminal, condition=lambda value: isinstance(value, TerminalOutcome))
        .add_edge(generation, generation_merge)
        .add_edge(
            generation_merge,
            final,
            condition=lambda value: isinstance(value, ValidatedStage),
        )
        .add_edge(
            generation_merge,
            terminal,
            condition=lambda value: isinstance(value, TerminalOutcome),
        )
        .add_edge(final, terminal)
        .build()
    )
