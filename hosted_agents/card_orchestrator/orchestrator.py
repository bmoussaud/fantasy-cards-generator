from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from enum import StrEnum
from typing import Literal

from agent_framework.exceptions import (
    ChatClientInvalidAuthException,
    ChatClientInvalidRequestException,
    ChatClientInvalidResponseException,
    IntegrationInvalidAuthException,
    IntegrationInvalidRequestException,
    IntegrationInvalidResponseException,
)
from azure.core.exceptions import (
    ClientAuthenticationError,
    ServiceRequestError,
    ServiceResponseError,
)
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
)
from pydantic import BaseModel, ConfigDict, ValidationError

from app.foundry_agent_client import GenerateCardAgentRequest, GenerateCardAgentResponse
from app.generation import (
    GeneratedCardModel,
    HeuristicModerationService,
    ModerationDecision,
    derive_art_prompt,
)
from app.telemetry import instrument_generation, record_dependency_attempt, record_moderation
from hosted_agents.card_orchestrator.settings import POLICY, RuntimeSettings
from hosted_agents.card_orchestrator.specialists import SCHEMAS, Specialists, create_specialists

REASONS = {
    "allowed",
    "living-artist-imitation",
    "copyrighted-logo",
    "trademark-request",
    "copyrighted-character",
    "graphic-violence",
    "sexual-content-minor",
    "self-harm",
}


class SafetyEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    stage: Literal[
        "pre_prompt",
        "concept",
        "lore",
        "final_text",
        "final_art_prompt",
        "hosted_guardrails",
        "post_image",
    ]
    policy: Literal["original-fantasy-v1", "hosted_guardrails", "post_image"]
    decision: Literal["allowed", "blocked", "unavailable", "not_applicable"]
    reason: Literal[
        "allowed",
        "living-artist-imitation",
        "copyrighted-logo",
        "trademark-request",
        "copyrighted-character",
        "graphic-violence",
        "sexual-content-minor",
        "self-harm",
        "not_observed",
        "text_only",
        "invalid_evidence",
    ]


class RuntimeFailureStage(StrEnum):
    SPECIALIST_SETUP = "specialist_setup"
    CONCEPT = "concept"
    LORE = "lore"
    ART_DIRECTION = "art_direction"
    ORCHESTRATION = "orchestration"


class RuntimeFailureReason(StrEnum):
    TIMEOUT = "timeout"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    RESOURCE_NOT_FOUND = "resource_not_found"
    INVALID_REQUEST = "invalid_request"
    RATE_LIMITED = "rate_limited"
    SERVICE_ERROR = "service_error"
    TRANSPORT_ERROR = "transport_error"
    INVALID_RESPONSE = "invalid_response"
    DEPENDENCY_ERROR = "dependency_error"


class RuntimeFailureHttpType(StrEnum):
    NONE = "none"
    BAD_REQUEST = "bad_request"
    AUTHENTICATION = "authentication"
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    UNPROCESSABLE = "unprocessable"
    RATE_LIMIT = "rate_limit"
    SERVER = "server"
    API_STATUS = "api_status"


class RuntimeFailureHttpStatus(StrEnum):
    NONE = "none"
    HTTP_400 = "http_400"
    HTTP_401 = "http_401"
    HTTP_403 = "http_403"
    HTTP_404 = "http_404"
    HTTP_408 = "http_408"
    HTTP_409 = "http_409"
    HTTP_422 = "http_422"
    HTTP_429 = "http_429"
    HTTP_500 = "http_500"
    HTTP_502 = "http_502"
    HTTP_503 = "http_503"
    HTTP_504 = "http_504"
    HTTP_OTHER = "http_other"


_HTTP_STATUS = {
    400: RuntimeFailureHttpStatus.HTTP_400,
    401: RuntimeFailureHttpStatus.HTTP_401,
    403: RuntimeFailureHttpStatus.HTTP_403,
    404: RuntimeFailureHttpStatus.HTTP_404,
    408: RuntimeFailureHttpStatus.HTTP_408,
    409: RuntimeFailureHttpStatus.HTTP_409,
    422: RuntimeFailureHttpStatus.HTTP_422,
    429: RuntimeFailureHttpStatus.HTTP_429,
    500: RuntimeFailureHttpStatus.HTTP_500,
    502: RuntimeFailureHttpStatus.HTTP_502,
    503: RuntimeFailureHttpStatus.HTTP_503,
    504: RuntimeFailureHttpStatus.HTTP_504,
}


class RuntimeFailure(Exception):
    """A payload-free, closed-enum dependency failure."""

    def __init__(
        self,
        stage: RuntimeFailureStage | str,
        reason: RuntimeFailureReason | str,
        http_type: RuntimeFailureHttpType | str = RuntimeFailureHttpType.NONE,
        http_status: RuntimeFailureHttpStatus | str = RuntimeFailureHttpStatus.NONE,
    ) -> None:
        self.stage = RuntimeFailureStage(stage)
        self.reason = RuntimeFailureReason(reason)
        self.http_type = RuntimeFailureHttpType(http_type)
        self.http_status = RuntimeFailureHttpStatus(http_status)
        self.error_code = self.reason.value
        super().__init__("dependency_failure")

    @property
    def response_code(self) -> str:
        return ":".join(
            (
                "card_runtime",
                self.stage.value,
                self.reason.value,
                self.http_type.value,
                self.http_status.value,
            )
        )


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    while id(exc) not in seen and len(chain) < 8:
        seen.add(id(exc))
        chain.append(exc)
        next_exc = exc.__cause__ or exc.__context__
        if next_exc is None:
            break
        exc = next_exc
    return chain


def _http_failure(status: int) -> tuple[
    RuntimeFailureReason,
    RuntimeFailureHttpType,
    RuntimeFailureHttpStatus,
]:
    http_status = _HTTP_STATUS.get(status, RuntimeFailureHttpStatus.HTTP_OTHER)
    if status == 400:
        return RuntimeFailureReason.INVALID_REQUEST, RuntimeFailureHttpType.BAD_REQUEST, http_status
    if status == 401:
        return (
            RuntimeFailureReason.AUTHENTICATION,
            RuntimeFailureHttpType.AUTHENTICATION,
            http_status,
        )
    if status == 403:
        return (
            RuntimeFailureReason.AUTHORIZATION,
            RuntimeFailureHttpType.PERMISSION_DENIED,
            http_status,
        )
    if status == 404:
        return (
            RuntimeFailureReason.RESOURCE_NOT_FOUND,
            RuntimeFailureHttpType.NOT_FOUND,
            http_status,
        )
    if status == 408:
        return RuntimeFailureReason.TIMEOUT, RuntimeFailureHttpType.API_STATUS, http_status
    if status == 409:
        return RuntimeFailureReason.INVALID_REQUEST, RuntimeFailureHttpType.CONFLICT, http_status
    if status == 422:
        return (
            RuntimeFailureReason.INVALID_REQUEST,
            RuntimeFailureHttpType.UNPROCESSABLE,
            http_status,
        )
    if status == 429:
        return RuntimeFailureReason.RATE_LIMITED, RuntimeFailureHttpType.RATE_LIMIT, http_status
    if status >= 500:
        return RuntimeFailureReason.SERVICE_ERROR, RuntimeFailureHttpType.SERVER, http_status
    return RuntimeFailureReason.DEPENDENCY_ERROR, RuntimeFailureHttpType.API_STATUS, http_status


def classify_runtime_failure(exc: BaseException, stage: RuntimeFailureStage) -> RuntimeFailure:
    if isinstance(exc, RuntimeFailure):
        return exc
    chain = _exception_chain(exc)
    for current in chain:
        if isinstance(current, APIStatusError):
            reason, http_type, http_status = _http_failure(current.status_code)
            return RuntimeFailure(stage, reason, http_type, http_status)
    if any(isinstance(current, (TimeoutError, APITimeoutError)) for current in chain):
        return RuntimeFailure(stage, RuntimeFailureReason.TIMEOUT)
    if any(
        isinstance(
            current,
            (
                ClientAuthenticationError,
                ChatClientInvalidAuthException,
                IntegrationInvalidAuthException,
            ),
        )
        for current in chain
    ):
        return RuntimeFailure(stage, RuntimeFailureReason.AUTHENTICATION)
    if any(
        isinstance(
            current,
            (ChatClientInvalidRequestException, IntegrationInvalidRequestException),
        )
        for current in chain
    ):
        return RuntimeFailure(stage, RuntimeFailureReason.INVALID_REQUEST)
    if any(
        isinstance(
            current,
            (
                ChatClientInvalidResponseException,
                IntegrationInvalidResponseException,
            ),
        )
        for current in chain
    ):
        return RuntimeFailure(stage, RuntimeFailureReason.INVALID_RESPONSE)
    if any(
        isinstance(current, (APIConnectionError, ServiceRequestError, ServiceResponseError))
        for current in chain
    ):
        return RuntimeFailure(stage, RuntimeFailureReason.TRANSPORT_ERROR)
    return RuntimeFailure(stage, RuntimeFailureReason.DEPENDENCY_ERROR)


class _Stop(Exception):
    def __init__(self, status: str, reason: str) -> None:
        self.status = status
        self.reason = reason


class CardOrchestrator:
    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        specialist_factory: Callable[
            [RuntimeSettings], AbstractAsyncContextManager[Specialists]
        ] = create_specialists,
        moderation: HeuristicModerationService | None = None,
    ) -> None:
        self.settings = settings
        self.specialist_factory = specialist_factory
        self.moderation = moderation or HeuristicModerationService(
            settings.moderation_policy,
            record_telemetry=False,
        )

    @instrument_generation("generate")
    async def generate(
        self, request: GenerateCardAgentRequest, *, hosted_version: str | None = None
    ) -> GenerateCardAgentResponse:
        telemetry_version = hosted_version or self.settings.version
        evidence: list[SafetyEvidence] = []
        metadata = {
            "agentVersion": self.settings.version,
            "candidate": "offline",
        }
        if hosted_version:
            metadata["hostedVersion"] = hosted_version
        failure_stage = RuntimeFailureStage.ORCHESTRATION
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                await self._gate(
                    request.query,
                    "pre_prompt",
                    "pre_prompt",
                    evidence,
                    telemetry_version,
                )
                failure_stage = RuntimeFailureStage.SPECIALIST_SETUP
                async with self.specialist_factory(self.settings) as specialists:
                    card = None
                    for stage in ("concept", "lore", "art_direction"):
                        failure_stage = RuntimeFailureStage(stage)
                        payload = (
                            {"query": request.query}
                            if card is None
                            else {"card": card.model_dump()}
                        )
                        started = time.perf_counter()
                        try:
                            async with asyncio.timeout(self.settings.stage_timeout_seconds):
                                result = await specialists.run(stage, payload)
                        except Exception as exc:
                            failure = classify_runtime_failure(exc, failure_stage)
                            record_dependency_attempt(
                                dependency="foundry_text",
                                attempt=1,
                                outcome=_dependency_failure_outcome(failure),
                                duration_ms=(time.perf_counter() - started) * 1000,
                                request_id=None,
                                error_code=failure.reason.value,
                                retryable=False,
                                stage=stage,
                                agent_version=telemetry_version,
                            )
                            raise failure from None
                        record_dependency_attempt(
                            dependency="foundry_text",
                            attempt=1,
                            outcome=(
                                "completed"
                                if result.status == "completed"
                                else "blocked" if result.status == "refused" else "failed"
                            ),
                            duration_ms=(time.perf_counter() - started) * 1000,
                            request_id=None,
                            error_code="none",
                            retryable=False,
                            stage=stage,
                            agent_version=telemetry_version,
                        )
                        if result.status != "completed":
                            if result.status not in {"refused", "held", "routing_defer"}:
                                raise _Stop("held", "invalid_stage_status")
                            reason = (
                                result.reason
                                if result.reason
                                in {
                                    "model_refusal",
                                    "model_content_filter",
                                    "model_incomplete",
                                }
                                else "stage_not_completed"
                            )
                            raise _Stop(result.status, reason)
                        try:
                            refinement = SCHEMAS[stage].model_validate_json(result.text)
                            merged = (card.model_dump() if card else {}) | refinement.model_dump()
                            card = GeneratedCardModel.model_validate(merged)
                        except (ValidationError, ValueError, TypeError):
                            raise _Stop("held", "schema_invalid") from None
                        if stage in ("concept", "lore"):
                            await self._gate(
                                card.model_dump_json(),
                                "post_text",
                                stage,
                                evidence,
                                telemetry_version,
                            )
                art_prompt = derive_art_prompt(card)
                await self._gate(
                    card.model_dump_json(),
                    "post_text",
                    "final_text",
                    evidence,
                    telemetry_version,
                )
                await self._gate(
                    art_prompt,
                    "post_art_prompt",
                    "final_art_prompt",
                    evidence,
                    telemetry_version,
                )
                required = {"pre_prompt", "concept", "lore", "final_text", "final_art_prompt"}
                if {e.stage for e in evidence if e.decision == "allowed"} != required:
                    raise _Stop("held", "invalid_evidence")
                response = GenerateCardAgentResponse(
                    schemaVersion=1, status="completed", card=card, artPrompt=art_prompt
                )
        except _Stop as stop:
            response = GenerateCardAgentResponse(
                schemaVersion=1, status=stop.status, safetyHints=[stop.reason]
            )
        except Exception as exc:
            raise classify_runtime_failure(exc, failure_stage) from None

        evidence.extend(
            [
                SafetyEvidence(
                    stage="hosted_guardrails",
                    policy="hosted_guardrails",
                    decision="unavailable",
                    reason="not_observed",
                ),
                SafetyEvidence(
                    stage="post_image",
                    policy="post_image",
                    decision="not_applicable",
                    reason="text_only",
                ),
            ]
        )
        response.metadata = metadata | {"safetyEvidence": [e.model_dump() for e in evidence]}
        return response

    async def _gate(
        self,
        text: str,
        moderation_stage: str,
        evidence_stage: str,
        evidence: list[SafetyEvidence],
        agent_version: str,
    ) -> None:
        decision = await self.moderation.moderate_text(text, stage=moderation_stage)
        if (
            not isinstance(decision, ModerationDecision)
            or decision.stage != moderation_stage
            or decision.reasonCode not in REASONS
            or decision.allowed != (decision.reasonCode == "allowed")
        ):
            raise _Stop("held", "invalid_evidence")
        record_moderation(
            stage=evidence_stage,
            allowed=decision.allowed,
            reason=decision.reasonCode,
            policy=POLICY,
            agent_version=agent_version,
        )
        evidence.append(
            SafetyEvidence(
                stage=evidence_stage,
                policy=POLICY,
                decision="allowed" if decision.allowed else "blocked",
                reason=decision.reasonCode,
            )
        )
        if not decision.allowed:
            raise _Stop("refused", decision.reasonCode)


def _dependency_failure_outcome(failure: RuntimeFailure) -> str:
    if failure.reason == RuntimeFailureReason.RATE_LIMITED:
        return "throttled"
    if failure.reason == RuntimeFailureReason.TIMEOUT:
        return "timed_out"
    return "failed"
