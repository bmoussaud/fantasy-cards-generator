from __future__ import annotations

import os
import re
from collections.abc import Generator
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.responses import RedirectResponse

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("ENTRA_CLIENT_ID", "client-id")
os.environ.setdefault("ENTRA_CLIENT_SECRET", "client-secret")
os.environ.setdefault("ENTRA_AUTHORITY", "https://login.microsoftonline.com/organizations/v2.0")
os.environ.setdefault("ENTRA_REDIRECT_URI", "https://testserver/auth/callback")
os.environ.setdefault("ENTRA_POST_LOGOUT_REDIRECT_URI", "https://testserver/")
os.environ.setdefault("AI_MODE", "mock")
os.environ.setdefault("PERSISTENCE_MODE", "memory")
os.environ.setdefault("RATE_LIMIT_USER_REQUESTS", "6")
os.environ.setdefault("RATE_LIMIT_USER_WINDOW_SECONDS", "60")
os.environ.setdefault("RATE_LIMIT_IP_REQUESTS", "12")
os.environ.setdefault("RATE_LIMIT_IP_WINDOW_SECONDS", "60")
os.environ.setdefault("TRUSTED_PROXY_HOPS", "0")
os.environ.setdefault("UPSTREAM_MAX_RETRIES", "2")
os.environ.setdefault("IMAGE_MAX_RETRIES", "0")
os.environ.setdefault("UPSTREAM_BASE_BACKOFF_SECONDS", "0.01")
os.environ.setdefault("TEXT_TIMEOUT_SECONDS", "0.2")
os.environ.setdefault("IMAGE_TIMEOUT_SECONDS", "0.2")
os.environ.setdefault("OVERALL_TIMEOUT_SECONDS", "0.6")
os.environ.setdefault("AUDIT_RETENTION_DAYS", "30")
os.environ.setdefault("PROFILE_PHOTOS_CONTAINER_NAME", "profile-photos")
os.environ.setdefault("CONTENT_SAFETY_ENDPOINT", "https://content-safety.example")
os.environ.setdefault("CONTENT_SAFETY_API_VERSION", "2024-09-01")
os.environ.setdefault("CONTENT_SAFETY_MAX_HATE_SEVERITY", "2")
os.environ.setdefault("CONTENT_SAFETY_MAX_SELF_HARM_SEVERITY", "2")
os.environ.setdefault("CONTENT_SAFETY_MAX_SEXUAL_SEVERITY", "2")
os.environ.setdefault("CONTENT_SAFETY_MAX_VIOLENCE_SEVERITY", "2")
os.environ.setdefault("SAVED_PHOTO_MAX_COUNT", "10")
os.environ.setdefault("SAVED_PHOTO_MAX_BYTES", "4194304")
os.environ.setdefault("SAVED_PHOTO_THUMBNAIL_SIZE", "200")
os.environ.setdefault("TELEMETRY_ENABLED", "false")
os.environ.setdefault("APPLICATIONINSIGHTS_CONNECTION_STRING", "")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

# The application settings are read when `app.main` is imported, so the test
# environment must be populated before importing it.
from app import main as main_module  # noqa: E402
from app.main import create_app  # noqa: E402

TEST_TENANT_ID = str(uuid4())
TEST_OBJECT_ID = str(uuid4())
TEST_OWNER_ID = f"{TEST_TENANT_ID}:{TEST_OBJECT_ID}"


class FakeOAuthClient:
    server_metadata = {
        "issuer": "https://login.microsoftonline.com/{tenantid}/v2.0",
    }

    async def load_server_metadata(self) -> dict[str, str]:
        return self.server_metadata

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
            url="https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize?code_challenge=test",
            status_code=307,
        )

    async def authorize_access_token(self, request, **_: object) -> dict[str, Any]:
        assert request.query_params["code"] == "valid-code"
        return {
            "id_token": "signed-id-token",
            "access_token": "unused",
            "userinfo": {
                "sub": "user-123",
                "name": "Aragorn",
                "email": "aragorn@example.com",
                "tid": TEST_TENANT_ID,
                "oid": TEST_OBJECT_ID,
                "roles": "ignored",
            },
        }


@pytest.fixture(autouse=True)
def base_environment(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_SESSION_SECRET_KEY", "test-session-secret")
    monkeypatch.setenv("ENTRA_CLIENT_ID", "client-id")
    monkeypatch.setenv("ENTRA_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("ENTRA_AUTHORITY", "https://login.microsoftonline.com/organizations/v2.0")
    monkeypatch.setenv("ENTRA_REDIRECT_URI", "https://testserver/auth/callback")
    monkeypatch.setenv("ENTRA_POST_LOGOUT_REDIRECT_URI", "https://testserver/")
    monkeypatch.setenv("AI_MODE", "mock")
    monkeypatch.setenv("PERSISTENCE_MODE", "memory")
    monkeypatch.setenv("RATE_LIMIT_USER_REQUESTS", "6")
    monkeypatch.setenv("RATE_LIMIT_USER_WINDOW_SECONDS", "60")
    monkeypatch.setenv("RATE_LIMIT_IP_REQUESTS", "12")
    monkeypatch.setenv("RATE_LIMIT_IP_WINDOW_SECONDS", "60")
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "0")
    monkeypatch.setenv("UPSTREAM_MAX_RETRIES", "2")
    monkeypatch.setenv("IMAGE_MAX_RETRIES", "0")
    monkeypatch.setenv("UPSTREAM_BASE_BACKOFF_SECONDS", "0.01")
    monkeypatch.setenv("TEXT_TIMEOUT_SECONDS", "0.2")
    monkeypatch.setenv("IMAGE_TIMEOUT_SECONDS", "0.2")
    monkeypatch.setenv("OVERALL_TIMEOUT_SECONDS", "0.6")
    monkeypatch.setenv("AUDIT_RETENTION_DAYS", "30")
    monkeypatch.setenv("PROFILE_PHOTOS_CONTAINER_NAME", "profile-photos")
    monkeypatch.setenv("CONTENT_SAFETY_ENDPOINT", "https://content-safety.example")
    monkeypatch.setenv("CONTENT_SAFETY_API_VERSION", "2024-09-01")
    monkeypatch.setenv("CONTENT_SAFETY_MAX_HATE_SEVERITY", "2")
    monkeypatch.setenv("CONTENT_SAFETY_MAX_SELF_HARM_SEVERITY", "2")
    monkeypatch.setenv("CONTENT_SAFETY_MAX_SEXUAL_SEVERITY", "2")
    monkeypatch.setenv("CONTENT_SAFETY_MAX_VIOLENCE_SEVERITY", "2")
    monkeypatch.setenv("SAVED_PHOTO_MAX_COUNT", "10")
    monkeypatch.setenv("SAVED_PHOTO_MAX_BYTES", "4194304")
    monkeypatch.setenv("SAVED_PHOTO_THUMBNAIL_SIZE", "200")
    monkeypatch.setenv("TELEMETRY_ENABLED", "false")
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    yield


def make_authenticated_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(main_module, "create_oauth_client", lambda settings: FakeOAuthClient())
    client = TestClient(create_app(), base_url="https://testserver")
    login_response = client.get("/auth/login", follow_redirects=False)
    assert login_response.status_code == 307
    callback_response = client.get(
        "/auth/callback?code=valid-code&state=opaque",
        follow_redirects=False,
    )
    assert callback_response.status_code == 303
    return client


@pytest.fixture
def authenticated_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    return make_authenticated_client(monkeypatch)


def extract_hidden_value(html: str, input_name: str) -> str:
    match = re.search(rf'name="{input_name}" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)
