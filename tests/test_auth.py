from __future__ import annotations

import json
import time
from base64 import b64decode
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from authlib.integrations.base_client.async_openid import AsyncOpenIDMixin
from authlib.integrations.base_client.errors import OAuthError
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from joserfc import jwt
from joserfc.errors import InvalidClaimError
from joserfc.jwk import OctKey

from app import main as main_module
from app.auth import (
    DEFAULT_ENTRA_AUTHORITY,
    DEFAULT_ENTRA_CLIENT_SECRET_OVERLAP,
    AuthSettings,
    EntraOAuthClientManager,
    build_claims_options,
    build_logout_redirect_target,
    extract_user_claims,
    load_auth_settings,
)
from app.main import create_app
from app.secrets import SecretValue
from tests.conftest import TEST_OBJECT_ID, TEST_OWNER_ID, TEST_TENANT_ID, FakeOAuthClient


class FakeAsyncOpenIDClient(AsyncOpenIDMixin):
    client_id = "client-id"

    def __init__(self, metadata: dict[str, Any], jwks: dict[str, Any]) -> None:
        self.server_metadata = metadata
        self._jwks = jwks

    async def load_server_metadata(self) -> dict[str, Any]:
        return self.server_metadata

    async def fetch_jwk_set(self, force: bool = False) -> dict[str, Any]:
        return self._jwks


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class FakeRuntimeSecretProvider:
    def __init__(self, secret: SecretValue) -> None:
        self.secret = secret
        self.calls: list[str] = []

    def set_secret(self, secret: SecretValue) -> None:
        self.secret = secret

    async def get_secret(self, name: str) -> SecretValue:
        self.calls.append(name)
        return self.secret

    async def aclose(self) -> None:
        return None


class RecordingOAuthClient:
    def __init__(
        self,
        client_secret: str | None,
        *,
        authorize_results: list[dict[str, Any] | OAuthError] | None = None,
    ) -> None:
        self.client_secret = client_secret
        self.authorize_calls = 0
        self._authorize_results = list(authorize_results or [default_token_payload()])

    async def load_server_metadata(self) -> dict[str, str]:
        return {"issuer": "https://login.microsoftonline.com/{tenantid}/v2.0"}

    async def authorize_access_token(self, request, **_: object) -> dict[str, Any]:
        self.authorize_calls += 1
        result = self._authorize_results.pop(0)
        if isinstance(result, OAuthError):
            raise result
        return result


@dataclass
class RecordingOAuthClientFactory:
    def __init__(self) -> None:
        self.created_secrets: list[str | None] = []
        self.configured_clients: dict[str | None, list[RecordingOAuthClient]] = {}

    def queue_client(self, client_secret: str | None, client: RecordingOAuthClient) -> None:
        self.configured_clients.setdefault(client_secret, []).append(client)

    def __call__(self, settings: AuthSettings) -> RecordingOAuthClient:
        self.created_secrets.append(settings.client_secret)
        queued = self.configured_clients.get(settings.client_secret, [])
        if queued:
            return queued.pop(0)
        return RecordingOAuthClient(settings.client_secret)


def decode_session_cookie(cookie_value: str, secret_key: str) -> dict[str, object]:
    signer = TimestampSigner(secret_key)
    unsigned = signer.unsign(cookie_value.encode("utf-8"))
    return json.loads(b64decode(unsigned))


def make_runtime_secret(
    clock: FakeClock,
    *,
    value: str,
    version: str | None,
) -> SecretValue:
    return SecretValue(
        name="ENTRA_CLIENT_SECRET",
        value=value,
        version=version,
        fetched_at=clock.now(),
        source="azure",
    )


def default_token_payload() -> dict[str, Any]:
    return {
        "userinfo": {
            "sub": "user-123",
            "name": "Aragorn",
            "email": "aragorn@example.com",
            "tid": TEST_TENANT_ID,
            "oid": TEST_OBJECT_ID,
        }
    }


def test_load_auth_settings_defaults_to_organizations_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ENTRA_AUTHORITY", raising=False)
    monkeypatch.delenv("ENTRA_EXTERNAL_ID_AUTHORITY", raising=False)

    settings = load_auth_settings()

    assert settings.authority == DEFAULT_ENTRA_AUTHORITY
    assert (
        settings.metadata_url
        == "https://login.microsoftonline.com/organizations/v2.0/.well-known/openid-configuration"
    )


def test_load_auth_settings_accepts_legacy_external_id_env_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
    monkeypatch.delenv("ENTRA_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("ENTRA_AUTHORITY", raising=False)
    monkeypatch.delenv("ENTRA_REDIRECT_URI", raising=False)
    monkeypatch.delenv("ENTRA_POST_LOGOUT_REDIRECT_URI", raising=False)
    monkeypatch.setenv("ENTRA_EXTERNAL_ID_CLIENT_ID", "legacy-client-id")
    monkeypatch.setenv("ENTRA_EXTERNAL_ID_CLIENT_SECRET", "legacy-client-secret")
    monkeypatch.setenv(
        "ENTRA_EXTERNAL_ID_AUTHORITY",
        "https://login.microsoftonline.com/organizations/v2.0",
    )
    monkeypatch.setenv("ENTRA_EXTERNAL_ID_REDIRECT_URI", "https://legacy.example/auth/callback")
    monkeypatch.setenv("ENTRA_EXTERNAL_ID_POST_LOGOUT_REDIRECT_URI", "https://legacy.example/")

    settings = load_auth_settings()

    assert settings.client_id == "legacy-client-id"
    assert settings.client_secret == "legacy-client-secret"
    assert settings.authority == DEFAULT_ENTRA_AUTHORITY
    assert settings.redirect_uri == "https://legacy.example/auth/callback"
    assert settings.post_logout_redirect_uri == "https://legacy.example/"


def test_login_reloads_entra_secret_after_rotation_without_restarting_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    provider = FakeRuntimeSecretProvider(
        make_runtime_secret(clock, value="old-secret", version="v1")
    )
    seen_client_secrets: list[str | None] = []

    def fake_create_oauth_client(settings: AuthSettings) -> FakeOAuthClient:
        seen_client_secrets.append(settings.client_secret)
        return FakeOAuthClient()

    monkeypatch.setattr(main_module, "create_oauth_client", fake_create_oauth_client)

    with TestClient(create_app(secret_provider=provider), base_url="https://testserver") as client:
        first_response = client.get("/auth/login", follow_redirects=False)
        provider.set_secret(make_runtime_secret(clock, value="new-secret", version="v2"))
        second_response = client.get("/auth/login", follow_redirects=False)

    assert first_response.status_code == 307
    assert second_response.status_code == 307
    assert seen_client_secrets == ["old-secret", "new-secret"]


def test_entra_oauth_client_manager_rebuilds_client_when_secret_version_changes() -> None:
    clock = FakeClock()
    provider = FakeRuntimeSecretProvider(
        make_runtime_secret(clock, value="old-secret", version="v1")
    )
    factory = RecordingOAuthClientFactory()
    manager = EntraOAuthClientManager(
        settings=load_auth_settings(),
        secret_provider=provider,
        client_factory=factory,
        clock=clock.now,
    )

    first_client = asyncio_run(manager.get_client())
    second_client = asyncio_run(manager.get_client())
    provider.set_secret(make_runtime_secret(clock, value="new-secret", version="v2"))
    rotated_client = asyncio_run(manager.get_client())

    assert first_client is second_client
    assert rotated_client is not first_client
    assert factory.created_secrets == ["old-secret", "new-secret"]


def test_entra_oauth_client_manager_uses_previous_secret_during_overlap() -> None:
    clock = FakeClock()
    provider = FakeRuntimeSecretProvider(
        make_runtime_secret(clock, value="old-secret", version="v1")
    )
    factory = RecordingOAuthClientFactory()
    previous_client = RecordingOAuthClient("old-secret")
    current_client = RecordingOAuthClient(
        "new-secret",
        authorize_results=[OAuthError(error="invalid_client")],
    )
    factory.queue_client("old-secret", previous_client)
    factory.queue_client("new-secret", current_client)
    manager = EntraOAuthClientManager(
        settings=load_auth_settings(),
        secret_provider=provider,
        client_factory=factory,
        clock=clock.now,
    )

    asyncio_run(manager.get_client())
    provider.set_secret(make_runtime_secret(clock, value="new-secret", version="v2"))
    token = asyncio_run(manager.authorize_access_token(object()))

    assert token["userinfo"]["email"] == "aragorn@example.com"
    assert current_client.authorize_calls == 1
    assert previous_client.authorize_calls == 1


def test_entra_oauth_client_manager_expires_previous_secret_after_overlap_window() -> None:
    clock = FakeClock()
    provider = FakeRuntimeSecretProvider(
        make_runtime_secret(clock, value="old-secret", version="v1")
    )
    factory = RecordingOAuthClientFactory()
    previous_client = RecordingOAuthClient("old-secret")
    current_client = RecordingOAuthClient(
        "new-secret",
        authorize_results=[OAuthError(error="invalid_client")],
    )
    factory.queue_client("old-secret", previous_client)
    factory.queue_client("new-secret", current_client)
    manager = EntraOAuthClientManager(
        settings=load_auth_settings(),
        secret_provider=provider,
        client_factory=factory,
        clock=clock.now,
    )

    asyncio_run(manager.get_client())
    provider.set_secret(make_runtime_secret(clock, value="new-secret", version="v2"))
    asyncio_run(manager.get_client())
    clock.advance(DEFAULT_ENTRA_CLIENT_SECRET_OVERLAP + timedelta(seconds=1))

    with pytest.raises(OAuthError, match="invalid_client"):
        asyncio_run(manager.authorize_access_token(object()))

    assert current_client.authorize_calls == 1
    assert previous_client.authorize_calls == 0


def test_protected_shell_redirects_anonymous_users_to_login() -> None:
    client = TestClient(create_app(), base_url="https://testserver")

    response = client.get("/app", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/auth/login"


def test_create_app_fails_closed_when_session_secret_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APP_SESSION_SECRET_KEY", raising=False)

    with pytest.raises(
        RuntimeError,
        match="APP_SESSION_SECRET_KEY must be set before starting the application.",
    ):
        create_app()


def test_load_auth_settings_allows_key_vault_backed_session_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APP_SESSION_SECRET_KEY", raising=False)
    monkeypatch.setenv("SECRET_PROVIDER_BACKEND", "azure")
    monkeypatch.setenv("KEY_VAULT_URI", "https://vault.example")

    settings = load_auth_settings()

    assert settings.session_secret_key is None


def test_login_redirects_to_entra_and_sets_secure_session_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "create_oauth_client", lambda settings: FakeOAuthClient())
    client = TestClient(create_app(), base_url="https://testserver")

    response = client.get("/auth/login", follow_redirects=False)

    assert response.status_code == 307
    assert (
        "login.microsoftonline.com/organizations/oauth2/v2.0/authorize"
        in response.headers["location"]
    )
    assert "code_challenge=" in response.headers["location"]
    set_cookie = response.headers["set-cookie"].lower()
    assert "fantasy_cards_session=" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie
    assert "secure" in set_cookie


def test_callback_persists_owner_claims_in_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "create_oauth_client", lambda settings: FakeOAuthClient())
    client = TestClient(create_app(), base_url="https://testserver")

    login_response = client.get("/auth/login", follow_redirects=False)
    callback_response = client.get(
        "/auth/callback?code=valid-code&state=opaque",
        follow_redirects=False,
    )
    app_shell_response = client.get("/app")

    assert login_response.status_code == 307
    assert callback_response.status_code == 303
    assert callback_response.headers["location"] == "/app"
    set_cookie = callback_response.headers["set-cookie"].lower()
    assert "secure" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie
    stored_session = decode_session_cookie(
        client.cookies.get("fantasy_cards_session"),
        "test-session-secret",
    )
    assert stored_session["user"] == {
        "sub": "user-123",
        "name": "Aragorn",
        "email": "aragorn@example.com",
        "tenant_id": TEST_TENANT_ID,
        "object_id": TEST_OBJECT_ID,
        "owner_id": TEST_OWNER_ID,
    }
    assert "signed-id-token" not in app_shell_response.text
    assert "unused" not in app_shell_response.text
    assert "roles" not in app_shell_response.text
    assert "Aragorn" in app_shell_response.text
    assert "aragorn@example.com" in app_shell_response.text


def test_callback_rejects_missing_nonce_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "create_oauth_client", lambda settings: FakeOAuthClient())
    client = TestClient(create_app(), base_url="https://testserver")

    response = client.get("/auth/callback?code=valid-code&state=opaque")

    assert response.status_code == 400
    assert response.json()["detail"] == "Missing login state. Start the sign-in flow again."


def test_callback_rejects_oauth_validation_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingOAuthClient(FakeOAuthClient):
        async def authorize_access_token(self, request, **_: object) -> dict[str, Any]:
            raise OAuthError(error="mismatching_state")

    monkeypatch.setattr(main_module, "create_oauth_client", lambda settings: FailingOAuthClient())
    client = TestClient(create_app(), base_url="https://testserver")

    client.get("/auth/login", follow_redirects=False)
    response = client.get("/auth/callback?code=valid-code&state=opaque")

    assert response.status_code == 400
    assert response.json()["detail"] == "Authentication failed: mismatching_state"


def test_extract_user_claims_uses_tid_and_oid_for_owner_identity() -> None:
    claims = extract_user_claims(
        {
            "sub": "user-123",
            "name": "Aragorn",
            "email": "aragorn@example.com",
            "tid": TEST_TENANT_ID,
            "oid": TEST_OBJECT_ID,
        }
    )

    assert claims == {
        "sub": "user-123",
        "name": "Aragorn",
        "email": "aragorn@example.com",
        "tenant_id": TEST_TENANT_ID,
        "object_id": TEST_OBJECT_ID,
        "owner_id": TEST_OWNER_ID,
    }


def test_logout_uses_organizations_logout_endpoint() -> None:
    settings = load_auth_settings()

    assert (
        build_logout_redirect_target(settings)
        == "https://login.microsoftonline.com/organizations/oauth2/v2.0/logout"
        "?post_logout_redirect_uri=https%3A%2F%2Ftestserver%2F"
    )


def test_build_claims_options_accepts_multitenant_entra_issuer_template() -> None:
    tenant_id = str(uuid4())
    metadata = {
        "issuer": "https://login.microsoftonline.com/{tenantid}/v2.0",
        "id_token_signing_alg_values_supported": ["HS256"],
    }
    token, jwks = make_signed_id_token(
        issuer=f"https://login.microsoftonline.com/{tenant_id}/v2.0",
        tenant_id=tenant_id,
    )
    client = FakeAsyncOpenIDClient(metadata, jwks)

    claims = asyncio_run(
        client.parse_id_token(
            {"id_token": token, "access_token": "unused"},
            nonce="test-nonce",
            claims_options=build_claims_options(metadata["issuer"]),
        )
    )

    assert claims["iss"] == f"https://login.microsoftonline.com/{tenant_id}/v2.0"
    assert claims["tid"] == tenant_id


@pytest.mark.parametrize(
    ("issuer", "tenant_id"),
    [
        ("https://evil.example.invalid/tenant/v2.0", str(uuid4())),
        (
            f"https://login.microsoftonline.com/{uuid4()}/v2.0",
            str(uuid4()),
        ),
    ],
)
def test_build_claims_options_rejects_invalid_multitenant_issuer_variants(
    issuer: str,
    tenant_id: str,
) -> None:
    metadata = {
        "issuer": "https://login.microsoftonline.com/{tenantid}/v2.0",
        "id_token_signing_alg_values_supported": ["HS256"],
    }
    token, jwks = make_signed_id_token(
        issuer=issuer,
        tenant_id=tenant_id,
    )
    client = FakeAsyncOpenIDClient(metadata, jwks)

    with pytest.raises(InvalidClaimError, match="iss"):
        asyncio_run(
            client.parse_id_token(
                {"id_token": token, "access_token": "unused"},
                nonce="test-nonce",
                claims_options=build_claims_options(metadata["issuer"]),
            )
        )


def asyncio_run(awaitable: Any) -> Any:
    import asyncio

    return asyncio.run(awaitable)


def make_signed_id_token(issuer: str, tenant_id: str) -> tuple[str, dict[str, Any]]:
    signing_key = OctKey.import_key(
        "test-signing-secret",
        {
            "kid": "test-key",
            "alg": "HS256",
        },
    )
    now = int(time.time())
    token = jwt.encode(
        {"alg": "HS256", "kid": "test-key"},
        {
            "iss": issuer,
            "sub": "user-123",
            "aud": "client-id",
            "exp": now + 300,
            "iat": now,
            "nonce": "test-nonce",
            "tid": tenant_id,
        },
        signing_key,
        algorithms=["HS256"],
    )
    return token, {"keys": [signing_key.as_dict()]}
