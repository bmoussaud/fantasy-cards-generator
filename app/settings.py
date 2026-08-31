from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

AI_MODE_VALUES = {"mock", "live"}
PERSISTENCE_MODE_VALUES = {"memory", "azure"}


@dataclass(frozen=True)
class RateLimitSettings:
    requests: int
    window_seconds: int


@dataclass(frozen=True)
class RetrySettings:
    max_retries: int
    image_max_retries: int
    base_backoff_seconds: float
    text_timeout_seconds: float
    image_timeout_seconds: float
    overall_timeout_seconds: float


@dataclass(frozen=True)
class AppSettings:
    app_env: str
    ai_mode: Literal["mock", "live"]
    persistence_mode: Literal["memory", "azure"]
    foundry_endpoint: str | None
    foundry_api_version: str
    foundry_text_deployment: str | None
    foundry_image_deployment: str | None
    cosmos_endpoint: str | None
    cosmos_database_name: str | None
    cosmos_container_name: str | None
    blob_endpoint: str | None
    blob_container_name: str | None
    moderation_service: str
    moderation_policy_name: str
    user_rate_limit: RateLimitSettings
    ip_rate_limit: RateLimitSettings
    trusted_proxy_hops: int
    retry: RetrySettings
    audit_retention_days: int
    image_size: str


class SettingsError(RuntimeError):
    pass


def load_app_settings() -> AppSettings:
    app_env = _string_env("APP_ENV", default="development")
    ai_mode = _string_env("AI_MODE", default="mock")
    if ai_mode not in AI_MODE_VALUES:
        raise SettingsError("AI_MODE must be one of: live, mock.")

    default_persistence_mode = "memory" if ai_mode == "mock" else "azure"
    persistence_mode = _string_env("PERSISTENCE_MODE", default=default_persistence_mode)
    if persistence_mode not in PERSISTENCE_MODE_VALUES:
        raise SettingsError("PERSISTENCE_MODE must be one of: azure, memory.")

    settings = AppSettings(
        app_env=app_env,
        ai_mode=ai_mode,
        persistence_mode=persistence_mode,
        foundry_endpoint=_optional_env("FOUNDRY_ENDPOINT"),
        foundry_api_version=_string_env("FOUNDRY_API_VERSION", default="2025-03-01-preview"),
        foundry_text_deployment=_optional_env("FOUNDRY_TEXT_DEPLOYMENT"),
        foundry_image_deployment=_optional_env("FOUNDRY_IMAGE_DEPLOYMENT"),
        cosmos_endpoint=_optional_env("COSMOS_ENDPOINT"),
        cosmos_database_name=_optional_env("COSMOS_DATABASE_NAME"),
        cosmos_container_name=_optional_env("COSMOS_CONTAINER_NAME"),
        blob_endpoint=_optional_env("BLOB_ENDPOINT"),
        blob_container_name=_optional_env("BLOB_CONTAINER_NAME"),
        moderation_service=_string_env("MODERATION_SERVICE", default="heuristic"),
        moderation_policy_name=_string_env(
            "MODERATION_POLICY_NAME",
            default="conservative-v1",
        ),
        user_rate_limit=RateLimitSettings(
            requests=_int_env("RATE_LIMIT_USER_REQUESTS", default=6, minimum=1),
            window_seconds=_int_env("RATE_LIMIT_USER_WINDOW_SECONDS", default=60, minimum=1),
        ),
        ip_rate_limit=RateLimitSettings(
            requests=_int_env("RATE_LIMIT_IP_REQUESTS", default=12, minimum=1),
            window_seconds=_int_env("RATE_LIMIT_IP_WINDOW_SECONDS", default=60, minimum=1),
        ),
        trusted_proxy_hops=_int_env("TRUSTED_PROXY_HOPS", default=0, minimum=0),
        retry=RetrySettings(
            max_retries=_int_env("UPSTREAM_MAX_RETRIES", default=2, minimum=0),
            image_max_retries=_int_env("IMAGE_MAX_RETRIES", default=0, minimum=0),
            base_backoff_seconds=_float_env(
                "UPSTREAM_BASE_BACKOFF_SECONDS",
                default=0.15,
                minimum=0.01,
            ),
            text_timeout_seconds=_float_env_with_legacy_fallback(
                "TEXT_TIMEOUT_SECONDS",
                legacy_name="UPSTREAM_TIMEOUT_SECONDS",
                default=20.0,
                minimum=0.1,
            ),
            image_timeout_seconds=_float_env_with_legacy_fallback(
                "IMAGE_TIMEOUT_SECONDS",
                legacy_name="UPSTREAM_TIMEOUT_SECONDS",
                default=150.0,
                minimum=0.1,
            ),
            overall_timeout_seconds=_float_env(
                "OVERALL_TIMEOUT_SECONDS",
                default=225.0,
                minimum=0.5,
            ),
        ),
        audit_retention_days=_int_env("AUDIT_RETENTION_DAYS", default=30, minimum=1),
        image_size=_string_env("IMAGE_SIZE", default="1024x1536"),
    )
    _validate_app_settings(settings)
    return settings


def _validate_app_settings(settings: AppSettings) -> None:
    if settings.moderation_service != "heuristic":
        raise SettingsError("MODERATION_SERVICE must currently be 'heuristic'.")

    if settings.persistence_mode == "azure":
        _require(
            settings.cosmos_endpoint,
            "COSMOS_ENDPOINT must be set when PERSISTENCE_MODE=azure.",
        )
        _require(
            settings.cosmos_database_name,
            "COSMOS_DATABASE_NAME must be set when PERSISTENCE_MODE=azure.",
        )
        _require(
            settings.cosmos_container_name,
            "COSMOS_CONTAINER_NAME must be set when PERSISTENCE_MODE=azure.",
        )
        _require(
            settings.blob_endpoint,
            "BLOB_ENDPOINT must be set when PERSISTENCE_MODE=azure.",
        )
        _require(
            settings.blob_container_name,
            "BLOB_CONTAINER_NAME must be set when PERSISTENCE_MODE=azure.",
        )

    if settings.ai_mode == "live":
        _require(settings.foundry_endpoint, "FOUNDRY_ENDPOINT must be set when AI_MODE=live.")
        _require(
            settings.foundry_text_deployment,
            "FOUNDRY_TEXT_DEPLOYMENT must be set when AI_MODE=live.",
        )
        _require(
            settings.foundry_image_deployment,
            "FOUNDRY_IMAGE_DEPLOYMENT must be set when AI_MODE=live.",
        )

    if settings.retry.overall_timeout_seconds <= settings.retry.text_timeout_seconds:
        raise SettingsError("OVERALL_TIMEOUT_SECONDS must be greater than TEXT_TIMEOUT_SECONDS.")


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _string_env(name: str, *, default: str | None = None) -> str:
    value = _optional_env(name)
    if value is not None:
        return value
    if default is None:
        raise SettingsError(f"{name} must be set.")
    return default


def _int_env(name: str, *, default: int, minimum: int) -> int:
    raw_value = _optional_env(name)
    value = default if raw_value is None else int(raw_value)
    if value < minimum:
        raise SettingsError(f"{name} must be >= {minimum}.")
    return value


def _float_env(name: str, *, default: float, minimum: float) -> float:
    raw_value = _optional_env(name)
    value = default if raw_value is None else float(raw_value)
    if value < minimum:
        raise SettingsError(f"{name} must be >= {minimum}.")
    return value


def _float_env_with_legacy_fallback(
    name: str,
    *,
    legacy_name: str,
    default: float,
    minimum: float,
) -> float:
    raw_value = _optional_env(name)
    if raw_value is None:
        raw_value = _optional_env(legacy_name)
    value = default if raw_value is None else float(raw_value)
    if value < minimum:
        raise SettingsError(f"{name} must be >= {minimum}.")
    return value


def _require(value: object, message: str) -> None:
    if value in (None, ""):
        raise SettingsError(message)
