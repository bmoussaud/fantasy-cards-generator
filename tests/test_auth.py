from __future__ import annotations

import json
import logging
import time
from base64 import b64decode, b64encode
from io import BytesIO
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from authlib.integrations.base_client.async_openid import AsyncOpenIDMixin
from authlib.integrations.base_client.errors import OAuthError
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from joserfc import jwt
from joserfc.errors import InvalidClaimError
from joserfc.jwk import OctKey
from PIL import Image

from app import entra_profile, telemetry
from app import main as main_module
from app.auth import (
    DEFAULT_ENTRA_AUTHORITY,
    build_claims_options,
    build_logout_redirect_target,
    extract_user_claims,
    load_auth_settings,
)
from app.generation import ReferenceImageUpload
from app.main import create_app
from app.photos import ProfilePhotoImportState
from tests.conftest import (
    TEST_OBJECT_ID,
    TEST_OWNER_ID,
    TEST_TENANT_ID,
    FakeOAuthClient,
    begin_login,
)


class FakeAsyncOpenIDClient(AsyncOpenIDMixin):
    client_id = "client-id"

    def __init__(self, metadata: dict[str, Any], jwks: dict[str, Any]) -> None:
        self.server_metadata = metadata
        self._jwks = jwks

    async def load_server_metadata(self) -> dict[str, Any]:
        return self.server_metadata

    async def fetch_jwk_set(self, force: bool = False) -> dict[str, Any]:
        return self._jwks


def decode_session_cookie(cookie_value: str, secret_key: str) -> dict[str, object]:
    signer = TimestampSigner(secret_key)
    unsigned = signer.unsign(cookie_value.encode("utf-8"))
    return json.loads(b64decode(unsigned))


def callback_state(response) -> str:
    return parse_qs(urlsplit(response.headers["location"]).query)["state"][0]


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

    response = begin_login(client)

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


def test_standard_session_middleware_accepts_current_key_cookie() -> None:
    session = {
        "user": {
            "sub": "user-123",
            "name": "Aragorn",
            "email": "aragorn@example.com",
            "tenant_id": TEST_TENANT_ID,
            "object_id": TEST_OBJECT_ID,
            "owner_id": TEST_OWNER_ID,
        }
    }
    encoded = b64encode(json.dumps(session).encode("utf-8"))
    cookie = TimestampSigner("test-session-secret").sign(encoded).decode("utf-8")
    client = TestClient(create_app(), base_url="https://testserver")
    client.cookies.set("fantasy_cards_session", cookie)

    response = client.get("/app")

    assert response.status_code == 200
    assert "Aragorn" in response.text
    assert "aragorn@example.com" in response.text


def test_callback_persists_owner_claims_in_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "create_oauth_client", lambda settings: FakeOAuthClient())
    client = TestClient(create_app(), base_url="https://testserver")

    login_response = begin_login(client)
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


def test_profile_photo_import_is_an_explicit_sign_in_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_scopes: list[tuple[str, ...] | None] = []

    def oauth_client(settings, *, scopes=None):
        captured_scopes.append(scopes)
        return FakeOAuthClient()

    async def unexpected_fetch(_: str):
        pytest.fail("Unchecked photo consent must not fetch a photo")

    monkeypatch.setattr(main_module, "create_oauth_client", oauth_client)
    monkeypatch.setattr(main_module, "fetch_profile_photo", unexpected_fetch)
    client = TestClient(create_app(), base_url="https://testserver")

    page = client.get("/auth/login")
    assert page.status_code == 200
    assert 'name="import_profile_photo"' in page.text
    assert "checked" not in page.text.split('name="import_profile_photo"')[0].rsplit("<input", 1)[1]
    assert not captured_scopes
    begin_login(client)
    callback = client.get("/auth/callback?code=valid-code&state=opaque", follow_redirects=False)
    assert callback.status_code == 303
    shell = client.get("/app")
    assert "/auth/profile-photo/import" not in shell.text
    assert captured_scopes == [None, None]
    assert client.post("/auth/profile-photo/import", follow_redirects=False).status_code == 404
    assert client.post("/auth/profile-photo/decline", follow_redirects=False).status_code == 404

    client = TestClient(create_app(), base_url="https://testserver")
    start = begin_login(client, import_profile_photo=True)
    assert start.status_code == 307
    assert captured_scopes[-1] is not None
    assert "User.Read" in captured_scopes[-1]
    assert callback_state(start)


def test_sign_in_photo_choice_requires_csrf_and_cannot_be_enabled_by_a_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_client(*args, **kwargs):
        pytest.fail("A link or unprotected POST must not start authorization")

    monkeypatch.setattr(main_module, "create_oauth_client", unexpected_client)
    client = TestClient(create_app(), base_url="https://testserver")
    assert client.get("/auth/login?import_profile_photo=true").status_code == 200
    response = client.post("/auth/login", data={"import_profile_photo": "true"})
    assert response.status_code == 403
    assert "profile_photo_import" not in decode_session_cookie(
        client.cookies.get("fantasy_cards_session"), "test-session-secret"
    )


def test_sign_in_photo_choice_is_state_bound_and_callback_cannot_be_replayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetches = []

    async def no_photo(token):
        fetches.append(token)
        return None

    monkeypatch.setattr(main_module, "create_oauth_client", lambda settings, **_: FakeOAuthClient())
    monkeypatch.setattr(main_module, "fetch_profile_photo", no_photo)
    client = TestClient(create_app(), base_url="https://testserver")
    begin_login(client, import_profile_photo=True)
    mismatch = client.get(
        "/auth/callback?code=valid-code&state=wrong-state", follow_redirects=False
    )
    assert mismatch.headers["location"] == "/auth/login"
    assert "invalid or expired" in client.get("/auth/login").text
    assert not fetches
    start = begin_login(client, import_profile_photo=True)
    url = f"/auth/callback?code=valid-code&state={callback_state(start)}"
    assert client.get(url, follow_redirects=False).status_code == 303
    assert len(fetches) == 1
    assert client.get(url, follow_redirects=False).status_code == 400
    assert len(fetches) == 1


@pytest.mark.parametrize("prior_status", ["imported", "deleted_suppressed", "accepted"])
def test_sign_in_does_not_duplicate_or_restore_profile_photo(
    monkeypatch: pytest.MonkeyPatch, prior_status: str
) -> None:
    services = create_app().state.services
    asyncio_run(
        services.profile_photo_import_state_repository.save(
            ProfilePhotoImportState(owner_id=TEST_OWNER_ID, status=prior_status)
        )
    )

    async def unexpected_fetch(_):
        pytest.fail("Already imported, deleted, or in-progress photos must not be fetched again")

    monkeypatch.setattr(main_module, "create_oauth_client", lambda settings, **_: FakeOAuthClient())
    monkeypatch.setattr(main_module, "fetch_profile_photo", unexpected_fetch)
    client = TestClient(create_app(services=services), base_url="https://testserver")
    start = begin_login(client, import_profile_photo=True)
    callback = client.get(
        f"/auth/callback?code=valid-code&state={callback_state(start)}", follow_redirects=False
    )
    assert callback.status_code == 303
    shell = client.get("/app")
    assert shell.status_code == 200
    assert (
        "already in My Photos"
        if prior_status == "imported"
        else "not available in the current state"
    ) in shell.text
    state = asyncio_run(services.profile_photo_import_state_repository.get(TEST_OWNER_ID))
    assert state.status == prior_status


def test_profile_photo_import_consent_denial_returns_to_sign_in_without_authenticating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ImportConsentDeniedClient(FakeOAuthClient):
        async def authorize_access_token(self, request, **_: object) -> dict[str, Any]:
            if request.query_params.get("error") == "access_denied":
                raise OAuthError(error="access_denied")
            return await super().authorize_access_token(request)

    monkeypatch.setattr(
        main_module,
        "create_oauth_client",
        lambda settings, **_: ImportConsentDeniedClient(),
    )
    client = TestClient(create_app(), base_url="https://testserver")
    start = begin_login(client, import_profile_photo=True)
    assert start.status_code == 307
    callback = client.get(
        f"/auth/callback?error=access_denied&state={callback_state(start)}",
        follow_redirects=False,
    )

    assert callback.status_code == 303
    assert callback.headers["location"] == "/auth/login"
    shell = client.get("/auth/login")
    assert shell.status_code == 200
    assert "Microsoft photo access was not granted" in shell.text
    assert 'role="alert"' in shell.text
    assert client.get("/app", follow_redirects=False).status_code == 307
    session = decode_session_cookie(
        client.cookies.get("fantasy_cards_session"), "test-session-secret"
    )
    assert "user" not in session
    assert "profile_photo_import" not in session


@pytest.mark.parametrize(
    ("outcome", "message", "stage"),
    [
        ("imported", "was imported into My Photos", "import_complete"),
        ("no_photo", "Microsoft did not return a profile photo", "import_complete"),
        ("claim_failed", "could not be imported", "import_claim"),
        ("claim_unavailable", "not available in the current state", "import_claim"),
        ("graph_rejected", "could not be imported", "profile_photo_fetch"),
        ("save_failed", "could not be imported", "photo_save"),
        ("complete_changed", "not available in the current state", "import_complete"),
    ],
)
def test_profile_import_callback_displays_result_and_safe_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outcome: str,
    message: str,
    stage: str,
) -> None:
    sensitive = "DO-NOT-LOG-graph-token-or-error-body"
    services = create_app().state.services
    monkeypatch.setattr(telemetry, "_enabled", False)
    redirects = []

    class ImportOAuthClient(FakeOAuthClient):
        async def authorize_redirect(self, request, redirect_uri, **kwargs):
            redirects.append(redirect_uri)
            return await super().authorize_redirect(request, redirect_uri, **kwargs)

        async def authorize_access_token(self, request, **kwargs):
            token = await super().authorize_access_token(request, **kwargs)
            token["access_token"] = sensitive
            return token

    async def fake_fetch(access_token: str):
        assert access_token == sensitive
        if outcome == "no_photo":
            return None
        if outcome == "graph_rejected":
            raise entra_profile.ProfilePhotoFetchError(
                sensitive, error_code="graph_photo_rejected", status_code=403
            )
        image = BytesIO()
        Image.new("RGB", (32, 32), color="purple").save(image, format="PNG")
        return ReferenceImageUpload(
            content=image.getvalue(), content_type="image/png", filename="profile.png"
        )

    async def allow_photo(_):
        return []

    async def fail(*args, **kwargs):
        raise RuntimeError(sensitive)

    async def unavailable(*args, **kwargs):
        return None

    monkeypatch.setattr(
        main_module, "create_oauth_client", lambda settings, **_: ImportOAuthClient()
    )
    monkeypatch.setattr(main_module, "fetch_profile_photo", fake_fetch)
    monkeypatch.setattr(services.photo_moderation_service, "assert_allowed", allow_photo)
    if outcome == "claim_failed":
        monkeypatch.setattr(services.profile_photo_import_state_repository, "claim", fail)
    if outcome == "claim_unavailable":
        monkeypatch.setattr(services.profile_photo_import_state_repository, "claim", unavailable)
    if outcome == "save_failed":
        monkeypatch.setattr(main_module.SavedPhotoService, "save_photo", fail)
    if outcome == "complete_changed":
        monkeypatch.setattr(
            services.profile_photo_import_state_repository, "complete_import", unavailable
        )

    client = TestClient(create_app(services=services), base_url="https://testserver")
    start = begin_login(client, import_profile_photo=True)
    with caplog.at_level(logging.INFO, logger=telemetry.LOGGER_NAME):
        callback = client.get(
            f"/auth/callback?code=valid-code&state={callback_state(start)}",
            headers={"X-Request-ID": "photo-import-reference"},
            follow_redirects=False,
        )
    shell = client.get("/app")
    library = client.get("/my/photos").json()
    session = decode_session_cookie(
        client.cookies.get("fantasy_cards_session"), "test-session-secret"
    )

    assert callback.status_code == 303
    assert callback.headers["location"] == "/app"
    assert shell.status_code == 200
    assert message in shell.text
    assert session["user"]["owner_id"] == TEST_OWNER_ID
    assert len(library["photos"]) == (1 if outcome == "imported" else 0)
    assert "/auth/profile-photo/import" not in shell.text
    assert len(redirects) == 1
    assert f"stage={stage}" in caplog.text
    assert "auth.profile_photo_import_result" in caplog.text
    assert "request_id=photo-import-reference" in caplog.text
    if outcome not in {"imported", "no_photo"}:
        assert 'role="alert"' in shell.text
        assert "photo-import-reference" in shell.text
        assert any(record.levelno >= logging.WARNING for record in caplog.records)
    else:
        assert 'role="status"' in shell.text
    if outcome == "graph_rejected":
        assert "error_code=graph_photo_rejected" in caplog.text
        assert "http_status=403" in caplog.text
    if outcome in {"graph_rejected", "save_failed"}:
        state = asyncio_run(services.profile_photo_import_state_repository.get(TEST_OWNER_ID))
        assert state.status == "failed_retryable"
    assert sensitive not in shell.text + json.dumps(session) + caplog.text
    assert "profile_photo_import" not in session
    assert "profile-photo-import-result-heading" not in client.get("/app").text


def test_graph_profile_photo_request_uses_delegated_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "image/jpeg"}
        content = b"jpeg"

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get(self, url, *, headers, timeout):
            captured.update({"url": url, "headers": headers, "timeout": timeout})
            return FakeResponse()

    monkeypatch.setattr(entra_profile.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio_run(entra_profile.fetch_profile_photo("delegated-token"))

    assert result is not None
    assert captured["url"] == entra_profile.GRAPH_PROFILE_PHOTO_URL
    assert captured["headers"] == {"Authorization": "Bearer delegated-token"}


@pytest.mark.parametrize("status_code", [401, 403, 429, 500])
def test_graph_photo_failure_has_safe_status_without_response_body(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    import httpx

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            status_code, text="SENSITIVE-GRAPH-RESPONSE", request=request
        )
    )
    monkeypatch.setattr(
        entra_profile.httpx, "AsyncClient", lambda: real_client(transport=transport)
    )

    with pytest.raises(entra_profile.ProfilePhotoFetchError) as caught:
        asyncio_run(entra_profile.fetch_profile_photo("SENSITIVE-TOKEN"))

    assert caught.value.status_code == status_code
    assert caught.value.error_code == "graph_photo_rejected"
    assert "SENSITIVE" not in str(caught.value)


def test_profile_photo_import_state_write_failure_keeps_auth_and_resets_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = create_app().state.services
    client = TestClient(create_app(services=services), base_url="https://testserver")

    async def no_photo(_):
        return None

    async def failing_complete_no_photo(_):
        raise RuntimeError("state persistence failed")

    monkeypatch.setattr(
        services.profile_photo_import_state_repository,
        "complete_no_photo",
        failing_complete_no_photo,
    )
    monkeypatch.setattr(main_module, "fetch_profile_photo", no_photo)
    monkeypatch.setattr(
        main_module,
        "create_oauth_client",
        lambda settings, **_: FakeOAuthClient(),
    )

    start = begin_login(client, import_profile_photo=True)
    callback = client.get(
        f"/auth/callback?code=valid-code&state={callback_state(start)}",
        follow_redirects=False,
    )

    assert callback.status_code == 303
    assert client.get("/app").status_code == 200
    state = asyncio_run(services.profile_photo_import_state_repository.get(TEST_OWNER_ID))
    assert state is not None
    assert state.status == "failed_retryable"


def test_static_oauth_callback_preserves_authorization_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    class QueryCapturingOAuthClient(FakeOAuthClient):
        async def authorize_access_token(self, request, **kwargs: object) -> dict[str, Any]:
            captured.update(request.query_params)
            return await super().authorize_access_token(request, **kwargs)

    monkeypatch.setattr(
        main_module,
        "create_oauth_client",
        lambda settings: QueryCapturingOAuthClient(),
    )
    client = TestClient(create_app(), base_url="https://testserver")
    begin_login(client)

    response = client.get(
        "/auth/callback?code=valid-code&state=opaque&session_state=tenant-state",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert captured == {
        "code": "valid-code",
        "state": "opaque",
        "session_state": "tenant-state",
    }


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

    begin_login(client)
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
