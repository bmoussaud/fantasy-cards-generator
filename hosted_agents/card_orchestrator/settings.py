from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.foundry_agent_client import FoundryAgentConfigurationError, _normalize_project_endpoint

POLICY = "original-fantasy-v1"


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    project_endpoint: str
    model_deployment: str = Field(min_length=1, max_length=128, pattern=r"^[\w.-]+$")
    version: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    stage_timeout_seconds: float = Field(default=20, gt=0, le=20, allow_inf_nan=False)
    timeout_seconds: float = Field(default=65, gt=0, le=65, allow_inf_nan=False)
    moderation_policy: str = POLICY

    @field_validator("project_endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        try:
            return _normalize_project_endpoint(value).rstrip("/")
        except FoundryAgentConfigurationError:
            raise ValueError("Invalid project endpoint") from None

    @field_validator("moderation_policy")
    @classmethod
    def require_policy(cls, value: str) -> str:
        if value != POLICY:
            raise ValueError("Unsupported moderation policy")
        return value

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> RuntimeSettings:
        env = os.environ if environ is None else environ
        values = {
            "project_endpoint": env.get("FOUNDRY_PROJECT_ENDPOINT"),
            "model_deployment": env.get("AZURE_AI_MODEL_DEPLOYMENT_NAME"),
            "version": env.get("CARD_ORCHESTRATOR_VERSION"),
        }
        for field, key in (
            ("stage_timeout_seconds", "CARD_ORCHESTRATOR_STAGE_TIMEOUT_SECONDS"),
            ("timeout_seconds", "CARD_ORCHESTRATOR_TIMEOUT_SECONDS"),
            ("moderation_policy", "CARD_ORCHESTRATOR_MODERATION_POLICY"),
        ):
            if key in env:
                values[field] = env[key]
        return cls.model_validate(values)
