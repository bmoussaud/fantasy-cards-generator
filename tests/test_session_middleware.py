from __future__ import annotations

import asyncio
import json
from base64 import b64encode
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from app.secrets import (
    SecretProviderError,
    SecretValue,
    SecretVersion,
    SecretVersionUnavailableError,
)
from app.session_middleware import LOGGER, RotatingSessionMiddleware, load_session_signing_keys


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


@dataclass(frozen=True, slots=True)
class FakeManagedSecretVersion:
    version: str
    value: str
    created_on: datetime
    enabled: bool = True


class FakeSessionSecretProvider:
    def __init__(
        self,
        *,
        clock: FakeClock,
        versions: list[FakeManagedSecretVersion],
        list_error: Exception | None = None,
    ) -> None:
        self._clock = clock
        self._versions = list(versions)
        self._list_error = list_error

    def replace_versions(self, versions: list[FakeManagedSecretVersion]) -> None:
        self._versions = list(versions)

    async def get_secret(self, name: str) -> SecretValue:
        if name != "APP_SESSION_SECRET_KEY":
            raise SecretVersionUnavailableError(f"Unsupported secret '{name}'.")

        for version in self._versions:
            if version.enabled:
                return SecretValue(
                    name=name,
                    value=version.value,
                    version=version.version,
                    fetched_at=self._clock.now(),
                    source="azure",
                )
        raise SecretVersionUnavailableError(f"Secret '{name}' has no readable enabled version.")

    async def get_secret_version(self, name: str, *, version: str | None = None) -> SecretValue:
        if name != "APP_SESSION_SECRET_KEY":
            raise SecretVersionUnavailableError(f"Unsupported secret '{name}'.")

        if version is None:
            return await self.get_secret(name)

        for candidate in self._versions:
            if candidate.version != version:
                continue
            if not candidate.enabled:
                raise SecretVersionUnavailableError(
                    f"Secret '{name}' version '{version}' is disabled."
                )
            return SecretValue(
                name=name,
                value=candidate.value,
                version=candidate.version,
                fetched_at=self._clock.now(),
                source="azure",
            )
        raise SecretVersionUnavailableError(f"Secret '{name}' version '{version}' is unavailable.")

    async def list_secret_versions(self, name: str) -> list[SecretVersion]:
        if name != "APP_SESSION_SECRET_KEY":
            raise SecretVersionUnavailableError(f"Unsupported secret '{name}'.")
        if self._list_error is not None:
            raise self._list_error
        return [
            SecretVersion(
                name=name,
                version=version.version,
                enabled=version.enabled,
                created_on=version.created_on,
                updated_on=version.created_on,
                source="azure",
            )
            for version in self._versions
        ]

    async def aclose(self) -> None:
        return None


def make_version(
    clock: FakeClock,
    version: str,
    value: str,
    *,
    age_seconds: float = 0,
    enabled: bool = True,
) -> FakeManagedSecretVersion:
    return FakeManagedSecretVersion(
        version=version,
        value=value,
        created_on=clock.now() - timedelta(seconds=age_seconds),
        enabled=enabled,
    )


def make_signed_session_cookie(secret_key: str, session: dict[str, object]) -> str:
    payload = b64encode(json.dumps(session).encode("utf-8"))
    return TimestampSigner(secret_key).sign(payload).decode("utf-8")


def assert_cookie_is_signed_with(cookie_value: str, secret_key: str) -> None:
    TimestampSigner(secret_key).unsign(cookie_value.encode("utf-8"))


def build_session_app(
    *,
    provider: FakeSessionSecretProvider,
    clock: FakeClock,
    overlap_seconds: float = 3600,
) -> FastAPI:
    app = FastAPI()
    app.state.secret_provider = provider
    app.add_middleware(
        RotatingSessionMiddleware,
        session_cookie="fantasy_cards_session",
        same_site="lax",
        https_only=True,
        signing_key_overlap=timedelta(seconds=overlap_seconds),
        clock=clock.now,
    )

    @app.get("/session")
    async def read_session(request: Request) -> JSONResponse:
        return JSONResponse({"user": request.session.get("user")})

    @app.post("/session/{user}")
    async def write_session(request: Request, user: str) -> JSONResponse:
        request.session["user"] = user
        return JSONResponse({"user": user})

    return app


def test_rotating_session_middleware_signs_and_verifies_current_key() -> None:
    clock = FakeClock()
    provider = FakeSessionSecretProvider(
        clock=clock,
        versions=[make_version(clock, "v1", "current-signing-key")],
    )
    client = TestClient(
        build_session_app(provider=provider, clock=clock), base_url="https://testserver"
    )

    response = client.post("/session/aragorn")

    assert response.status_code == 200
    set_cookie = response.headers["set-cookie"].lower()
    assert "fantasy_cards_session=" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie
    assert "secure" in set_cookie
    assert_cookie_is_signed_with(client.cookies.get("fantasy_cards_session"), "current-signing-key")

    follow_up = client.get("/session")

    assert follow_up.status_code == 200
    assert follow_up.json() == {"user": "aragorn"}


def test_rotating_session_middleware_accepts_previous_key_within_overlap_window() -> None:
    clock = FakeClock()
    provider = FakeSessionSecretProvider(
        clock=clock,
        versions=[make_version(clock, "v1", "old-key", age_seconds=1800)],
    )
    client = TestClient(
        build_session_app(provider=provider, clock=clock), base_url="https://testserver"
    )

    initial = client.post("/session/aragorn")
    assert initial.status_code == 200

    provider.replace_versions(
        [
            make_version(clock, "v2", "new-key"),
            make_version(clock, "v1", "old-key", age_seconds=1800),
        ]
    )

    response = client.get("/session")

    assert response.status_code == 200
    assert response.json() == {"user": "aragorn"}


def test_rotating_session_middleware_rejects_previous_key_after_overlap_expires() -> None:
    clock = FakeClock()
    provider = FakeSessionSecretProvider(
        clock=clock,
        versions=[make_version(clock, "v1", "old-key", age_seconds=1800)],
    )
    client = TestClient(
        build_session_app(provider=provider, clock=clock, overlap_seconds=300),
        base_url="https://testserver",
    )

    initial = client.post("/session/aragorn")
    assert initial.status_code == 200

    provider.replace_versions(
        [
            make_version(clock, "v2", "new-key"),
            make_version(clock, "v1", "old-key", age_seconds=1800),
        ]
    )
    clock.advance(301)

    response = client.get("/session")

    assert response.status_code == 200
    assert response.json() == {"user": None}


def test_rotating_session_middleware_rejects_disabled_and_older_keys() -> None:
    clock = FakeClock()
    provider = FakeSessionSecretProvider(
        clock=clock,
        versions=[
            make_version(clock, "v3", "current-key"),
            make_version(clock, "v2", "disabled-key", age_seconds=60, enabled=False),
            make_version(clock, "v1", "older-key", age_seconds=120),
        ],
    )
    client = TestClient(
        build_session_app(provider=provider, clock=clock), base_url="https://testserver"
    )

    for secret_key in ("disabled-key", "older-key"):
        client.cookies.set(
            "fantasy_cards_session",
            make_signed_session_cookie(secret_key, {"user": "aragorn"}),
            domain="testserver",
            path="/",
        )
        response = client.get("/session")
        assert response.status_code == 200
        assert response.json() == {"user": None}


def test_load_session_signing_keys_keeps_current_key_when_version_listing_is_denied(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock()
    error = SecretProviderError(
        (
            "Failed while listing versions for secret 'APP_SESSION_SECRET_KEY' "
            "(error_category=access_denied)."
        ),
        error_category="access_denied",
    )
    provider = FakeSessionSecretProvider(
        clock=clock,
        versions=[make_version(clock, "v2", "current-key")],
        list_error=error,
    )

    with caplog.at_level("WARNING", logger=LOGGER.name):
        signing_keys = asyncio.run(
            load_session_signing_keys(
                provider,
                overlap_window=timedelta(seconds=3600),
                clock=clock.now,
            )
        )

    assert signing_keys.current.version == "v2"
    assert signing_keys.current.value == "current-key"
    assert signing_keys.previous is None
    assert caplog.records[-1].message == "session.previous_key_resolution_degraded"
    assert caplog.records[-1].error_category == "access_denied"


def test_rotating_session_middleware_converges_across_replicas_after_rotation() -> None:
    clock = FakeClock()
    provider = FakeSessionSecretProvider(
        clock=clock,
        versions=[make_version(clock, "v1", "old-key", age_seconds=120)],
    )
    replica_a = TestClient(
        build_session_app(provider=provider, clock=clock), base_url="https://a.test"
    )
    replica_b = TestClient(
        build_session_app(provider=provider, clock=clock), base_url="https://b.test"
    )

    signed_on_replica_a = replica_a.post("/session/aragorn")
    assert signed_on_replica_a.status_code == 200

    provider.replace_versions(
        [
            make_version(clock, "v2", "new-key"),
            make_version(clock, "v1", "old-key", age_seconds=120),
        ]
    )

    replica_b.cookies.set(
        "fantasy_cards_session",
        replica_a.cookies.get("fantasy_cards_session"),
        domain="b.test",
        path="/",
    )
    first_read = replica_b.get("/session")
    assert first_read.status_code == 200
    assert first_read.json() == {"user": "aragorn"}

    renewed = replica_b.post("/session/aragorn")
    assert renewed.status_code == 200
    assert_cookie_is_signed_with(replica_b.cookies.get("fantasy_cards_session"), "new-key")

    replica_a.cookies.set(
        "fantasy_cards_session",
        replica_b.cookies.get("fantasy_cards_session"),
        domain="a.test",
        path="/",
    )
    second_read = replica_a.get("/session")

    assert second_read.status_code == 200
    assert second_read.json() == {"user": "aragorn"}
