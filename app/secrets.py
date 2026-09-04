from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from azure.core.exceptions import AzureError, ResourceNotFoundError, ServiceRequestError
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
class SecretProviderConfig:
    backend: str = DEFAULT_SECRET_PROVIDER_BACKEND
    key_vault_uri: str | None = None
    cache_ttl_seconds: float = DEFAULT_SECRET_CACHE_TTL_SECONDS
    request_timeout_seconds: float = DEFAULT_SECRET_REQUEST_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_SECRET_MAX_RETRIES
    retry_backoff_seconds: float = DEFAULT_SECRET_RETRY_BACKOFF_SECONDS


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
        clock: Clock = utc_now,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._secret_references = dict(secret_references or DEFAULT_SECRET_REFERENCES)
        self._cache_ttl = _coerce_timedelta(cache_ttl)
        self._request_timeout_seconds = request_timeout_seconds
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
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
                self._cache.pop(name, None)
                return None
            return cached.secret

    async def _set_cached(self, secret: SecretValue) -> None:
        async with self._lock:
            self._cache[secret.name] = _CachedSecret(
                secret=secret,
                expires_at=secret.fetched_at + self._cache_ttl,
            )

    async def _refresh_and_cache(self, reference: SecretReference) -> SecretValue:
        try:
            secret = await self._refresh_secret(reference)
            await self._set_cached(secret)
            return secret
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

            return SecretValue(
                name=reference.logical_name,
                value=str(getattr(secret, "value", "")),
                version=_optional_text(getattr(properties, "version", None)) or version.version,
                fetched_at=self._clock(),
                source="azure",
            )

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
        try:
            versions = await self._run_with_timeout_and_retry(
                lambda: self._collect_versions(reference.key_vault_name),
                timeout_context=f"listing versions for secret '{reference.logical_name}'",
            )
        except ResourceNotFoundError as exc:
            raise SecretNotFoundError(
                f"Secret '{reference.logical_name}' does not exist in Key Vault."
            ) from exc

        if not versions:
            raise SecretVersionUnavailableError(
                f"Secret '{reference.logical_name}' has no enabled versions in Key Vault."
            )
        return versions

    async def _collect_versions(self, key_vault_name: str) -> list[_SecretVersionCandidate]:
        versions: list[_SecretVersionCandidate] = []
        iterator = self._client.list_properties_of_secret_versions(key_vault_name)
        async for properties in iterator:
            if getattr(properties, "enabled", True) is False:
                continue
            version = _optional_text(getattr(properties, "version", None))
            if version is None:
                continue
            versions.append(
                _SecretVersionCandidate(
                    version=version,
                    sort_key=getattr(properties, "updated_on", None)
                    or getattr(properties, "created_on", None),
                )
            )
        versions.sort(
            key=lambda candidate: candidate.sort_key or datetime.min.replace(tzinfo=UTC),
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
    )


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


def _is_retryable_azure_error(error: AzureError) -> bool:
    status_code = getattr(error, "status_code", None)
    return status_code in {408, 429, 500, 502, 503, 504}


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
