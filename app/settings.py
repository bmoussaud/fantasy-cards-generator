from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

AI_MODE_VALUES = {"mock", "live"}
PERSISTENCE_MODE_VALUES = {"memory", "azure"}
TELEMETRY_ENV_VALUES = {"development", "production", "test"}
IMAGE_QUALITY_VALUES = {"low", "medium", "high"}


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
class TelemetrySettings:
    enabled: bool
    connection_string: str | None
    sampling_ratio: float
    service_name: str
    environment: str
    service_version: str | None
    container_revision: str | None
    container_replica: str | None


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
    profile_photos_container_name: str
    content_safety_endpoint: str | None
    content_safety_api_version: str
    content_safety_max_hate_severity: int
    content_safety_max_self_harm_severity: int
    content_safety_max_sexual_severity: int
    content_safety_max_violence_severity: int
    healthz_cosmos_timeout_ms: int
    healthz_blob_timeout_ms: int
    moderation_service: str
    moderation_policy_name: str
    user_rate_limit: RateLimitSettings
    ip_rate_limit: RateLimitSettings
    trusted_proxy_hops: int
    retry: RetrySettings
    audit_retention_days: int
    image_size: str
    image_quality: Literal["low", "medium", "high"]
    saved_photo_max_count: int
    saved_photo_max_bytes: int
    saved_photo_thumbnail_size: int


class SettingsError(RuntimeError):
    pass


def load_telemetry_settings() -> TelemetrySettings:
    environment = _telemetry_environment(
        _optional_env("TELEMETRY_ENVIRONMENT") or _string_env("APP_ENV", default="development")
    )
    service_name = _optional_env("TELEMETRY_SERVICE_NAME") or _string_env(
        "OTEL_SERVICE_NAME",
        default="fantasy-cards-generator",
    )
    return TelemetrySettings(
        enabled=_bool_env("TELEMETRY_ENABLED", default=False),
        connection_string=_optional_env("APPLICATIONINSIGHTS_CONNECTION_STRING"),
        sampling_ratio=_ratio_env("TELEMETRY_SAMPLING_RATIO", default=1.0),
        service_name=_bounded_identifier(service_name, name="TELEMETRY_SERVICE_NAME"),
        environment=environment,
        service_version=_optional_bounded_identifier("APP_VERSION"),
        container_revision=_optional_bounded_identifier("CONTAINER_APP_REVISION"),
        container_replica=_optional_bounded_identifier("CONTAINER_APP_REPLICA_NAME"),
    )


def load_app_settings() -> AppSettings:
    app_env = _string_env("APP_ENV", default="development")
    ai_mode = _string_env("AI_MODE", default="mock")
    if ai_mode not in AI_MODE_VALUES:
        raise SettingsError("AI_MODE must be one of: live, mock.")

    default_persistence_mode = "memory" if ai_mode == "mock" else "azure"
    persistence_mode = _string_env("PERSISTENCE_MODE", default=default_persistence_mode)
    if persistence_mode not in PERSISTENCE_MODE_VALUES:
        raise SettingsError("PERSISTENCE_MODE must be one of: azure, memory.")

    image_quality_raw = _string_env("IMAGE_QUALITY", default="low")
    if image_quality_raw not in IMAGE_QUALITY_VALUES:
        raise SettingsError("IMAGE_QUALITY must be one of: low, medium, high.")

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
        profile_photos_container_name=_string_env(
            "PROFILE_PHOTOS_CONTAINER_NAME",
            default="profile-photos",
        ),
        content_safety_endpoint=_optional_env("CONTENT_SAFETY_ENDPOINT")
        or _optional_env("FOUNDRY_ENDPOINT"),
        content_safety_api_version=_string_env(
            "CONTENT_SAFETY_API_VERSION",
            default="2024-09-01",
        ),
        content_safety_max_hate_severity=_int_env(
            "CONTENT_SAFETY_MAX_HATE_SEVERITY",
            default=2,
            minimum=0,
        ),
        content_safety_max_self_harm_severity=_int_env(
            "CONTENT_SAFETY_MAX_SELF_HARM_SEVERITY",
            default=2,
            minimum=0,
        ),
        content_safety_max_sexual_severity=_int_env(
            "CONTENT_SAFETY_MAX_SEXUAL_SEVERITY",
            default=2,
            minimum=0,
        ),
        content_safety_max_violence_severity=_int_env(
            "CONTENT_SAFETY_MAX_VIOLENCE_SEVERITY",
            default=2,
            minimum=0,
        ),
        healthz_cosmos_timeout_ms=_int_env("HEALTHZ_COSMOS_TIMEOUT_MS", default=1500, minimum=1),
        healthz_blob_timeout_ms=_int_env("HEALTHZ_BLOB_TIMEOUT_MS", default=1500, minimum=1),
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
        image_quality=image_quality_raw,  # type: ignore[arg-type]
        saved_photo_max_count=_int_env("SAVED_PHOTO_MAX_COUNT", default=10, minimum=1),
        saved_photo_max_bytes=_int_env(
            "SAVED_PHOTO_MAX_BYTES",
            default=4 * 1024 * 1024,
            minimum=1,
        ),
        saved_photo_thumbnail_size=_int_env(
            "SAVED_PHOTO_THUMBNAIL_SIZE",
            default=200,
            minimum=50,
        ),
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

    for name, value in (
        ("CONTENT_SAFETY_MAX_HATE_SEVERITY", settings.content_safety_max_hate_severity),
        ("CONTENT_SAFETY_MAX_SELF_HARM_SEVERITY", settings.content_safety_max_self_harm_severity),
        ("CONTENT_SAFETY_MAX_SEXUAL_SEVERITY", settings.content_safety_max_sexual_severity),
        ("CONTENT_SAFETY_MAX_VIOLENCE_SEVERITY", settings.content_safety_max_violence_severity),
    ):
        if value not in {0, 2, 4, 6}:
            raise SettingsError(f"{name} must be one of: 0, 2, 4, 6.")


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


def _bool_env(name: str, *, default: bool) -> bool:
    raw_value = _optional_env(name)
    if raw_value is None:
        return default
    normalized = raw_value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise SettingsError(f"{name} must be a boolean value.")


def _ratio_env(name: str, *, default: float) -> float:
    value = _float_env(name, default=default, minimum=0.0)
    if value <= 0.0 or value > 1.0:
        raise SettingsError(f"{name} must be > 0 and <= 1.")
    return value


def _telemetry_environment(value: str) -> str:
    normalized = value.strip().lower()
    aliases = {"dev": "development", "prod": "production", "testing": "test"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in TELEMETRY_ENV_VALUES:
        return "other"
    return normalized


def _optional_bounded_identifier(name: str) -> str | None:
    value = _optional_env(name)
    if value is None:
        return None
    return _bounded_identifier(value, name=name)


def _bounded_identifier(value: str, *, name: str) -> str:
    if not 1 <= len(value) <= 64 or not all(
        character.isascii() and (character.isalnum() or character in "._-") for character in value
    ):
        raise SettingsError(
            f"{name} must be 1-64 ASCII letters, digits, dots, underscores, or hyphens."
        )
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
