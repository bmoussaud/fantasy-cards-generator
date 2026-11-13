from __future__ import annotations

import asyncio
import json
import socket
from base64 import b64decode
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import aiohttp
import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from pydantic import ValidationError

from app import linkedin_profile as linkedin_profile_module
from app import main as main_module
from app.linkedin_profile import (
    LINKEDIN_OIDC_SCOPES,
    AzureCosmosLinkedInTransientRepository,
    InMemoryLinkedInTransientRepository,
    LinkedInImportDraft,
    LinkedInOAuthState,
    NormalizedLinkedInProfile,
    fetch_linkedin_picture,
    hash_oauth_state,
    load_linkedin_settings,
    transient_expiry,
    validate_return_path,
)
from app.photos import InMemoryProfilePhotoImportStateRepository, ProfilePhotoImportState
from app.settings import SettingsError
from tests.conftest import (
    TEST_OWNER_ID,
    extract_hidden_value,
    make_authenticated_client,
)

OWNER_ID = f"{uuid4()}:{uuid4()}"
OTHER_OWNER_ID = f"{uuid4()}:{uuid4()}"


class FakeLinkedInOAuthClient:
    async def create_authorization_url(
        self,
        redirect_uri: str | None,
        **kwargs: object,
    ) -> dict[str, str]:
        assert redirect_uri == "https://testserver/auth/linkedin/callback"
        assert kwargs["scope"] == "openid profile"
        assert kwargs["nonce"]
        assert kwargs["code_verifier"]
        state = str(kwargs["state"])
        return {
            "url": "https://www.linkedin.com/oauth/v2/authorization?"
            + urlencode({"state": state, "code_challenge": "test"}),
            "state": state,
            "nonce": str(kwargs["nonce"]),
            "code_verifier": str(kwargs["code_verifier"]),
        }

    async def fetch_access_token(
        self,
        redirect_uri: str | None,
        **kwargs: object,
    ) -> dict[str, str]:
        assert redirect_uri == "https://testserver/auth/linkedin/callback"
        assert kwargs["code"] == "linkedin-code"
        assert kwargs["code_verifier"]
        return {
            "access_token": "must-not-persist",
            "id_token": "validated-by-fake",
        }

    async def parse_id_token(
        self,
        token: dict[str, str],
        *,
        nonce: str,
    ) -> dict[str, str]:
        assert token["access_token"] == "must-not-persist"
        assert nonce
        return {"sub": "linkedin-subject"}

    async def userinfo(self, **kwargs: object) -> dict[str, object]:
        assert kwargs["token"]
        return {
            "sub": "linkedin-subject",
            "given_name": "Ada",
            "family_name": "Lovelace",
            "picture": "https://media.licdn.com/photo.jpg",
            "email": "must-not-persist@example.com",
            "headline": "Ignored",
            "posts": ["Ignored"],
        }


def enabled_environment() -> dict[str, str]:
    return {
        "LINKEDIN_OIDC_ENABLED": "true",
        "LINKEDIN_CLIENT_ID": "linkedin-client",
        "LINKEDIN_CLIENT_SECRET": "linkedin-secret",
        "LINKEDIN_REDIRECT_URI": "https://cards.example/auth/linkedin/callback",
        "LINKEDIN_PILOT_OWNER_ALLOWLIST": OWNER_ID,
    }


def oauth_state(*, expires_at: str | None = None) -> LinkedInOAuthState:
    return LinkedInOAuthState(
        owner_id=OWNER_ID,
        state_hash=hash_oauth_state("opaque-state"),
        nonce="nonce",
        code_verifier="verifier",
        creation_draft_id="draft-1",
        photo_consent=False,
        return_path="/app",
        expires_at=expires_at or transient_expiry(),
    )


def test_linkedin_settings_are_disabled_and_empty_by_default() -> None:
    settings = load_linkedin_settings({})

    assert settings.enabled is False
    assert settings.is_configured
    assert settings.scopes == ("openid", "profile")
    assert settings.pilot_owner_allowlist == frozenset()
    assert settings.picture_host_allowlist == frozenset()


@pytest.mark.parametrize(
    "missing_name",
    [
        "LINKEDIN_CLIENT_ID",
        "LINKEDIN_CLIENT_SECRET",
        "LINKEDIN_REDIRECT_URI",
        "LINKEDIN_PILOT_OWNER_ALLOWLIST",
    ],
)
def test_enabled_linkedin_settings_fail_closed_when_required_value_is_missing(
    missing_name: str,
) -> None:
    environment = enabled_environment()
    environment.pop(missing_name)

    with pytest.raises(SettingsError, match=missing_name):
        load_linkedin_settings(environment)


def test_linkedin_settings_keep_exact_scopes_and_canonical_owner_gate() -> None:
    environment = enabled_environment() | {
        "LINKEDIN_PICTURE_HOST_ALLOWLIST": "media.licdn.com, Example.CDN.test.",
    }

    settings = load_linkedin_settings(environment)

    assert settings.scopes == LINKEDIN_OIDC_SCOPES
    assert settings.allows_owner(OWNER_ID)
    assert not settings.allows_owner(OTHER_OWNER_ID)
    assert not settings.allows_owner("sub:user-123")
    assert settings.picture_host_allowlist == {
        "media.licdn.com",
        "example.cdn.test",
    }


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LINKEDIN_PILOT_OWNER_ALLOWLIST", "sub:user-123"),
        ("LINKEDIN_PICTURE_HOST_ALLOWLIST", "https://media.licdn.com"),
        ("LINKEDIN_PICTURE_HOST_ALLOWLIST", "127.0.0.1"),
        ("LINKEDIN_REDIRECT_URI", "http://cards.example/auth/linkedin/callback"),
    ],
)
def test_linkedin_settings_reject_unsafe_configuration(name: str, value: str) -> None:
    environment = enabled_environment() | {name: value}

    with pytest.raises(SettingsError):
        load_linkedin_settings(environment)


def test_normalized_profile_ignores_unsupported_fields() -> None:
    profile = NormalizedLinkedInProfile.model_validate(
        {
            "sub": "linkedin-subject",
            "given_name": "Ada",
            "family_name": "Lovelace",
            "picture": "https://media.licdn.com/photo.jpg",
            "email": "must-not-survive@example.com",
            "headline": "Must not survive",
            "positions": [{"title": "Ignored"}],
            "posts": ["Ignored"],
        }
    )

    assert profile.display_name == "Ada Lovelace"
    assert set(profile.model_dump()) == {
        "sub",
        "name",
        "given_name",
        "family_name",
        "picture",
    }
    assert "email" not in profile.model_dump()


def test_normalized_profile_requires_a_name() -> None:
    with pytest.raises(ValidationError, match="display name"):
        NormalizedLinkedInProfile.model_validate({"sub": "linkedin-subject"})


def test_transient_documents_exclude_provider_subject_and_picture_uri() -> None:
    state_document = oauth_state().to_document()
    draft_document = LinkedInImportDraft(
        owner_id=OWNER_ID,
        draft_id="draft-1",
        display_name="Ada Lovelace",
        saved_photo_id=None,
        outcome="name_only",
        expires_at=transient_expiry(),
    ).to_document()
    serialized = repr((state_document, draft_document))

    assert "linkedin-subject" not in serialized
    assert "picture" not in serialized.lower()
    assert "accessToken" not in serialized
    assert 1 <= int(state_document["ttl"]) <= 900
    assert 1 <= int(draft_document["ttl"]) <= 900


def test_in_memory_state_is_single_use_under_concurrency() -> None:
    repository = InMemoryLinkedInTransientRepository()
    record = oauth_state()

    async def scenario() -> list[LinkedInOAuthState | None]:
        assert await repository.create_oauth_state(record)
        return await asyncio.gather(
            repository.consume_oauth_state(
                owner_id=OWNER_ID,
                state_hash=record.state_hash,
            ),
            repository.consume_oauth_state(
                owner_id=OWNER_ID,
                state_hash=record.state_hash,
            ),
        )

    results = asyncio.run(scenario())

    assert sum(result is not None for result in results) == 1


def test_in_memory_state_rejects_wrong_owner_and_expiry() -> None:
    repository = InMemoryLinkedInTransientRepository()
    expired = oauth_state(
        expires_at=(datetime.now(UTC) - timedelta(seconds=1))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

    async def scenario() -> tuple[LinkedInOAuthState | None, LinkedInOAuthState | None]:
        assert await repository.create_oauth_state(expired)
        wrong_owner = await repository.consume_oauth_state(
            owner_id=OTHER_OWNER_ID,
            state_hash=expired.state_hash,
        )
        expired_result = await repository.consume_oauth_state(
            owner_id=OWNER_ID,
            state_hash=expired.state_hash,
        )
        return wrong_owner, expired_result

    assert asyncio.run(scenario()) == (None, None)


def test_in_memory_draft_replaces_same_owner_and_deletes_by_owner() -> None:
    repository = InMemoryLinkedInTransientRepository()
    initial = LinkedInImportDraft(
        owner_id=OWNER_ID,
        draft_id="draft-1",
        display_name="Ada",
        saved_photo_id=None,
        outcome="name_only",
        expires_at=transient_expiry(),
    )
    replacement = LinkedInImportDraft(
        owner_id=OWNER_ID,
        draft_id="draft-1",
        display_name="Ada Lovelace",
        saved_photo_id="photo-1",
        outcome="imported",
        expires_at=transient_expiry(),
    )

    async def scenario() -> tuple[LinkedInImportDraft | None, LinkedInImportDraft | None]:
        await repository.replace_draft(initial)
        await repository.replace_draft(replacement)
        saved = await repository.get_draft(owner_id=OWNER_ID, draft_id="draft-1")
        await repository.delete_owner(OWNER_ID)
        deleted = await repository.get_draft(owner_id=OWNER_ID, draft_id="draft-1")
        return saved, deleted

    saved, deleted = asyncio.run(scenario())

    assert saved is replacement
    assert deleted is None


def test_cosmos_consume_uses_etag_condition() -> None:
    record = oauth_state()
    document = record.to_document() | {"_etag": "etag-1"}

    class FakeContainer:
        def __init__(self) -> None:
            self.deleted: dict[str, object] | None = None

        async def read_item(self, item: str, *, partition_key: str):
            assert item == record.document_id
            assert partition_key == OWNER_ID
            return document

        async def delete_item(self, item: str, **kwargs: object) -> None:
            self.deleted = {"item": item, **kwargs}

    container = FakeContainer()
    repository = AzureCosmosLinkedInTransientRepository.__new__(
        AzureCosmosLinkedInTransientRepository
    )
    repository._container = container

    consumed = asyncio.run(
        repository.consume_oauth_state(
            owner_id=OWNER_ID,
            state_hash=record.state_hash,
        )
    )

    assert consumed is not None
    assert container.deleted is not None
    assert container.deleted["etag"] == "etag-1"
    assert "IfNotModified" in str(container.deleted["match_condition"])


@pytest.mark.parametrize(
    "picture_uri",
    [
        "http://media.licdn.com/photo.jpg",
        "https://evil.example/photo.jpg",
        "https://user:password@media.licdn.com/photo.jpg",
        "https://media.licdn.com:444/photo.jpg",
        "https://media.licdn.com/photo.jpg#fragment",
    ],
)
def test_linkedin_picture_rejects_non_allowlisted_uri(picture_uri: str) -> None:
    with pytest.raises(ValueError):
        asyncio.run(
            fetch_linkedin_picture(
                picture_uri,
                allowed_hosts=frozenset({"media.licdn.com"}),
            )
        )


def test_linkedin_picture_rejects_private_dns_resolution() -> None:
    async def private_resolver(_: str) -> tuple[str, ...]:
        return ("127.0.0.1", "10.0.0.5")

    with pytest.raises(ValueError, match="public"):
        asyncio.run(
            fetch_linkedin_picture(
                "https://media.licdn.com/photo.jpg",
                allowed_hosts=frozenset({"media.licdn.com"}),
                resolver=private_resolver,
            )
        )


@pytest.mark.parametrize(
    ("status_code", "headers", "body", "message"),
    [
        (302, {"location": "https://media.licdn.com/other.jpg"}, b"", "redirect"),
        (200, {"content-type": "text/html"}, b"<html></html>", "content type"),
        (
            200,
            {
                "content-type": "image/jpeg",
                "content-length": str(4 * 1024 * 1024 + 1),
            },
            b"",
            "too large",
        ),
    ],
)
def test_linkedin_picture_rejects_unsafe_response(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    headers: dict[str, str],
    body: bytes,
    message: str,
) -> None:
    async def public_resolver(_: str) -> tuple[str, ...]:
        return ("93.184.216.34",)

    class FakeContent:
        async def iter_chunked(self, _: int):
            yield body

    class FakeResponse:
        def __init__(self) -> None:
            self.status = status_code
            self.headers = headers
            self.content = FakeContent()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def raise_for_status(self) -> None:
            if self.status >= 400:
                raise aiohttp.ClientResponseError(
                    request_info=None,
                    history=(),
                    status=self.status,
                )

    class FakeClientSession:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def get(self, *_: object, **__: object) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(linkedin_profile_module.aiohttp, "ClientSession", FakeClientSession)

    with pytest.raises(ValueError, match=message):
        asyncio.run(
            fetch_linkedin_picture(
                "https://media.licdn.com/photo.jpg",
                allowed_hosts=frozenset({"media.licdn.com"}),
                resolver=public_resolver,
            )
        )


def test_linkedin_picture_connection_uses_only_prevalidated_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def public_resolver(_: str) -> tuple[str, ...]:
        return ("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946")

    class FakeContent:
        async def iter_chunked(self, _: int):
            yield b"safe-image"

    class FakeResponse:
        status = 200
        headers = {"content-type": "image/jpeg"}
        content = FakeContent()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

    class FakeClientSession:
        def __init__(self, **kwargs: object) -> None:
            captured["connector"] = kwargs["connector"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def get(self, *_: object, **__: object) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(linkedin_profile_module.aiohttp, "ClientSession", FakeClientSession)

    result = asyncio.run(
        fetch_linkedin_picture(
            "https://media.licdn.com/photo.jpg",
            allowed_hosts=frozenset({"media.licdn.com"}),
            resolver=public_resolver,
        )
    )

    connector = captured["connector"]
    pinned_resolver = connector._resolver
    resolved = asyncio.run(pinned_resolver.resolve("media.licdn.com", 443, socket.AF_UNSPEC))
    assert {item["host"] for item in resolved} == {
        "93.184.216.34",
        "2606:2800:220:1:248:1893:25c8:1946",
    }
    with pytest.raises(OSError, match="unexpected hostname"):
        asyncio.run(pinned_resolver.resolve("internal.example", 443))
    assert result.content == b"safe-image"


def test_profile_photo_import_state_is_provider_qualified() -> None:
    repository = InMemoryProfilePhotoImportStateRepository()

    async def scenario() -> tuple[
        ProfilePhotoImportState | None,
        ProfilePhotoImportState | None,
    ]:
        await repository.save(
            ProfilePhotoImportState(
                owner_id=OWNER_ID,
                status="imported",
                photo_id="entra-photo",
            )
        )
        await repository.save(
            ProfilePhotoImportState(
                owner_id=OWNER_ID,
                status="deleted_suppressed",
                provider="linkedin",
            )
        )
        entra = await repository.get(OWNER_ID)
        linkedin = await repository.get(OWNER_ID, provider="linkedin")
        return entra, linkedin

    entra, linkedin = asyncio.run(scenario())

    assert entra is not None
    assert entra.photo_id == "entra-photo"
    assert linkedin is not None
    assert linkedin.status == "deleted_suppressed"


@pytest.mark.parametrize("value", ["https://example.com/app", "//evil.example", "app", "/app#x"])
def test_return_path_must_be_local(value: str) -> None:
    with pytest.raises(ValueError):
        validate_return_path(value)


def enable_linkedin(monkeypatch: pytest.MonkeyPatch, *, owner_id: str = TEST_OWNER_ID) -> None:
    monkeypatch.setenv("LINKEDIN_OIDC_ENABLED", "true")
    monkeypatch.setenv("LINKEDIN_CLIENT_ID", "linkedin-client")
    monkeypatch.setenv("LINKEDIN_CLIENT_SECRET", "linkedin-secret")
    monkeypatch.setenv(
        "LINKEDIN_REDIRECT_URI",
        "https://testserver/auth/linkedin/callback",
    )
    monkeypatch.setenv("LINKEDIN_PILOT_OWNER_ALLOWLIST", owner_id)


def begin_linkedin_import(
    client: TestClient,
    *,
    import_photo: bool = False,
):
    page = client.get("/app")
    assert page.status_code == 200
    data = {
        "csrf_token": extract_hidden_value(page.text, "csrf_token"),
        "creation_draft_id": extract_hidden_value(page.text, "creation_draft_id"),
    }
    if import_photo:
        data["import_linkedin_photo"] = "true"
    return client.post(
        "/auth/linkedin/start",
        data=data,
        follow_redirects=False,
    )


def decode_session_cookie(client: TestClient) -> dict[str, object]:
    cookie = client.cookies.get("fantasy_cards_session")
    assert cookie is not None
    unsigned = TimestampSigner("test-session-secret").unsign(cookie.encode("utf-8"))
    return json.loads(b64decode(unsigned))


def test_linkedin_action_is_hidden_when_feature_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_authenticated_client(monkeypatch)

    response = client.get("/app")

    assert response.status_code == 200
    assert 'action="/auth/linkedin/start"' not in response.text


def test_linkedin_action_is_hidden_for_owner_outside_pilot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_linkedin(monkeypatch, owner_id=OWNER_ID)
    monkeypatch.setattr(
        main_module,
        "create_linkedin_oauth_client",
        lambda settings: FakeLinkedInOAuthClient(),
    )
    client = make_authenticated_client(monkeypatch)

    response = client.get("/app")

    assert response.status_code == 200
    assert 'action="/auth/linkedin/start"' not in response.text


def test_linkedin_start_rejects_draft_id_not_bound_to_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_linkedin(monkeypatch)

    def unexpected_client(_: object):
        raise AssertionError("OAuth must not start for a mismatched creation draft.")

    monkeypatch.setattr(main_module, "create_linkedin_oauth_client", unexpected_client)
    client = make_authenticated_client(monkeypatch)
    page = client.get("/app")
    active_draft_id = extract_hidden_value(page.text, "creation_draft_id")

    response = client.post(
        "/auth/linkedin/start",
        data={
            "csrf_token": extract_hidden_value(page.text, "csrf_token"),
            "creation_draft_id": uuid4().hex,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/app"
    assert decode_session_cookie(client)["linkedin_creation_draft_id"] == active_draft_id


def test_linkedin_name_import_is_owner_bound_single_use_and_content_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_linkedin(monkeypatch)
    monkeypatch.setattr(
        main_module,
        "create_linkedin_oauth_client",
        lambda settings: FakeLinkedInOAuthClient(),
    )
    client = make_authenticated_client(monkeypatch)

    start = begin_linkedin_import(client)

    assert start.status_code == 307
    query = parse_qs(urlsplit(start.headers["location"]).query)
    state = query["state"][0]
    session_after_start = decode_session_cookie(client)
    serialized_session = repr(session_after_start)
    assert state not in serialized_session
    assert "must-not-persist" not in serialized_session
    assert "code_verifier" not in serialized_session
    assert len(client.cookies["fantasy_cards_session"]) <= 3500

    callback = client.get(
        f"/auth/linkedin/callback?code=linkedin-code&state={state}",
        follow_redirects=False,
    )

    assert callback.status_code == 303
    assert callback.headers["location"] == "/app"
    repository = client.app.state.services.linkedin_transient_repository
    draft_id = str(decode_session_cookie(client)["linkedin_creation_draft_id"])
    draft = asyncio.run(repository.get_draft(owner_id=TEST_OWNER_ID, draft_id=draft_id))
    assert draft is not None
    assert draft.display_name == "Ada Lovelace"
    assert draft.saved_photo_id is None
    assert "linkedin-subject" not in repr(draft)
    assert "media.licdn.com" not in repr(draft)
    assert "must-not-persist" not in repr(decode_session_cookie(client))

    replay = client.get(
        f"/auth/linkedin/callback?code=linkedin-code&state={state}",
        follow_redirects=False,
    )
    assert replay.status_code == 303
    notice = decode_session_cookie(client)["linkedin_import_notice"]
    assert isinstance(notice, dict)
    assert "already used" in str(notice["message"])


def test_linkedin_picture_is_not_fetched_without_bound_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_linkedin(monkeypatch)
    monkeypatch.setenv("LINKEDIN_PICTURE_HOST_ALLOWLIST", "media.licdn.com")
    monkeypatch.setattr(
        main_module,
        "create_linkedin_oauth_client",
        lambda settings: FakeLinkedInOAuthClient(),
    )

    async def unexpected_fetch(*args: object, **kwargs: object):
        raise AssertionError("Picture fetch must require bound consent.")

    monkeypatch.setattr(main_module, "fetch_linkedin_picture", unexpected_fetch)
    client = make_authenticated_client(monkeypatch)
    start = begin_linkedin_import(client, import_photo=False)
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]

    response = client.get(
        f"/auth/linkedin/callback?code=linkedin-code&state={state}",
        follow_redirects=False,
    )

    assert response.status_code == 303
    repository = client.app.state.services.linkedin_transient_repository
    draft_id = str(decode_session_cookie(client)["linkedin_creation_draft_id"])
    draft = asyncio.run(repository.get_draft(owner_id=TEST_OWNER_ID, draft_id=draft_id))
    assert draft is not None
    assert draft.outcome == "name_only"
    assert draft.saved_photo_id is None


def test_linkedin_draft_prefills_editable_name_and_is_consumed_after_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_linkedin(monkeypatch)
    monkeypatch.setattr(
        main_module,
        "create_linkedin_oauth_client",
        lambda settings: FakeLinkedInOAuthClient(),
    )
    client = make_authenticated_client(monkeypatch)
    start = begin_linkedin_import(client)
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    callback = client.get(
        f"/auth/linkedin/callback?code=linkedin-code&state={state}",
        follow_redirects=False,
    )
    assert callback.status_code == 303

    page = client.get("/app")
    assert 'value="Ada Lovelace"' in page.text
    assert "Imported from LinkedIn and fully editable." in page.text
    draft_id = extract_hidden_value(page.text, "creation_draft_id")
    generated = client.post(
        "/ui/cards/generate",
        data={
            "display_name": "Ada, Container Commander",
            "current_role": "Platform architect",
            "expertise": ["Container platforms"],
            "creation_draft_id": draft_id,
            "csrf_token": extract_hidden_value(page.text, "csrf_token"),
            "idempotency_key": uuid4().hex,
        },
    )

    assert generated.status_code == 200
    repository = client.app.state.services.linkedin_transient_repository
    assert asyncio.run(repository.get_draft(owner_id=TEST_OWNER_ID, draft_id=draft_id)) is None
    assert "linkedin_creation_draft_id" not in decode_session_cookie(client)


def test_linkedin_draft_is_preserved_after_structured_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_linkedin(monkeypatch)
    monkeypatch.setattr(
        main_module,
        "create_linkedin_oauth_client",
        lambda settings: FakeLinkedInOAuthClient(),
    )
    client = make_authenticated_client(monkeypatch)
    start = begin_linkedin_import(client)
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    client.get(
        f"/auth/linkedin/callback?code=linkedin-code&state={state}",
        follow_redirects=False,
    )

    page = client.get("/app")
    draft_id = extract_hidden_value(page.text, "creation_draft_id")
    response = client.post(
        "/ui/cards/generate",
        data={
            "display_name": "Ada, Container Commander",
            "current_role": "x",
            "expertise": ["Container platforms"],
            "creation_draft_id": draft_id,
            "csrf_token": extract_hidden_value(page.text, "csrf_token"),
            "idempotency_key": uuid4().hex,
        },
    )

    assert response.status_code == 422
    repository = client.app.state.services.linkedin_transient_repository
    assert asyncio.run(repository.get_draft(owner_id=TEST_OWNER_ID, draft_id=draft_id)) is not None
    assert decode_session_cookie(client)["linkedin_creation_draft_id"] == draft_id


def test_structured_generation_rejects_draft_id_not_bound_to_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_authenticated_client(monkeypatch)
    page = client.get("/app")
    active_draft_id = extract_hidden_value(page.text, "creation_draft_id")
    submitted_draft_id = uuid4().hex
    repository = client.app.state.services.linkedin_transient_repository
    expires_at = transient_expiry()

    async def seed_drafts() -> None:
        for draft_id in (active_draft_id, submitted_draft_id):
            await repository.replace_draft(
                LinkedInImportDraft(
                    owner_id=TEST_OWNER_ID,
                    draft_id=draft_id,
                    display_name="Ada Lovelace",
                    saved_photo_id=None,
                    outcome="name_only",
                    expires_at=expires_at,
                )
            )

    asyncio.run(seed_drafts())
    response = client.post(
        "/ui/cards/generate",
        data={
            "display_name": "Ada Lovelace",
            "current_role": "Platform architect",
            "expertise": ["Container platforms"],
            "creation_draft_id": submitted_draft_id,
            "csrf_token": extract_hidden_value(page.text, "csrf_token"),
            "idempotency_key": uuid4().hex,
        },
    )

    assert response.status_code == 422
    assert response.headers["x-generation-error"] == "invalid_creation_draft"
    assert (
        asyncio.run(repository.get_draft(owner_id=TEST_OWNER_ID, draft_id=active_draft_id))
        is not None
    )
    assert (
        asyncio.run(repository.get_draft(owner_id=TEST_OWNER_ID, draft_id=submitted_draft_id))
        is not None
    )
    assert decode_session_cookie(client)["linkedin_creation_draft_id"] == active_draft_id


def test_linkedin_picture_is_not_fetched_without_proven_host_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_linkedin(monkeypatch)
    monkeypatch.setattr(
        main_module,
        "create_linkedin_oauth_client",
        lambda settings: FakeLinkedInOAuthClient(),
    )

    async def unexpected_fetch(*args: object, **kwargs: object):
        raise AssertionError("Picture fetch must require a proven host allowlist.")

    monkeypatch.setattr(main_module, "fetch_linkedin_picture", unexpected_fetch)
    client = make_authenticated_client(monkeypatch)
    start = begin_linkedin_import(client, import_photo=True)
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]

    response = client.get(
        f"/auth/linkedin/callback?code=linkedin-code&state={state}",
        follow_redirects=False,
    )

    assert response.status_code == 303
    repository = client.app.state.services.linkedin_transient_repository
    draft_id = str(decode_session_cookie(client)["linkedin_creation_draft_id"])
    draft = asyncio.run(repository.get_draft(owner_id=TEST_OWNER_ID, draft_id=draft_id))
    assert draft is not None
    assert draft.outcome == "name_only"
    assert draft.saved_photo_id is None


def test_linkedin_picture_failure_preserves_name_only_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_linkedin(monkeypatch)
    monkeypatch.setenv("LINKEDIN_PICTURE_HOST_ALLOWLIST", "media.licdn.com")
    monkeypatch.setattr(
        main_module,
        "create_linkedin_oauth_client",
        lambda settings: FakeLinkedInOAuthClient(),
    )

    async def rejected_fetch(*args: object, **kwargs: object):
        raise ValueError("Unsafe picture")

    monkeypatch.setattr(main_module, "fetch_linkedin_picture", rejected_fetch)
    client = make_authenticated_client(monkeypatch)
    start = begin_linkedin_import(client, import_photo=True)
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]

    response = client.get(
        f"/auth/linkedin/callback?code=linkedin-code&state={state}",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert client.get("/app").status_code == 200
    repository = client.app.state.services.linkedin_transient_repository
    draft_id = str(decode_session_cookie(client)["linkedin_creation_draft_id"])
    draft = asyncio.run(repository.get_draft(owner_id=TEST_OWNER_ID, draft_id=draft_id))
    assert draft is not None
    assert draft.display_name == "Ada Lovelace"
    assert draft.outcome == "name_only"
    assert draft.saved_photo_id is None


def test_linkedin_denial_consumes_state_without_creating_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_linkedin(monkeypatch)
    monkeypatch.setattr(
        main_module,
        "create_linkedin_oauth_client",
        lambda settings: FakeLinkedInOAuthClient(),
    )
    client = make_authenticated_client(monkeypatch)
    start = begin_linkedin_import(client)
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    draft_id = str(decode_session_cookie(client)["linkedin_creation_draft_id"])

    denied = client.get(
        f"/auth/linkedin/callback?error=access_denied&state={state}",
        follow_redirects=False,
    )

    assert denied.status_code == 303
    repository = client.app.state.services.linkedin_transient_repository
    assert asyncio.run(repository.get_draft(owner_id=TEST_OWNER_ID, draft_id=draft_id)) is None
    replay = client.get(
        f"/auth/linkedin/callback?error=access_denied&state={state}",
        follow_redirects=False,
    )
    notice = decode_session_cookie(client)["linkedin_import_notice"]
    assert replay.status_code == 303
    assert isinstance(notice, dict)
    assert "already used" in str(notice["message"])


def test_linkedin_callback_requires_current_entra_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_linkedin(monkeypatch)
    client = TestClient(main_module.create_app(), base_url="https://testserver")

    response = client.get(
        "/auth/linkedin/callback?code=linkedin-code&state=opaque",
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == "/auth/login"
