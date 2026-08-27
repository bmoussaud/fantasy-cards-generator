from __future__ import annotations

import json
from base64 import b64decode
from collections.abc import Generator

import pytest
from authlib.integrations.base_client.errors import OAuthError
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from starlette.responses import RedirectResponse

from app import main as main_module
from app.main import create_app


class FakeOAuthClient:
    async def authorize_redirect(
        self,
        request,
        redirect_uri: str | None,
        nonce: str | None = None,
        **_: object,
    ) -> RedirectResponse:
        assert redirect_uri == "https://testserver/auth/callback"
        assert nonce
        return RedirectResponse(
            url="https://example.ciamlogin.com/oauth2/v2.0/authorize?code_challenge=test",
            status_code=307,
        )

    async def authorize_access_token(self, request) -> dict[str, str]:
        assert request.query_params["code"] == "valid-code"
        return {"id_token": "signed-id-token", "access_token": "unused"}

    async def parse_id_token(
        self,
        request,
        token: dict[str, str],
        nonce: str | None = None,
        **_: object,
    ) -> dict[str, str]:
        assert token["id_token"] == "signed-id-token"
        assert nonce
        return {
            "sub": "user-123",
            "name": "Aragorn",
            "email": "aragorn@example.com",
            "roles": "ignored",
        }


def decode_session_cookie(cookie_value: str, secret_key: str) -> dict[str, object]:
    signer = TimestampSigner(secret_key)
    unsigned = signer.unsign(cookie_value.encode("utf-8"))
    return json.loads(b64decode(unsigned))


@pytest.fixture(autouse=True)
def auth_environment(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_SESSION_SECRET_KEY", "test-session-secret")
    monkeypatch.setenv("ENTRA_EXTERNAL_ID_CLIENT_ID", "client-id")
    monkeypatch.setenv("ENTRA_EXTERNAL_ID_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv(
        "ENTRA_EXTERNAL_ID_AUTHORITY",
        "https://tenant.ciamlogin.com/tenant-id/v2.0",
    )
    monkeypatch.setenv("ENTRA_EXTERNAL_ID_REDIRECT_URI", "https://testserver/auth/callback")
    monkeypatch.setenv("ENTRA_EXTERNAL_ID_POST_LOGOUT_REDIRECT_URI", "https://testserver/")
    yield


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


def test_login_redirects_to_entra_and_sets_secure_session_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "create_oauth_client", lambda settings: FakeOAuthClient())
    client = TestClient(create_app(), base_url="https://testserver")

    response = client.get("/auth/login", follow_redirects=False)

    assert response.status_code == 307
    assert "example.ciamlogin.com" in response.headers["location"]
    assert "code_challenge=" in response.headers["location"]
    set_cookie = response.headers["set-cookie"].lower()
    assert "fantasy_cards_session=" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie
    assert "secure" in set_cookie


def test_callback_persists_minimal_user_claims_in_session(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert stored_session == {
        "user": {
            "sub": "user-123",
            "name": "Aragorn",
            "email": "aragorn@example.com",
        }
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
        async def authorize_access_token(self, request) -> dict[str, str]:
            raise OAuthError(error="mismatching_state")

    monkeypatch.setattr(main_module, "create_oauth_client", lambda settings: FailingOAuthClient())
    client = TestClient(create_app(), base_url="https://testserver")

    client.get("/auth/login", follow_redirects=False)
    response = client.get("/auth/callback?code=valid-code&state=opaque")

    assert response.status_code == 400
    assert response.json()["detail"] == "Authentication failed: mismatching_state"
