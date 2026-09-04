from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from azure.core.exceptions import (
    AzureError,
    ClientAuthenticationError,
    ResourceNotFoundError,
    ServiceRequestError,
)
from azure.identity.aio import DefaultAzureCredential
from azure.keyvault.secrets.aio import SecretClient

SecretSource = Literal["azure", "env"]
Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]

DEFAULT_SECRET_PROVIDER_BACKEND = "auto"
DEFAULT_SECRET_CACHE_TTL_SECONDS = 60.0
DEFAULT_SECRET_REQUEST_TIMEOUT_SECONDS = 2.0
DEFAULT_SECRET_MAX_RETRIES = 2
DEFAULT_SECRET_RETRY_BACKOFF_SECONDS = 0.25
DEFAULT_SECRET_MAX_STALE_SECONDS = 300.0

LOGGER = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class SecretReference:
    logical_name: str
    env_names: tuple[str, ...]
    key_vault_name: str


@dataclass(frozen=True, slots=True)
class SecretValue:
    name: str
    value: str
    version: str | None
    fetched_at: datetime
    source: SecretSource


@dataclass(frozen=True, slots=True)
class SecretVersion:
    name: str
    version: str | None
    enabled: bool
    created_on: datetime | None
    updated_on: datetime | None
    source: SecretSource

    @property
    def activated_on(self) -> datetime | None:
        return self.updated_on or self.created_on


@dataclass(frozen=True, slots=True)
class SecretProviderConfig:
    backend: str = DEFAULT_SECRET_PROVIDER_BACKEND
    key_vault_uri: str | None = None
    cache_ttl_seconds: float = DEFAULT_SECRET_CACHE_TTL_SECONDS
    request_timeout_seconds: float = DEFAULT_SECRET_REQUEST_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_SECRET_MAX_RETRIES
    retry_backoff_seconds: float = DEFAULT_SECRET_RETRY_BACKOFF_SECONDS
    max_stale_seconds: float = DEFAULT_SECRET_MAX_STALE_SECONDS


@dataclass(slots=True)
class _CachedSecret:
    secret: SecretValue
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _SecretVersionCandidate:
    version: str
    sort_key: datetime | None


DEFAULT_SECRET_REFERENCES: dict[str, SecretReference] = {
    "APP_SESSION_SECRET_KEY": SecretReference(
        logical_name="APP_SESSION_SECRET_KEY",
        env_names=("APP_SESSION_SECRET_KEY",),
        key_vault_name="app-session-secret-key",
    ),
    "ENTRA_CLIENT_SECRET": SecretReference(
        logical_name="ENTRA_CLIENT_SECRET",
        env_names=("ENTRA_CLIENT_SECRET", "ENTRA_EXTERNAL_ID_CLIENT_SECRET"),
        key_vault_name="entra-client-secret",
    ),
}


class SecretProvider(Protocol):
    async def get_secret(self, name: str) -> SecretValue: ...

    async def aclose(self) -> None: ...


class SecretProviderError(RuntimeError):
    pass


class SecretNotFoundError(SecretProviderError):
    pass


class SecretRefreshTimeout(SecretProviderError):
    pass


class SecretVersionUnavailableError(SecretProviderError):
    pass


class SecretStaleValueExpiredError(SecretProviderError):
    pass


class EnvSecretProvider:
    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        secret_references: Mapping[str, SecretReference] | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self._environ = environ if environ is not None else os.environ
        self._secret_references = dict(secret_references or DEFAULT_SECRET_REFERENCES)
        self._clock = clock

    async def get_secret(self, name: str) -> SecretValue:
        reference = resolve_secret_reference(name, self._secret_references)
        for env_name in reference.env_names:
            value = self._environ.get(env_name)
            if value not in (None, ""):
                return SecretValue(
                    name=reference.logical_name,
                    value=value,
                    version=None,
                    fetched_at=self._clock(),
                    source="env",
                )
        searched = ", ".join(reference.env_names)
        raise SecretNotFoundError(f"Secret '{reference.logical_name}' was not found in {searched}.")

    async def get_secret_version(self, name: str, *, version: str | None = None) -> SecretValue:
        secret = await self.get_secret(name)
        if version not in (None, secret.version):
            raise SecretVersionUnavailableError(
                f"Secret '{secret.name}' version '{version}' is unavailable from environment."
            )
        return secret

    async def list_secret_versions(self, name: str) -> list[SecretVersion]:
        secret = await self.get_secret(name)
        return [
            SecretVersion(
                name=secret.name,
                version=secret.version,
                enabled=True,
                created_on=None,
                updated_on=None,
                source=secret.source,
            )
        ]

    async def aclose(self) -> None:
        return None


class AzureSecretProvider:
    def __init__(
        self,
        *,
        key_vault_uri: str,
        credential: DefaultAzureCredential | Any | None = None,
        client: SecretClient | Any | None = None,
        close_credential: bool | None = None,
        close_client: bool | None = None,
        secret_references: Mapping[str, SecretReference] | None = None,
        cache_ttl: timedelta | float = DEFAULT_SECRET_CACHE_TTL_SECONDS,
        request_timeout_seconds: float = DEFAULT_SECRET_REQUEST_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_SECRET_MAX_RETRIES,
        retry_backoff_seconds: float = DEFAULT_SECRET_RETRY_BACKOFF_SECONDS,
        max_stale: timedelta | float = DEFAULT_SECRET_MAX_STALE_SECONDS,
        clock: Clock = utc_now,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._secret_references = dict(secret_references or DEFAULT_SECRET_REFERENCES)
        self._cache_ttl = _coerce_timedelta(cache_ttl)
        self._request_timeout_seconds = request_timeout_seconds
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._max_stale = _coerce_timedelta(max_stale)
        self._clock = clock
        self._sleep = sleep
        self._cache: dict[str, _CachedSecret] = {}
        self._inflight: dict[str, asyncio.Task[SecretValue]] = {}
        self._lock = asyncio.Lock()
        self._closed = False

        owned_credential = credential
        self._owns_credential = credential is None and client is None
        if client is None:
            if owned_credential is None:
                owned_credential = DefaultAzureCredential(
                    exclude_interactive_browser_credential=False
                )
            client = SecretClient(vault_url=key_vault_uri, credential=owned_credential)
            self._owns_client = True
        else:
            self._owns_client = False

        self._credential = owned_credential
        self._client = client
        if close_client is not None:
            self._owns_client = close_client
        if close_credential is not None:
            self._owns_credential = close_credential

        if self._cache_ttl.total_seconds() <= 0:
            raise ValueError("cache_ttl must be greater than zero.")
        if self._request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be greater than zero.")
        if self._max_retries < 0:
            raise ValueError("max_retries must be >= 0.")
        if self._retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must be >= 0.")
        if self._max_stale.total_seconds() < 0:
            raise ValueError("max_stale must be >= 0.")

    async def get_secret(self, name: str) -> SecretValue:
        if self._closed:
            raise RuntimeError("Secret provider is already closed.")

        reference = resolve_secret_reference(name, self._secret_references)
        cached = await self._get_cached(reference.logical_name)
        if cached is not None:
            return cached

        async with self._lock:
            cached = self._cache.get(reference.logical_name)
            if cached is not None and cached.expires_at > self._clock():
                return cached.secret

            refresh = self._inflight.get(reference.logical_name)
            if refresh is None:
                refresh = asyncio.create_task(
                    self._refresh_and_cache(reference),
                    name=f"secret-refresh:{reference.logical_name}",
                )
                self._inflight[reference.logical_name] = refresh

        return await asyncio.shield(refresh)

    async def get_secret_version(self, name: str, *, version: str | None = None) -> SecretValue:
        if self._closed:
            raise RuntimeError("Secret provider is already closed.")

        if version is None:
            return await self.get_secret(name)

        reference = resolve_secret_reference(name, self._secret_references)
        try:
            secret = await self._run_with_timeout_and_retry(
                lambda: self._client.get_secret(reference.key_vault_name, version=version),
                timeout_context=(f"loading secret '{reference.logical_name}' version '{version}'"),
            )
        except ResourceNotFoundError as exc:
            raise SecretVersionUnavailableError(
                f"Secret '{reference.logical_name}' version '{version}' is unavailable."
            ) from exc

        properties = getattr(secret, "properties", None)
        if getattr(properties, "enabled", True) is False:
            raise SecretVersionUnavailableError(
                f"Secret '{reference.logical_name}' version '{version}' is disabled."
            )

        return SecretValue(
            name=reference.logical_name,
            value=str(getattr(secret, "value", "")),
            version=_optional_text(getattr(properties, "version", None)) or version,
            fetched_at=self._clock(),
            source="azure",
        )

    async def list_secret_versions(self, name: str) -> list[SecretVersion]:
        if self._closed:
            raise RuntimeError("Secret provider is already closed.")

        reference = resolve_secret_reference(name, self._secret_references)
        try:
            return await self._run_with_timeout_and_retry(
                lambda: self._collect_version_metadata(reference),
                timeout_context=f"listing versions for secret '{reference.logical_name}'",
            )
        except ResourceNotFoundError as exc:
            raise SecretNotFoundError(
                f"Secret '{reference.logical_name}' does not exist in Key Vault."
            ) from exc

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True

        close_error: Exception | None = None
        if self._owns_client and hasattr(self._client, "close"):
            try:
                await self._client.close()
            except Exception as exc:  # pragma: no cover - defensive cleanup path
                close_error = exc

        if (
            self._owns_credential
            and self._credential is not None
            and hasattr(self._credential, "close")
        ):
            try:
                await self._credential.close()
            except Exception as exc:  # pragma: no cover - defensive cleanup path
                if close_error is None:
                    close_error = exc

        if close_error is not None:
            raise close_error

    async def _get_cached(self, name: str) -> SecretValue | None:
        async with self._lock:
            cached = self._cache.get(name)
            if cached is None:
                return None
            if cached.expires_at <= self._clock():
                return None
            return cached.secret

    async def _get_cached_entry(self, name: str) -> _CachedSecret | None:
        async with self._lock:
            return self._cache.get(name)

    async def _set_cached(self, secret: SecretValue) -> None:
        async with self._lock:
            self._cache[secret.name] = _CachedSecret(
                secret=secret,
                expires_at=secret.fetched_at + self._cache_ttl,
            )

    async def _refresh_and_cache(self, reference: SecretReference) -> SecretValue:
        cached = await self._get_cached_entry(reference.logical_name)
        try:
            secret = await self._refresh_secret(reference)
            await self._set_cached(secret)
            self._log_refresh_outcome("success", secret=secret)
            return secret
        except Exception as exc:
            error_category = _normalize_secret_refresh_error(exc)
            stale_secret = self._get_stale_secret(cached)
            if stale_secret is not None:
                self._log_refresh_outcome(
                    "stale",
                    secret=stale_secret,
                    error_category=error_category,
                )
                return stale_secret

            self._log_refresh_outcome(
                "failed",
                secret=cached.secret if cached is not None else None,
                error_category=error_category,
            )
            if cached is None and isinstance(
                exc,
                (SecretNotFoundError, SecretRefreshTimeout, SecretVersionUnavailableError),
            ):
                raise type(exc)(str(exc)) from None
            if cached is None:
                raise SecretProviderError(
                    f"Secret '{reference.logical_name}' refresh failed without a known-good cached "
                    f"value (error_category={error_category})."
                ) from None
            raise SecretStaleValueExpiredError(
                f"Secret '{reference.logical_name}' refresh failed after cached value exceeded "
                f"max stale (age_seconds={_secret_age_seconds(cached.secret, self._clock())}, "
                f"error_category={error_category})."
            ) from None
        finally:
            async with self._lock:
                self._inflight.pop(reference.logical_name, None)

    async def _refresh_secret(self, reference: SecretReference) -> SecretValue:
        versions = await self._load_latest_enabled_versions(reference)
        last_error: Exception | None = None
        for version in versions:
            try:
                secret = await self._run_with_timeout_and_retry(
                    lambda version=version.version: self._client.get_secret(
                        reference.key_vault_name,
                        version=version,
                    ),
                    timeout_context=f"loading secret '{reference.logical_name}'",
                )
            except ResourceNotFoundError as exc:
                last_error = exc
                continue

            properties = getattr(secret, "properties", None)
            if getattr(properties, "enabled", True) is False:
                continue

            candidate = _validated_secret_value(
                reference.logical_name,
                value=getattr(secret, "value", None),
                version=_optional_text(getattr(properties, "version", None)) or version.version,
                fetched_at=self._clock(),
            )
            if candidate is not None:
                return candidate

        if last_error is not None:
            raise SecretVersionUnavailableError(
                f"Secret '{reference.logical_name}' has no readable enabled version."
            ) from last_error

        raise SecretVersionUnavailableError(
            f"Secret '{reference.logical_name}' has no enabled version in Key Vault."
        )

    async def _load_latest_enabled_versions(
        self,
        reference: SecretReference,
    ) -> list[_SecretVersionCandidate]:
        versions = [
            _SecretVersionCandidate(
                version=version.version,
                sort_key=version.activated_on,
            )
            for version in await self.list_secret_versions(reference.logical_name)
            if version.enabled and version.version is not None
        ]
        if not versions:
            raise SecretVersionUnavailableError(
                f"Secret '{reference.logical_name}' has no enabled versions in Key Vault."
            )
        return versions

    async def _collect_version_metadata(
        self,
        reference: SecretReference,
    ) -> list[SecretVersion]:
        versions: list[SecretVersion] = []
        iterator = self._client.list_properties_of_secret_versions(reference.key_vault_name)
        async for properties in iterator:
            version = _optional_text(getattr(properties, "version", None))
            if version is None:
                continue
            versions.append(
                SecretVersion(
                    name=reference.logical_name,
                    version=version,
                    enabled=getattr(properties, "enabled", True) is not False,
                    created_on=getattr(properties, "created_on", None),
                    updated_on=getattr(properties, "updated_on", None),
                    source="azure",
                )
            )
        versions.sort(
            key=lambda candidate: candidate.activated_on or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return versions

    async def _run_with_timeout_and_retry(
        self,
        operation: Callable[[], Awaitable[Any]],
        *,
        timeout_context: str,
    ) -> Any:
        attempt = 0
        while True:
            try:
                return await asyncio.wait_for(operation(), timeout=self._request_timeout_seconds)
            except asyncio.TimeoutError as exc:
                if attempt >= self._max_retries:
                    raise SecretRefreshTimeout(f"Timed out while {timeout_context}.") from exc
                await self._backoff(attempt)
                attempt += 1
            except ResourceNotFoundError:
                raise
            except ServiceRequestError as exc:
                if attempt >= self._max_retries:
                    raise SecretProviderError(f"Failed while {timeout_context}.") from exc
                await self._backoff(attempt)
                attempt += 1
            except AzureError as exc:
                if not _is_retryable_azure_error(exc) or attempt >= self._max_retries:
                    raise SecretProviderError(f"Failed while {timeout_context}.") from exc
                await self._backoff(attempt)
                attempt += 1

    async def _backoff(self, attempt: int) -> None:
        delay = self._retry_backoff_seconds * (attempt + 1)
        if delay > 0:
            await self._sleep(delay)

    def _get_stale_secret(self, cached: _CachedSecret | None) -> SecretValue | None:
        if cached is None or self._max_stale.total_seconds() <= 0:
            return None
        if self._clock() > cached.expires_at + self._max_stale:
            return None
        return cached.secret

    def _log_refresh_outcome(
        self,
        outcome: Literal["success", "stale", "failed"],
        *,
        secret: SecretValue | None,
        error_category: str = "none",
    ) -> None:
        LOGGER.log(
            {
                "success": logging.INFO,
                "stale": logging.WARNING,
                "failed": logging.ERROR,
            }[outcome],
            f"secret.refresh_{outcome}",
            extra={
                "refresh_outcome": outcome,
                "secret_age_seconds": (
                    _secret_age_seconds(secret, self._clock()) if secret is not None else None
                ),
                "secret_version_hash": _secret_version_hash(secret.version if secret else None),
                "error_category": error_category,
            },
        )


def build_secret_provider_from_environment(
    *,
    environ: Mapping[str, str] | None = None,
    secret_references: Mapping[str, SecretReference] | None = None,
) -> SecretProvider:
    config = load_secret_provider_config(environ=environ)
    backend = config.backend
    if backend not in {"auto", "azure", "env"}:
        raise ValueError("SECRET_PROVIDER_BACKEND must be one of: auto, azure, env.")

    if backend == "env" or (backend == "auto" and not config.key_vault_uri):
        return EnvSecretProvider(environ=environ, secret_references=secret_references)

    if not config.key_vault_uri:
        raise SecretProviderError("KEY_VAULT_URI must be set when using the Azure secret provider.")

    return AzureSecretProvider(
        key_vault_uri=config.key_vault_uri,
        secret_references=secret_references,
        cache_ttl=config.cache_ttl_seconds,
        request_timeout_seconds=config.request_timeout_seconds,
        max_retries=config.max_retries,
        retry_backoff_seconds=config.retry_backoff_seconds,
        max_stale=config.max_stale_seconds,
    )


async def get_secret_version(
    provider: SecretProvider | Any,
    name: str,
    *,
    version: str | None = None,
) -> SecretValue:
    getter = getattr(provider, "get_secret_version", None)
    if callable(getter):
        return await getter(name, version=version)

    secret = await provider.get_secret(name)
    if version not in (None, secret.version):
        raise SecretVersionUnavailableError(f"Secret '{name}' version '{version}' is unavailable.")
    return secret


async def list_secret_versions(provider: SecretProvider | Any, name: str) -> list[SecretVersion]:
    getter = getattr(provider, "list_secret_versions", None)
    if callable(getter):
        return await getter(name)

    secret = await provider.get_secret(name)
    return [
        SecretVersion(
            name=secret.name,
            version=secret.version,
            enabled=True,
            created_on=None,
            updated_on=None,
            source=secret.source,
        )
    ]


def load_secret_provider_config(
    *,
    environ: Mapping[str, str] | None = None,
) -> SecretProviderConfig:
    values = environ if environ is not None else os.environ
    return SecretProviderConfig(
        backend=(
            (values.get("SECRET_PROVIDER_BACKEND") or DEFAULT_SECRET_PROVIDER_BACKEND)
            .strip()
            .lower()
            or DEFAULT_SECRET_PROVIDER_BACKEND
        ),
        key_vault_uri=_optional_env(values, "KEY_VAULT_URI"),
        cache_ttl_seconds=_float_env(
            values,
            "SECRET_PROVIDER_CACHE_TTL_SECONDS",
            default=DEFAULT_SECRET_CACHE_TTL_SECONDS,
            minimum=0.001,
        ),
        request_timeout_seconds=_float_env(
            values,
            "SECRET_PROVIDER_REQUEST_TIMEOUT_SECONDS",
            default=DEFAULT_SECRET_REQUEST_TIMEOUT_SECONDS,
            minimum=0.001,
        ),
        max_retries=_int_env(
            values,
            "SECRET_PROVIDER_MAX_RETRIES",
            default=DEFAULT_SECRET_MAX_RETRIES,
            minimum=0,
        ),
        retry_backoff_seconds=_float_env(
            values,
            "SECRET_PROVIDER_RETRY_BACKOFF_SECONDS",
            default=DEFAULT_SECRET_RETRY_BACKOFF_SECONDS,
            minimum=0.0,
        ),
        max_stale_seconds=_float_env(
            values,
            "SECRET_PROVIDER_MAX_STALE_SECONDS",
            default=DEFAULT_SECRET_MAX_STALE_SECONDS,
            minimum=0.0,
        ),
    )


def resolve_secret_reference(
    name: str,
    secret_references: Mapping[str, SecretReference] | None = None,
) -> SecretReference:
    references = secret_references or DEFAULT_SECRET_REFERENCES
    reference = references.get(name)
    if reference is not None:
        return reference
    return SecretReference(
        logical_name=name,
        env_names=(name,),
        key_vault_name=name.lower().replace("_", "-"),
    )


def _coerce_timedelta(value: timedelta | float) -> timedelta:
    if isinstance(value, timedelta):
        return value
    return timedelta(seconds=value)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _validated_secret_value(
    name: str,
    *,
    value: object,
    version: str | None,
    fetched_at: datetime,
) -> SecretValue | None:
    normalized_value = _optional_text(value)
    normalized_version = _optional_text(version)
    if normalized_value is None or normalized_version is None:
        return None
    return SecretValue(
        name=name,
        value=normalized_value,
        version=normalized_version,
        fetched_at=fetched_at,
        source="azure",
    )


def _is_retryable_azure_error(error: AzureError) -> bool:
    status_code = getattr(error, "status_code", None)
    return status_code in {408, 429, 500, 502, 503, 504}


def _normalize_secret_refresh_error(error: Exception) -> str:
    root = _root_cause(error)
    if isinstance(root, (SecretRefreshTimeout, asyncio.TimeoutError)):
        return "timeout"
    if isinstance(
        root,
        (SecretNotFoundError, SecretVersionUnavailableError, ResourceNotFoundError),
    ):
        return "not_found"
    if isinstance(root, ClientAuthenticationError):
        return "auth_error"
    if isinstance(root, ServiceRequestError):
        return "unavailable"
    if isinstance(root, AzureError):
        status_code = getattr(root, "status_code", None)
        if status_code in {401, 403}:
            return "auth_error"
        if status_code == 404:
            return "not_found"
        if status_code in {408, 504}:
            return "timeout"
        if status_code in {429, 500, 502, 503}:
            return "unavailable"
    return "unknown"


def _root_cause(error: Exception) -> Exception:
    current = error
    while isinstance(getattr(current, "__cause__", None), Exception):
        current = current.__cause__
    return current


def _secret_age_seconds(secret: SecretValue, now: datetime) -> int:
    return max(0, int((now - secret.fetched_at).total_seconds()))


def _secret_version_hash(version: str | None) -> str:
    if version is None:
        return "none"
    return hashlib.sha256(version.encode("utf-8")).hexdigest()[:12]


def _optional_env(values: Mapping[str, str], name: str) -> str | None:
    value = values.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _float_env(
    values: Mapping[str, str],
    name: str,
    *,
    default: float,
    minimum: float,
) -> float:
    raw_value = _optional_env(values, name)
    value = default if raw_value is None else float(raw_value)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}.")
    return value


def _int_env(
    values: Mapping[str, str],
    name: str,
    *,
    default: int,
    minimum: int,
) -> int:
    raw_value = _optional_env(values, name)
    value = default if raw_value is None else int(raw_value)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}.")
    return value
