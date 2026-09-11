from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
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
DEFAULT_SECRET_REFRESH_INTERVAL_SECONDS = 20.0
DEFAULT_SECRET_REFRESH_TIMEOUT_SECONDS = 25.0
SECRET_REFRESH_FAILURE_COOLDOWN_SECONDS = 5.0
MAX_SECRET_SNAPSHOTS = 32

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
    expires_on: datetime | None = None
    not_before: datetime | None = None

    def is_valid_at(self, now: datetime) -> bool:
        return (self.not_before is None or self.not_before <= now) and (
            self.expires_on is None or now < self.expires_on
        )


@dataclass(frozen=True, slots=True)
class SecretVersion:
    name: str
    version: str | None
    enabled: bool
    created_on: datetime | None
    updated_on: datetime | None
    source: SecretSource
    expires_on: datetime | None = None
    not_before: datetime | None = None

    @property
    def activated_on(self) -> datetime | None:
        # Metadata edits must not reorder key history or reopen an elapsed overlap.
        return self.created_on

    def is_valid_at(self, now: datetime) -> bool:
        return (
            self.enabled
            and (self.not_before is None or self.not_before <= now)
            and (self.expires_on is None or now < self.expires_on)
        )


@dataclass(frozen=True, slots=True)
class SecretProviderConfig:
    backend: str = DEFAULT_SECRET_PROVIDER_BACKEND
    key_vault_uri: str | None = None
    cache_ttl_seconds: float = DEFAULT_SECRET_CACHE_TTL_SECONDS
    request_timeout_seconds: float = DEFAULT_SECRET_REQUEST_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_SECRET_MAX_RETRIES
    retry_backoff_seconds: float = DEFAULT_SECRET_RETRY_BACKOFF_SECONDS
    max_stale_seconds: float = DEFAULT_SECRET_MAX_STALE_SECONDS


@dataclass(frozen=True, slots=True)
class SecretSnapshot:
    current: SecretValue | None = None
    previous: SecretValue | None = None
    versions: tuple[SecretVersion, ...] = ()
    metadata_fetched_at: datetime | None = None
    retry_after: datetime | None = None
    error_category: str | None = None


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
    def __init__(self, message: str, *, error_category: str | None = None) -> None:
        super().__init__(message)
        self.error_category = error_category


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
        refresh_timeout_seconds: float = DEFAULT_SECRET_REFRESH_TIMEOUT_SECONDS,
    ) -> None:
        self._secret_references = dict(secret_references or DEFAULT_SECRET_REFERENCES)
        self._cache_ttl = _coerce_timedelta(cache_ttl)
        self._request_timeout_seconds = request_timeout_seconds
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._max_stale = _coerce_timedelta(max_stale)
        self._clock = clock
        self._sleep = sleep
        self._refresh_timeout_seconds = refresh_timeout_seconds
        self._cache: dict[str, SecretSnapshot] = {}
        self._inflight: dict[str, asyncio.Task[None]] = {}
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
        if self._refresh_timeout_seconds <= 0:
            raise ValueError("refresh_timeout_seconds must be greater than zero.")

    async def get_secret(self, name: str) -> SecretValue:
        snapshot = await self.get_snapshot(name)
        assert snapshot.current is not None
        return snapshot.current

    async def refresh_secret(self, name: str) -> SecretValue:
        await self._ensure_snapshot(name, force=True)
        snapshot = self.snapshot_for_read(name)
        assert snapshot.current is not None
        return snapshot.current

    async def get_snapshot(self, name: str) -> SecretSnapshot:
        await self._ensure_snapshot(name)
        return self.snapshot_for_read(name)

    def snapshot_for_read(self, name: str) -> SecretSnapshot:
        """Recheck deadlines and observed revocations without issuing SDK work."""
        if self._closed:
            raise RuntimeError("Secret provider is already closed.")
        name = resolve_secret_reference(name, self._secret_references).logical_name
        snapshot = self._cache.get(name, SecretSnapshot())
        if not self._eligible(snapshot.current, snapshot):
            category = snapshot.error_category or "not_found"
            error_type = (
                SecretStaleValueExpiredError
                if snapshot.current is not None
                else SecretVersionUnavailableError
            )
            raise error_type(
                f"Secret '{name}' has no eligible current value (error_category={category}).",
                error_category=category,
            )
        return replace(
            snapshot,
            previous=(
                snapshot.previous
                if self._eligible(snapshot.previous, snapshot)
                and snapshot.previous is not None
                and snapshot.current is not None
                and snapshot.previous.version != snapshot.current.version
                else None
            ),
        )

    def _eligible(self, secret: SecretValue | None, snapshot: SecretSnapshot) -> bool:
        now = self._clock()
        if secret is None or not secret.is_valid_at(now):
            return False
        if now >= secret.fetched_at + self._cache_ttl + self._max_stale:
            return False
        if snapshot.metadata_fetched_at is not None:
            if now >= snapshot.metadata_fetched_at + self._cache_ttl + self._max_stale:
                return False
        metadata = next((v for v in snapshot.versions if v.version == secret.version), None)
        if snapshot.versions and metadata is None:
            return False
        return metadata is None or metadata.is_valid_at(now)

    async def _ensure_snapshot(self, name: str, *, force: bool = False) -> None:
        if self._closed:
            raise RuntimeError("Secret provider is already closed.")
        reference = resolve_secret_reference(name, self._secret_references)
        name = reference.logical_name
        async with self._lock:
            snapshot = self._cache.get(name)
            now = self._clock()
            refresh = self._inflight.get(name)
            if refresh is None:
                if snapshot is not None:
                    if snapshot.retry_after is not None and now < snapshot.retry_after:
                        return
                    if (
                        not force
                        and self._eligible(snapshot.current, snapshot)
                        and snapshot.current is not None
                        and now < snapshot.current.fetched_at + self._cache_ttl
                        and snapshot.metadata_fetched_at is not None
                        and now < snapshot.metadata_fetched_at + self._cache_ttl
                    ):
                        return
                if name not in self._cache:
                    if len(self._cache) >= MAX_SECRET_SNAPSHOTS:
                        victim = next(
                            (key for key in self._cache if key not in self._inflight), None
                        )
                        if victim is None:
                            raise SecretProviderError("Secret snapshot capacity is exhausted.")
                        del self._cache[victim]
                    self._cache[name] = SecretSnapshot()
                refresh = asyncio.create_task(
                    self._refresh_and_cache(reference), name=f"secret-refresh:{name}"
                )
                refresh.add_done_callback(_observe_refresh_result)
                self._inflight[name] = refresh
        await asyncio.shield(refresh)

    async def get_secret_version(self, name: str, *, version: str | None = None) -> SecretValue:
        snapshot = await self.get_snapshot(name)
        for secret in (snapshot.current, snapshot.previous):
            if secret is not None and version in (None, secret.version):
                return secret
        raise SecretVersionUnavailableError(
            f"Secret '{name}' requested version is unavailable.", error_category="not_found"
        )

    async def list_secret_versions(self, name: str) -> list[SecretVersion]:
        return list((await self.get_snapshot(name)).versions)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True

        tasks = tuple(self._inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._cache.clear()
        try:
            if self._owns_client and hasattr(self._client, "close"):
                await self._client.close()
        finally:
            if (
                self._owns_credential
                and self._credential is not None
                and hasattr(self._credential, "close")
            ):
                await self._credential.close()

    async def _refresh_and_cache(self, reference: SecretReference) -> None:
        name = reference.logical_name
        cached = self._cache[name]
        try:
            async with asyncio.timeout(self._refresh_timeout_seconds):
                await self._refresh_snapshot(reference)
            self._cache[name] = replace(self._cache[name], retry_after=None, error_category=None)
            self._log_refresh_outcome("success", secret=self._cache[name].current)
        except (SecretProviderError, AzureError, TimeoutError) as exc:
            error_category = _normalize_secret_refresh_error(exc)
            self._cache[name] = replace(
                self._cache[name],
                retry_after=self._clock()
                + timedelta(seconds=SECRET_REFRESH_FAILURE_COOLDOWN_SECONDS),
                error_category=error_category,
            )
            snapshot = self._cache[name]
            if self._eligible(snapshot.current, snapshot):
                self._log_refresh_outcome(
                    "stale", secret=snapshot.current, error_category=error_category
                )
                return
            self._log_refresh_outcome(
                "failed",
                secret=cached.current,
                error_category=error_category,
            )
            if cached.current is None:
                error_type = SecretProviderError
                if isinstance(exc, ResourceNotFoundError):
                    error_type = SecretNotFoundError
                elif isinstance(exc, (SecretRefreshTimeout, TimeoutError)):
                    error_type = SecretRefreshTimeout
                elif isinstance(exc, SecretVersionUnavailableError):
                    error_type = SecretVersionUnavailableError
                raise error_type(
                    f"Secret '{name}' refresh failed without a known-good cached value "
                    f"(error_category={error_category}).",
                    error_category=error_category,
                ) from None
            raise SecretStaleValueExpiredError(
                f"Secret '{name}' has no eligible cached value "
                f"(error_category={error_category}).",
                error_category=error_category,
            ) from None
        finally:
            async with self._lock:
                self._inflight.pop(reference.logical_name, None)

    async def _fetch_value(
        self, reference: SecretReference, version: str | None = None
    ) -> SecretValue:
        try:
            secret = await self._run_with_timeout_and_retry(
                lambda: self._client.get_secret(reference.key_vault_name, version=version),
                timeout_context=f"loading secret '{reference.logical_name}'",
            )
        except ResourceNotFoundError:
            snapshot = self._cache[reference.logical_name]
            self._cache[reference.logical_name] = replace(
                snapshot, current=None if version is None else snapshot.current, previous=None
            )
            raise SecretVersionUnavailableError(
                f"Secret '{reference.logical_name}' requested version is unavailable.",
                error_category="not_found",
            ) from None
        properties = getattr(secret, "properties", None)
        candidate = _validated_secret_value(
            reference.logical_name,
            value=getattr(secret, "value", None),
            version=_optional_text(getattr(properties, "version", None)),
            fetched_at=self._clock(),
            expires_on=getattr(properties, "expires_on", None),
            not_before=getattr(properties, "not_before", None),
        )
        if (
            getattr(properties, "enabled", True) is False
            or candidate is None
            or candidate.version is None
            or (version is not None and candidate.version != version)
        ):
            snapshot = self._cache[reference.logical_name]
            # An invalid latest value is never replaced by an arbitrary historical key.
            self._cache[reference.logical_name] = replace(
                snapshot,
                current=None if version is None else snapshot.current,
                previous=None,
            )
            raise SecretVersionUnavailableError(
                f"Secret '{reference.logical_name}' requested version is invalid.",
                error_category="not_found",
            )
        return candidate

    async def _refresh_snapshot(self, reference: SecretReference) -> None:
        name = reference.logical_name
        history = tuple(
            await self._run_with_timeout_and_retry(
                lambda: self._collect_version_metadata(reference),
                timeout_context=f"listing versions for secret '{name}'",
            )
        )
        snapshot = self._cache[name]
        order_known = all(v.version is not None and v.created_on is not None for v in history)
        order_known = order_known and all(
            newer.created_on != older.created_on for newer, older in zip(history[:2], history[1:3])
        )
        # Do not discard unplaced versions before proving adjacency. Even an
        # invalid historical version can lie between two otherwise usable keys.
        versions = (
            history[:2]
            if order_known
            else tuple(
                v for v in history if snapshot.current and v.version == snapshot.current.version
            )[:1]
        )
        previous = snapshot.previous
        if (
            snapshot.current is not None
            and len(versions) == 2
            and snapshot.current.version == versions[1].version
        ):
            previous = snapshot.current
        if previous is not None and (len(versions) < 2 or previous.version != versions[1].version):
            previous = None
        self._cache[name] = replace(
            snapshot, versions=versions, previous=previous, metadata_fetched_at=self._clock()
        )
        if order_known and versions and not versions[0].is_valid_at(self._clock()):
            self._cache[name] = replace(self._cache[name], current=None, previous=None)
            raise SecretVersionUnavailableError(
                f"Secret '{name}' latest version is invalid.", error_category="not_found"
            )
        current = await self._fetch_value(reference)
        current_metadata = next((v for v in history if v.version == current.version), None)
        if (
            (history and current_metadata is None)
            or (current_metadata is not None and not current_metadata.is_valid_at(self._clock()))
            or (order_known and versions and current.version != versions[0].version)
        ):
            self._cache[name] = replace(self._cache[name], current=None, previous=None)
            raise SecretVersionUnavailableError(
                f"Secret '{name}' latest version metadata changed during refresh.",
                error_category="not_found",
            )
        if not order_known:
            versions = (current_metadata,) if current_metadata is not None else ()
        self._cache[name] = replace(self._cache[name], current=current, versions=versions)
        if (
            len(versions) < 2
            or versions[0].created_on is None
            or versions[1].created_on is None
            or versions[0].created_on <= versions[1].created_on
        ):
            self._cache[name] = replace(self._cache[name], previous=None)
            LOGGER.warning(
                "secret.previous_version_resolution_degraded",
                extra={"error_category": "version_metadata_missing"},
            )
            return
        predecessor = versions[1]
        if not predecessor.is_valid_at(self._clock()):
            self._cache[name] = replace(self._cache[name], previous=None)
            return
        previous = await self._fetch_value(reference, predecessor.version)
        self._cache[name] = replace(self._cache[name], previous=previous)

    async def _collect_version_metadata(
        self,
        reference: SecretReference,
    ) -> list[SecretVersion]:
        versions: list[SecretVersion] = []
        iterator = self._client.list_properties_of_secret_versions(reference.key_vault_name)
        async for properties in iterator:
            version = _optional_text(getattr(properties, "version", None))
            metadata = SecretVersion(
                name=reference.logical_name,
                version=version,
                enabled=getattr(properties, "enabled", True) is not False,
                created_on=getattr(properties, "created_on", None),
                updated_on=getattr(properties, "updated_on", None),
                source="azure",
                expires_on=getattr(properties, "expires_on", None),
                not_before=getattr(properties, "not_before", None),
            )
            # Apply observations before another page/value fetch can fail, without
            # advancing the successful metadata or value fetch timestamps.
            snapshot = self._cache[reference.logical_name]
            order_unknown = (
                metadata.version is None
                or metadata.created_on is None
                or any(
                    v.version != metadata.version and v.created_on == metadata.created_on
                    for v in snapshot.versions
                )
            )
            if order_unknown and snapshot.previous is not None:
                LOGGER.warning(
                    "secret.previous_version_resolution_degraded",
                    extra={"error_category": "version_metadata_missing"},
                )
            observed_latest_invalid = (
                not metadata.is_valid_at(self._clock())
                and metadata.created_on is not None
                and bool(snapshot.versions)
                and snapshot.versions[0].created_on is not None
                and metadata.created_on >= snapshot.versions[0].created_on
            )
            self._cache[reference.logical_name] = replace(
                snapshot,
                versions=tuple(metadata if v.version == version else v for v in snapshot.versions),
                current=(
                    None
                    if observed_latest_invalid
                    or (
                        not metadata.is_valid_at(self._clock())
                        and snapshot.current
                        and snapshot.current.version == version
                    )
                    else snapshot.current
                ),
                previous=(
                    None
                    if order_unknown
                    or (
                        not metadata.is_valid_at(self._clock())
                        and snapshot.previous
                        and snapshot.previous.version == version
                    )
                    else snapshot.previous
                ),
            )
            versions.append(metadata)
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
            except asyncio.TimeoutError:
                if attempt >= self._max_retries:
                    raise _sanitized_secret_error(
                        SecretRefreshTimeout,
                        f"Timed out while {timeout_context}",
                        error_category="timeout",
                    ) from None
                await self._backoff(attempt)
                attempt += 1
            except ResourceNotFoundError:
                raise
            except ServiceRequestError:
                if attempt >= self._max_retries:
                    raise _sanitized_secret_error(
                        SecretProviderError,
                        f"Failed while {timeout_context}",
                        error_category="network_error",
                    ) from None
                await self._backoff(attempt)
                attempt += 1
            except AzureError as exc:
                if not _is_retryable_azure_error(exc) or attempt >= self._max_retries:
                    raise _sanitized_secret_error(
                        SecretProviderError,
                        f"Failed while {timeout_context}",
                        error_category=_normalize_secret_refresh_error(exc),
                    ) from None
                await self._backoff(attempt)
                attempt += 1

    async def _backoff(self, attempt: int) -> None:
        delay = self._retry_backoff_seconds * (attempt + 1)
        if delay > 0:
            await self._sleep(delay)

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


def _observe_refresh_result(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        task.exception()


async def run_secret_refresh_worker(
    provider: AzureSecretProvider,
    name: str,
    *,
    on_refresh: Callable[[], Awaitable[Any]] | None = None,
    sleep: Sleep = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    delay = DEFAULT_SECRET_REFRESH_INTERVAL_SECONDS
    while True:
        await sleep(delay)
        started = monotonic()
        try:
            await provider.refresh_secret(name)
            if on_refresh is not None:
                await on_refresh()
        except SecretProviderError as exc:
            LOGGER.warning(
                "secret.background_refresh_degraded",
                extra={"error_category": classify_secret_error(exc)},
            )
        delay = max(0.0, DEFAULT_SECRET_REFRESH_INTERVAL_SECONDS - (monotonic() - started))


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
    expires_on: datetime | None = None,
    not_before: datetime | None = None,
) -> SecretValue | None:
    normalized_version = _optional_text(version)
    if not isinstance(value, str) or not value.strip() or normalized_version is None:
        return None
    secret = SecretValue(
        name=name,
        value=value,
        version=normalized_version,
        fetched_at=fetched_at,
        source="azure",
        expires_on=expires_on,
        not_before=not_before,
    )
    return secret if secret.is_valid_at(fetched_at) else None


def _sanitized_secret_error(
    error_type: type[SecretProviderError],
    message: str,
    *,
    error_category: str,
) -> SecretProviderError:
    return error_type(
        f"{message} (error_category={error_category}).",
        error_category=error_category,
    )


def _is_retryable_azure_error(error: AzureError) -> bool:
    status_code = getattr(error, "status_code", None)
    return status_code in {408, 429, 500, 502, 503, 504}


def classify_secret_error(error: Exception) -> str:
    return _normalize_secret_refresh_error(error)


def _normalize_secret_refresh_error(error: Exception) -> str:
    root = _root_cause(error)
    category = _optional_text(getattr(root, "error_category", None))
    if category is not None:
        return category
    if isinstance(root, (SecretRefreshTimeout, asyncio.TimeoutError)):
        return "timeout"
    if isinstance(
        root,
        (SecretNotFoundError, SecretVersionUnavailableError, ResourceNotFoundError),
    ):
        return "not_found"
    if isinstance(root, ClientAuthenticationError):
        return "access_denied"
    if isinstance(root, ServiceRequestError):
        return "network_error"
    if isinstance(root, AzureError):
        status_code = getattr(root, "status_code", None)
        if status_code in {401, 403}:
            return "access_denied"
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
