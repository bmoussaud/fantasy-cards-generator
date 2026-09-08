from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from app.foundry_agent_client import GenerateCardAgentRequest, GenerateCardAgentResponse
from app.generation import (
    GeneratedCardModel,
    HeuristicModerationService,
    ModerationDecision,
    derive_art_prompt,
)
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


class RuntimeFailure(Exception):
    """A dependency failed; never include the original exception or payload."""


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
        self.moderation = moderation or HeuristicModerationService(settings.moderation_policy)

    async def generate(
        self, request: GenerateCardAgentRequest, *, hosted_version: str | None = None
    ) -> GenerateCardAgentResponse:
        evidence: list[SafetyEvidence] = []
        metadata = {
            "agentVersion": self.settings.version,
            "candidate": "offline",
        }
        if hosted_version:
            metadata["hostedVersion"] = hosted_version
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                await self._gate(request.query, "pre_prompt", "pre_prompt", evidence)
                async with self.specialist_factory(self.settings) as specialists:
                    card = None
                    for stage in ("concept", "lore", "art_direction"):
                        payload = (
                            {"query": request.query}
                            if card is None
                            else {"card": card.model_dump()}
                        )
                        async with asyncio.timeout(self.settings.stage_timeout_seconds):
                            result = await specialists.run(stage, payload)
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
                            await self._gate(card.model_dump_json(), "post_text", stage, evidence)
                art_prompt = derive_art_prompt(card)
                await self._gate(card.model_dump_json(), "post_text", "final_text", evidence)
                await self._gate(art_prompt, "post_art_prompt", "final_art_prompt", evidence)
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
        except Exception:
            raise RuntimeFailure("dependency_failure") from None

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
    ) -> None:
        decision = await self.moderation.moderate_text(text, stage=moderation_stage)
        if (
            not isinstance(decision, ModerationDecision)
            or decision.stage != moderation_stage
            or decision.reasonCode not in REASONS
            or decision.allowed != (decision.reasonCode == "allowed")
        ):
            raise _Stop("held", "invalid_evidence")
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
