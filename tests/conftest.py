from __future__ import annotations

import os
import re
from collections.abc import Generator
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.responses import RedirectResponse

os.environ.update(
    {
        "APP_ENV": "test",
        "APP_SESSION_SECRET_KEY": "test-session-secret",
        "ENTRA_CLIENT_ID": "client-id",
        "ENTRA_CLIENT_SECRET": "client-secret",
        "ENTRA_AUTHORITY": "https://login.microsoftonline.com/organizations/v2.0",
        "ENTRA_REDIRECT_URI": "https://testserver/auth/callback",
        "ENTRA_POST_LOGOUT_REDIRECT_URI": "https://testserver/",
        "PERSISTENCE_MODE": "memory",
        "FOUNDRY_ENDPOINT": "https://foundry.example",
        "FOUNDRY_IMAGE_DEPLOYMENT": "gpt-image-2",
        "FOUNDRY_PROJECT_ENDPOINT": (
            "https://test.services.ai.azure.com/api/projects/test-project"
        ),
        "FOUNDRY_AGENT_NAME": "card-orchestrator",
        "FOUNDRY_AGENT_VERSION": "1",
        "FOUNDRY_AGENT_TIMEOUT_SECONDS": "0.2",
        "RATE_LIMIT_USER_REQUESTS": "6",
        "RATE_LIMIT_USER_WINDOW_SECONDS": "60",
        "RATE_LIMIT_IP_REQUESTS": "12",
        "RATE_LIMIT_IP_WINDOW_SECONDS": "60",
        "TRUSTED_PROXY_HOPS": "0",
        "UPSTREAM_MAX_RETRIES": "2",
        "IMAGE_MAX_RETRIES": "0",
        "UPSTREAM_BASE_BACKOFF_SECONDS": "0.01",
        "TEXT_TIMEOUT_SECONDS": "0.2",
        "IMAGE_TIMEOUT_SECONDS": "0.2",
        "OVERALL_TIMEOUT_SECONDS": "0.6",
        "AUDIT_RETENTION_DAYS": "30",
        "PROFILE_PHOTOS_CONTAINER_NAME": "profile-photos",
        "CONTENT_SAFETY_ENDPOINT": "https://content-safety.example",
        "CONTENT_SAFETY_API_VERSION": "2024-09-01",
        "CONTENT_SAFETY_MAX_HATE_SEVERITY": "2",
        "CONTENT_SAFETY_MAX_SELF_HARM_SEVERITY": "2",
        "CONTENT_SAFETY_MAX_SEXUAL_SEVERITY": "2",
        "CONTENT_SAFETY_MAX_VIOLENCE_SEVERITY": "2",
        "SAVED_PHOTO_MAX_COUNT": "10",
        "SAVED_PHOTO_MAX_BYTES": "4194304",
        "SAVED_PHOTO_THUMBNAIL_SIZE": "200",
        "TELEMETRY_ENABLED": "false",
        "APPLICATIONINSIGHTS_CONNECTION_STRING": "",
        "OTEL_SDK_DISABLED": "true",
    }
)

# The application settings are read when `app.main` is imported, so the test
# environment must be populated before importing it.
from app import main as main_module  # noqa: E402
from app.generation import MockAgentClient, MockAIClient, create_services  # noqa: E402
from app.main import create_app  # noqa: E402
from app.settings import load_app_settings  # noqa: E402

_test_settings = load_app_settings()
main_module.app = create_app(
    services=create_services(
        _test_settings,
        ai_client=MockAIClient(_test_settings),
        agent_client=MockAgentClient(),
    )
)

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
        query = {"code_challenge": "test"}
        if "state" in _:
            query["state"] = str(_["state"])
        return RedirectResponse(
            url=(
                "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize?"
                + urlencode(query)
            ),
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
    monkeypatch.setenv("PERSISTENCE_MODE", "memory")
    monkeypatch.setenv("FOUNDRY_ENDPOINT", "https://foundry.example")
    monkeypatch.setenv("FOUNDRY_IMAGE_DEPLOYMENT", "gpt-image-2")
    monkeypatch.setenv(
        "FOUNDRY_PROJECT_ENDPOINT",
        "https://test.services.ai.azure.com/api/projects/test-project",
    )
    monkeypatch.setenv("FOUNDRY_AGENT_NAME", "card-orchestrator")
    monkeypatch.setenv("FOUNDRY_AGENT_VERSION", "1")
    monkeypatch.setenv("FOUNDRY_AGENT_TIMEOUT_SECONDS", "0.2")
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
    real_create_services = create_services

    def create_test_services(settings):
        return real_create_services(
            settings,
            ai_client=MockAIClient(settings),
            agent_client=MockAgentClient(),
        )

    monkeypatch.setattr(main_module, "create_services", create_test_services)
    yield


def begin_login(client: TestClient, *, import_profile_photo: bool = False):
    page = client.get("/auth/login", follow_redirects=False)
    assert page.status_code == 200
    data = {"csrf_token": extract_hidden_value(page.text, "csrf_token")}
    if import_profile_photo:
        data["import_profile_photo"] = "true"
    return client.post("/auth/login", data=data, follow_redirects=False)


def make_authenticated_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(main_module, "create_oauth_client", lambda settings: FakeOAuthClient())
    client = TestClient(create_app(), base_url="https://testserver")
    login_response = begin_login(client)
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
