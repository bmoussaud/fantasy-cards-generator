from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError, ServiceRequestError
from fastapi.testclient import TestClient

from app.main import create_app
from app.secrets import (
    LOGGER,
    AzureSecretProvider,
    EnvSecretProvider,
    SecretNotFoundError,
    SecretProviderError,
    SecretRefreshTimeout,
    SecretStaleValueExpiredError,
    SecretValue,
    SecretVersionUnavailableError,
    build_secret_provider_from_environment,
    load_secret_provider_config,
)


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


@dataclass(frozen=True, slots=True)
class FakeSecretProperties:
    version: str | None
    enabled: bool = True
    created_on: datetime | None = None
    updated_on: datetime | None = None


@dataclass(frozen=True, slots=True)
class FakeSecretBundle:
    value: str
    properties: FakeSecretProperties


class FakeAsyncCredential:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class FakeSecretClient:
    def __init__(self, *, versions: dict[str, list[FakeSecretProperties]]) -> None:
        self._versions = versions
        self._responses: dict[tuple[str, str | None], object] = {}
        self._list_responses: dict[str, object] = {}
        self.list_calls: list[str] = []
        self.get_calls: list[tuple[str, str | None]] = []
        self.close_calls = 0

    def set_response(self, name: str, version: str | None, response: object) -> None:
        self._responses[(name, version)] = response

    def set_list_response(self, name: str, response: object) -> None:
        self._list_responses[name] = response

    def list_properties_of_secret_versions(self, name: str) -> AsyncIterator[FakeSecretProperties]:
        self.list_calls.append(name)
        versions = list(self._versions.get(name, []))
        response = self._list_responses.get(name)

        async def iterator() -> AsyncIterator[FakeSecretProperties]:
            if isinstance(response, Exception):
                raise response
            for properties in versions:
                yield properties

        return iterator()

    async def get_secret(self, name: str, *, version: str | None = None) -> FakeSecretBundle:
        self.get_calls.append((name, version))
        response = self._responses[(name, version)]
        if isinstance(response, Exception):
            raise response
        if callable(response):
            result = response()
            if asyncio.iscoroutine(result):
                return await result
            return result
        return response

    async def close(self) -> None:
        self.close_calls += 1


async def fake_sleep(delay: float) -> None:
    return None


def run_async(awaitable):
    return asyncio.run(awaitable)


def make_version(
    clock: FakeClock,
    version: str,
    *,
    enabled: bool = True,
    age_seconds: float = 0,
) -> FakeSecretProperties:
    timestamp = clock.now() - timedelta(seconds=age_seconds)
    return FakeSecretProperties(
        version=version,
        enabled=enabled,
        created_on=timestamp,
        updated_on=timestamp,
    )


def test_env_secret_provider_reads_value_without_azure_calls() -> None:
    clock = FakeClock()
    provider = EnvSecretProvider(
        environ={"APP_SESSION_SECRET_KEY": "env-secret"},
        clock=clock.now,
    )

    secret = run_async(provider.get_secret("APP_SESSION_SECRET_KEY"))

    assert secret == SecretValue(
        name="APP_SESSION_SECRET_KEY",
        value="env-secret",
        version=None,
        fetched_at=clock.now(),
        source="env",
    )


def test_env_secret_provider_supports_legacy_entra_alias() -> None:
    provider = EnvSecretProvider(environ={"ENTRA_EXTERNAL_ID_CLIENT_SECRET": "legacy-secret"})

    secret = run_async(provider.get_secret("ENTRA_CLIENT_SECRET"))

    assert secret.value == "legacy-secret"
    assert secret.name == "ENTRA_CLIENT_SECRET"


def test_env_secret_provider_raises_for_missing_secret() -> None:
    provider = EnvSecretProvider(environ={})

    with pytest.raises(SecretNotFoundError, match="APP_SESSION_SECRET_KEY"):
        run_async(provider.get_secret("APP_SESSION_SECRET_KEY"))


def test_azure_secret_provider_caches_before_ttl_expiry() -> None:
    clock = FakeClock()
    version = make_version(clock, "v2")
    client = FakeSecretClient(versions={"app-session-secret-key": [version]})
    client.set_response(
        "app-session-secret-key",
        None,
        FakeSecretBundle("first-value", version),
    )
    provider = AzureSecretProvider(
        key_vault_uri="https://vault.example",
        client=client,
        cache_ttl=60,
        clock=clock.now,
        sleep=fake_sleep,
    )

    first = run_async(provider.get_secret("APP_SESSION_SECRET_KEY"))
    clock.advance(30)
    second = run_async(provider.get_secret("APP_SESSION_SECRET_KEY"))

    assert first.value == "first-value"
    assert second == first
    assert first.version == "v2"
    assert client.list_calls == []
    assert client.get_calls == [("app-session-secret-key", None)]


def test_azure_secret_provider_refreshes_after_ttl_and_tracks_new_version() -> None:
    clock = FakeClock()
    old_version = make_version(clock, "v1", age_seconds=30)
    client = FakeSecretClient(versions={"entra-client-secret": [old_version]})
    client.set_response("entra-client-secret", None, FakeSecretBundle("old", old_version))
    provider = AzureSecretProvider(
        key_vault_uri="https://vault.example",
        client=client,
        cache_ttl=60,
        clock=clock.now,
        sleep=fake_sleep,
    )

    first = run_async(provider.get_secret("ENTRA_CLIENT_SECRET"))

    clock.advance(61)
    new_version = make_version(clock, "v2")
    client._versions["entra-client-secret"] = [new_version, old_version]
    client.set_response("entra-client-secret", None, FakeSecretBundle("new", new_version))

    second = run_async(provider.get_secret("ENTRA_CLIENT_SECRET"))

    assert first.value == "old"
    assert first.version == "v1"
    assert second.value == "new"
    assert second.version == "v2"
    assert client.list_calls == []
    assert client.get_calls == [("entra-client-secret", None), ("entra-client-secret", None)]


def test_azure_secret_provider_skips_disabled_or_invalid_versions() -> None:
    clock = FakeClock()
    invalid = make_version(clock, "v3")
    disabled = make_version(clock, "v2", enabled=False, age_seconds=10)
    valid = make_version(clock, "v1", age_seconds=20)
    client = FakeSecretClient(versions={"app-session-secret-key": [invalid, disabled, valid]})
    client.set_response(
        "app-session-secret-key",
        None,
        FakeSecretBundle("   ", invalid),
    )
    client.set_response("app-session-secret-key", "v3", ResourceNotFoundError("missing version"))
    client.set_response(
        "app-session-secret-key",
        "v1",
        FakeSecretBundle("fallback-value", valid),
    )
    provider = AzureSecretProvider(
        key_vault_uri="https://vault.example",
        client=client,
        cache_ttl=60,
        clock=clock.now,
        sleep=fake_sleep,
    )

    secret = run_async(provider.get_secret("APP_SESSION_SECRET_KEY"))

    assert secret.value == "fallback-value"
    assert secret.version == "v1"
    assert client.get_calls == [
        ("app-session-secret-key", None),
        ("app-session-secret-key", "v3"),
        ("app-session-secret-key", "v1"),
    ]


def test_azure_secret_provider_coalesces_concurrent_refreshes() -> None:
    clock = FakeClock()
    version = make_version(clock, "v1")
    release = asyncio.Event()
    client = FakeSecretClient(versions={"app-session-secret-key": [version]})

    async def delayed_secret() -> FakeSecretBundle:
        await release.wait()
        return FakeSecretBundle("shared-value", version)

    client.set_response("app-session-secret-key", "v1", delayed_secret)
    client.set_response("app-session-secret-key", None, delayed_secret)
    provider = AzureSecretProvider(
        key_vault_uri="https://vault.example",
        client=client,
        cache_ttl=60,
        clock=clock.now,
        sleep=fake_sleep,
    )

    async def run_test() -> list:
        tasks = [
            asyncio.create_task(provider.get_secret("APP_SESSION_SECRET_KEY")) for _ in range(5)
        ]
        await asyncio.sleep(0)
        release.set()
        return await asyncio.gather(*tasks)

    results = run_async(run_test())

    assert [result.value for result in results] == ["shared-value"] * 5
    assert client.list_calls == []
    assert client.get_calls == [("app-session-secret-key", None)]


def test_azure_secret_provider_retries_transient_errors_without_sleeping_for_real() -> None:
    clock = FakeClock()
    version = make_version(clock, "v1")
    client = FakeSecretClient(versions={"entra-client-secret": [version]})
    attempts = {"count": 0}
    delays: list[float] = []

    async def tracked_sleep(delay: float) -> None:
        delays.append(delay)
        clock.advance(delay)

    async def flaky_secret() -> FakeSecretBundle:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ServiceRequestError("temporary")
        return FakeSecretBundle("recovered", version)

    client.set_response("entra-client-secret", None, flaky_secret)
    provider = AzureSecretProvider(
        key_vault_uri="https://vault.example",
        client=client,
        cache_ttl=60,
        clock=clock.now,
        sleep=tracked_sleep,
        max_retries=1,
        retry_backoff_seconds=0.5,
    )

    secret = run_async(provider.get_secret("ENTRA_CLIENT_SECRET"))

    assert secret.value == "recovered"
    assert attempts["count"] == 2
    assert delays == [0.5]


def test_azure_secret_provider_serves_stale_value_with_sanitized_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock()
    version = make_version(clock, "v1")
    client = FakeSecretClient(versions={"app-session-secret-key": [version]})
    client.set_response("app-session-secret-key", None, FakeSecretBundle("known-good", version))
    provider = AzureSecretProvider(
        key_vault_uri="https://vault.example",
        client=client,
        cache_ttl=60,
        max_stale=120,
        clock=clock.now,
        sleep=fake_sleep,
        max_retries=0,
    )

    expected = run_async(provider.get_secret("APP_SESSION_SECRET_KEY"))
    clock.advance(61)
    client.set_response(
        "app-session-secret-key",
        None,
        ServiceRequestError("known-good raw-sdk-detail"),
    )

    with caplog.at_level(logging.WARNING, logger=LOGGER.name):
        secret = run_async(provider.get_secret("APP_SESSION_SECRET_KEY"))

    assert secret == expected
    assert caplog.records[-1].message == "secret.refresh_stale"
    assert caplog.records[-1].refresh_outcome == "stale"
    assert caplog.records[-1].secret_age_seconds == 61
    assert caplog.records[-1].error_category == "network_error"
    assert caplog.records[-1].secret_version_hash != "v1"
    assert "known-good" not in caplog.text
    assert "raw-sdk-detail" not in caplog.text


def test_azure_secret_provider_times_out_with_bounded_calls() -> None:
    clock = FakeClock()
    version = make_version(clock, "v1")
    client = FakeSecretClient(versions={"app-session-secret-key": [version]})

    async def never_returns() -> FakeSecretBundle:
        await asyncio.Future()

    client.set_response("app-session-secret-key", None, never_returns)
    provider = AzureSecretProvider(
        key_vault_uri="https://vault.example",
        client=client,
        cache_ttl=60,
        clock=clock.now,
        sleep=fake_sleep,
        request_timeout_seconds=0.001,
        max_retries=0,
    )

    with pytest.raises(SecretRefreshTimeout, match="APP_SESSION_SECRET_KEY"):
        run_async(provider.get_secret("APP_SESSION_SECRET_KEY"))


def test_azure_secret_provider_fails_closed_once_stale_period_expires(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock()
    version = make_version(clock, "v1")
    client = FakeSecretClient(versions={"app-session-secret-key": [version]})
    client.set_response("app-session-secret-key", None, FakeSecretBundle("known-good", version))
    provider = AzureSecretProvider(
        key_vault_uri="https://vault.example",
        client=client,
        cache_ttl=60,
        max_stale=120,
        clock=clock.now,
        sleep=fake_sleep,
        max_retries=0,
    )

    run_async(provider.get_secret("APP_SESSION_SECRET_KEY"))
    clock.advance(181)
    client.set_response(
        "app-session-secret-key",
        None,
        ServiceRequestError("known-good raw-sdk-detail"),
    )

    with caplog.at_level(logging.ERROR, logger=LOGGER.name):
        with pytest.raises(SecretStaleValueExpiredError, match="error_category=network_error"):
            run_async(provider.get_secret("APP_SESSION_SECRET_KEY"))

    assert caplog.records[-1].message == "secret.refresh_failed"
    assert caplog.records[-1].refresh_outcome == "failed"
    assert caplog.records[-1].secret_age_seconds == 181
    assert caplog.records[-1].error_category == "network_error"
    assert "known-good" not in caplog.text
    assert "raw-sdk-detail" not in caplog.text


def test_azure_secret_provider_fails_closed_on_cold_start_without_cached_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock()
    version = make_version(clock, "v1")
    client = FakeSecretClient(versions={"app-session-secret-key": [version]})
    client.set_response(
        "app-session-secret-key",
        None,
        ServiceRequestError("cold-start raw-sdk-detail"),
    )
    provider = AzureSecretProvider(
        key_vault_uri="https://vault.example",
        client=client,
        cache_ttl=60,
        max_stale=120,
        clock=clock.now,
        sleep=fake_sleep,
        max_retries=0,
    )

    with caplog.at_level(logging.ERROR, logger=LOGGER.name):
        with pytest.raises(
            SecretProviderError,
            match="without a known-good cached value \\(error_category=network_error\\)",
        ):
            run_async(provider.get_secret("APP_SESSION_SECRET_KEY"))

    assert caplog.records[-1].message == "secret.refresh_failed"
    assert caplog.records[-1].refresh_outcome == "failed"
    assert caplog.records[-1].secret_age_seconds is None
    assert caplog.records[-1].secret_version_hash == "none"
    assert caplog.records[-1].error_category == "network_error"
    assert "raw-sdk-detail" not in caplog.text


def test_azure_secret_provider_raises_when_no_enabled_version_exists() -> None:
    clock = FakeClock()
    disabled = make_version(clock, "v2", enabled=False)
    client = FakeSecretClient(versions={"app-session-secret-key": [disabled]})
    client.set_response("app-session-secret-key", None, FakeSecretBundle("disabled", disabled))
    provider = AzureSecretProvider(
        key_vault_uri="https://vault.example",
        client=client,
        cache_ttl=60,
        clock=clock.now,
        sleep=fake_sleep,
    )

    with pytest.raises(SecretVersionUnavailableError, match="APP_SESSION_SECRET_KEY"):
        run_async(provider.get_secret("APP_SESSION_SECRET_KEY"))


def test_azure_secret_provider_does_not_replace_known_good_value_with_empty_secret() -> None:
    clock = FakeClock()
    good_version = make_version(clock, "v1", age_seconds=10)
    empty_version = make_version(clock, "v2")
    client = FakeSecretClient(versions={"app-session-secret-key": [good_version]})
    client.set_response(
        "app-session-secret-key",
        None,
        FakeSecretBundle("known-good", good_version),
    )
    provider = AzureSecretProvider(
        key_vault_uri="https://vault.example",
        client=client,
        cache_ttl=60,
        max_stale=120,
        clock=clock.now,
        sleep=fake_sleep,
    )

    initial = run_async(provider.get_secret("APP_SESSION_SECRET_KEY"))
    clock.advance(61)
    client._versions["app-session-secret-key"] = [empty_version, good_version]
    client.set_response("app-session-secret-key", None, FakeSecretBundle("   ", empty_version))
    client.set_response("app-session-secret-key", "v2", FakeSecretBundle("   ", empty_version))
    client.set_response(
        "app-session-secret-key",
        "v1",
        FakeSecretBundle("known-good", good_version),
    )

    refreshed = run_async(provider.get_secret("APP_SESSION_SECRET_KEY"))

    assert initial.value == "known-good"
    assert refreshed.value == "known-good"
    assert refreshed.version == "v1"
    assert client.get_calls == [
        ("app-session-secret-key", None),
        ("app-session-secret-key", None),
        ("app-session-secret-key", "v2"),
        ("app-session-secret-key", "v1"),
    ]


def test_azure_secret_provider_classifies_list_versions_access_denied() -> None:
    clock = FakeClock()
    error = HttpResponseError(message="forbidden raw-sdk-detail")
    error.status_code = 403
    client = FakeSecretClient(versions={"app-session-secret-key": []})
    client.set_list_response("app-session-secret-key", error)
    provider = AzureSecretProvider(
        key_vault_uri="https://vault.example",
        client=client,
        cache_ttl=60,
        clock=clock.now,
        sleep=fake_sleep,
        max_retries=0,
    )

    with pytest.raises(
        SecretProviderError,
        match=(
            "listing versions for secret 'APP_SESSION_SECRET_KEY' "
            "\\(error_category=access_denied\\)"
        ),
    ):
        run_async(provider.list_secret_versions("APP_SESSION_SECRET_KEY"))


def test_azure_secret_provider_classifies_list_versions_network_errors() -> None:
    clock = FakeClock()
    client = FakeSecretClient(versions={"app-session-secret-key": []})
    client.set_list_response(
        "app-session-secret-key",
        ServiceRequestError("network raw-sdk-detail"),
    )
    provider = AzureSecretProvider(
        key_vault_uri="https://vault.example",
        client=client,
        cache_ttl=60,
        clock=clock.now,
        sleep=fake_sleep,
        max_retries=0,
    )

    with pytest.raises(
        SecretProviderError,
        match=(
            "listing versions for secret 'APP_SESSION_SECRET_KEY' "
            "\\(error_category=network_error\\)"
        ),
    ):
        run_async(provider.list_secret_versions("APP_SESSION_SECRET_KEY"))


def test_azure_secret_provider_closes_owned_resources() -> None:
    clock = FakeClock()
    credential = FakeAsyncCredential()
    client = FakeSecretClient(versions={})
    provider = AzureSecretProvider(
        key_vault_uri="https://vault.example",
        credential=credential,
        client=client,
        close_credential=True,
        close_client=True,
        cache_ttl=60,
        clock=clock.now,
        sleep=fake_sleep,
    )

    run_async(provider.aclose())

    assert client.close_calls == 1
    assert credential.close_calls == 1


def test_build_secret_provider_from_environment_defaults_to_env_backend() -> None:
    provider = build_secret_provider_from_environment(
        environ={"APP_SESSION_SECRET_KEY": "present"},
    )

    assert isinstance(provider, EnvSecretProvider)


def test_load_secret_provider_config_reads_max_stale_seconds() -> None:
    config = load_secret_provider_config(
        environ={
            "KEY_VAULT_URI": "https://vault.example",
            "SECRET_PROVIDER_MAX_STALE_SECONDS": "90",
        },
    )

    assert config.max_stale_seconds == 90.0


def test_create_app_closes_injected_secret_provider_on_shutdown() -> None:
    class ClosingProvider:
        def __init__(self) -> None:
            self.closed = False

        async def get_secret(self, name: str) -> SecretValue:
            return SecretValue(
                name=name,
                value="unused",
                version=None,
                fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
                source="env",
            )

        async def aclose(self) -> None:
            self.closed = True

    provider = ClosingProvider()

    with TestClient(create_app(secret_provider=provider), base_url="https://testserver") as client:
        response = client.get("/")
        assert response.status_code == 200

    assert provider.closed is True
