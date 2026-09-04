from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.deletion import DeletionAuditRecord
from app.generation import StoredCard, create_services
from app.main import create_app
from app.photos import StoredSavedPhoto
from app.settings import load_app_settings
from tests.conftest import TEST_OWNER_ID, FakeOAuthClient

OTHER_OWNER_ID = "00000000-0000-0000-0000-000000000999:11111111-1111-1111-1111-111111111999"


def make_authenticated_client(
    monkeypatch: pytest.MonkeyPatch,
    services=None,
) -> TestClient:
    resolved_services = services or create_services(load_app_settings())
    monkeypatch.setattr(main_module, "create_oauth_client", lambda settings: FakeOAuthClient())
    client = TestClient(create_app(services=resolved_services), base_url="https://testserver")
    login_response = client.get("/auth/login", follow_redirects=False)
    assert login_response.status_code == 307
    callback_response = client.get(
        "/auth/callback?code=valid-code&state=opaque",
        follow_redirects=False,
    )
    assert callback_response.status_code == 303
    return client


def csrf_token(client: TestClient) -> str:
    response = client.get("/app")
    marker = 'name="csrf_token" value="'
    start = response.text.index(marker) + len(marker)
    end = response.text.index('"', start)
    return response.text[start:end]


def make_card(
    *,
    owner_id: str,
    card_id: str,
    name: str,
    blob_name: str | None = None,
) -> StoredCard:
    return StoredCard(
        id=card_id,
        document_type="card",
        owner_id=owner_id,
        request_id=f"req-{card_id}",
        idempotency_key=f"idem-{card_id}",
        request_hash=f"hash-{card_id}",
        status="completed",
        prompt=f"Prompt for {name}",
        validated_payload={
            "schemaVersion": 1,
            "name": name,
            "cardType": "hero",
            "rarity": "rare",
            "manaCost": 4,
            "attack": 5,
            "health": 4,
            "rulesText": f"{name} strikes with moonlit precision.",
            "flavorText": f"{name} never misses.",
            "artBrief": f"{name} under a silver sky.",
        },
        blob_name=blob_name,
        blob_content_type="image/png" if blob_name else None,
        blob_sha256="abc123" if blob_name else None,
        blob_size_bytes=128 if blob_name else None,
        image_url_path=f"/cards/{card_id}/image" if blob_name else None,
        moderation=[{"stage": "post_image", "allowed": True, "reasonCode": "allowed"}],
        completed_at="2026-09-02T14:00:00Z",
        created_at="2026-09-02T13:00:00Z",
        updated_at="2026-09-02T14:00:00Z",
    )


def store_card(client: TestClient, record: StoredCard) -> None:
    asyncio.run(client.app.state.services.card_repository.save(record))


def store_generation_audit(client: TestClient, record: StoredCard) -> None:
    asyncio.run(client.app.state.services.audit_repository.save_audit(record))


def store_asset(client: TestClient, blob_name: str, payload: bytes, content_type: str) -> None:
    asyncio.run(client.app.state.services.asset_store.upload(blob_name, payload, content_type))


def store_photo_asset(
    client: TestClient, blob_name: str, payload: bytes, content_type: str
) -> None:
    asyncio.run(
        client.app.state.services.photo_asset_store.upload(blob_name, payload, content_type)
    )


def store_saved_photo(client: TestClient, record: StoredSavedPhoto) -> None:
    asyncio.run(client.app.state.services.saved_photo_repository.save(record))


def list_deletion_audits(client: TestClient, owner_id: str) -> list[DeletionAuditRecord]:
    return asyncio.run(client.app.state.services.deletion_audit_repository.list_by_owner(owner_id))


def list_generation_audits(client: TestClient, owner_id: str) -> list[StoredCard]:
    return asyncio.run(client.app.state.services.audit_repository.list_audits_by_owner(owner_id))


class FlakyDeleteAssetStore:
    def __init__(self, wrapped_store, *, failing_blob_names: set[str]) -> None:
        self._wrapped_store = wrapped_store
        self._failing_blob_names = set(failing_blob_names)

    async def upload(self, blob_name: str, payload: bytes, content_type: str):
        return await self._wrapped_store.upload(blob_name, payload, content_type)

    async def download(self, blob_name: str):
        return await self._wrapped_store.download(blob_name)

    async def delete(self, blob_name: str) -> None:
        if blob_name in self._failing_blob_names:
            self._failing_blob_names.remove(blob_name)
            raise RuntimeError(f"transient delete failure for {blob_name}")
        await self._wrapped_store.delete(blob_name)


def test_delete_card_removes_document_and_blob_and_records_ttl_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_authenticated_client(monkeypatch)
    token = csrf_token(client)
    store_card(
        client,
        make_card(
            owner_id=TEST_OWNER_ID,
            card_id="delete-me",
            name="Delete Me",
            blob_name="cards/delete-me.png",
        ),
    )
    store_card(
        client,
        make_card(
            owner_id=OTHER_OWNER_ID,
            card_id="keep-me",
            name="Keep Me",
            blob_name="cards/keep-me.png",
        ),
    )
    store_generation_audit(
        client,
        StoredCard(
            id="delete-me",
            document_type="generation-audit",
            owner_id=TEST_OWNER_ID,
            request_id="req-delete-audit",
            idempotency_key="idem-delete-audit",
            request_hash="hash-delete-audit",
            status="audit_completed",
            ttl_seconds=30 * 24 * 60 * 60,
        ),
    )
    store_asset(client, "cards/delete-me.png", b"delete-bytes", "image/png")
    store_asset(client, "cards/keep-me.png", b"keep-bytes", "image/png")

    response = client.delete("/api/v1/cards/delete-me", headers={"x-csrf-token": token})

    assert response.status_code == 204
    assert (
        asyncio.run(client.app.state.services.card_repository.get(TEST_OWNER_ID, "delete-me"))
        is None
    )
    assert (
        asyncio.run(client.app.state.services.card_repository.get(OTHER_OWNER_ID, "keep-me"))
        is not None
    )
    assert list_generation_audits(client, TEST_OWNER_ID) == []
    with pytest.raises(FileNotFoundError):
        asyncio.run(client.app.state.services.asset_store.download("cards/delete-me.png"))
    assert asyncio.run(client.app.state.services.asset_store.download("cards/keep-me.png")) == (
        b"keep-bytes",
        "image/png",
    )

    audits = list_deletion_audits(client, TEST_OWNER_ID)
    assert len(audits) == 1
    audit = audits[0]
    assert audit.request_id == response.headers["x-request-id"]
    assert audit.ttl_seconds == 30 * 24 * 60 * 60
    assert audit.moderation_outcome == "allowed"
    assert audit.cost_estimate is None
    assert audit.timestamps.requested_at
    assert audit.timestamps.deleted_at
    assert audit.timestamps.asset_cleanup_queued_at
    assert audit.timestamps.asset_cleanup_completed_at


def test_delete_account_continues_cleanup_after_one_blob_delete_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = create_services(load_app_settings())
    services.asset_store = FlakyDeleteAssetStore(
        services.asset_store,
        failing_blob_names={"cards/second-card.png"},
    )
    client = make_authenticated_client(monkeypatch, services=services)
    token = csrf_token(client)
    store_card(
        client,
        make_card(
            owner_id=TEST_OWNER_ID,
            card_id="first-card",
            name="First Card",
            blob_name="cards/first-card.png",
        ),
    )
    store_card(
        client,
        make_card(
            owner_id=TEST_OWNER_ID,
            card_id="second-card",
            name="Second Card",
            blob_name="cards/second-card.png",
        ),
    )
    store_card(
        client,
        make_card(
            owner_id=TEST_OWNER_ID,
            card_id="third-card",
            name="Third Card",
            blob_name="cards/third-card.png",
        ),
    )
    store_asset(client, "cards/first-card.png", b"first", "image/png")
    store_asset(client, "cards/second-card.png", b"second", "image/png")
    store_asset(client, "cards/third-card.png", b"third", "image/png")

    response = client.delete("/api/v1/account", headers={"x-csrf-token": token})

    assert response.status_code == 204
    with pytest.raises(FileNotFoundError):
        asyncio.run(services.asset_store.download("cards/first-card.png"))
    assert asyncio.run(services.asset_store.download("cards/second-card.png")) == (
        b"second",
        "image/png",
    )
    with pytest.raises(FileNotFoundError):
        asyncio.run(services.asset_store.download("cards/third-card.png"))

    audits = list_deletion_audits(client, TEST_OWNER_ID)
    assert len(audits) == 1
    audit = audits[0]
    assert audit.timestamps.asset_cleanup_queued_at
    assert audit.timestamps.asset_cleanup_completed_at is None


def test_delete_account_removes_owned_data_clears_session_and_preserves_minimal_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_authenticated_client(monkeypatch)
    token = csrf_token(client)
    store_card(
        client,
        make_card(
            owner_id=TEST_OWNER_ID,
            card_id="account-card",
            name="Account Card",
            blob_name="cards/account-card.png",
        ),
    )
    store_card(
        client,
        make_card(
            owner_id=OTHER_OWNER_ID,
            card_id="other-card",
            name="Other Card",
            blob_name="cards/other-card.png",
        ),
    )
    store_generation_audit(
        client,
        StoredCard(
            id="account-card",
            document_type="generation-audit",
            owner_id=TEST_OWNER_ID,
            request_id="req-audit",
            idempotency_key="idem-audit",
            request_hash="hash-audit",
            status="audit_completed",
            ttl_seconds=30 * 24 * 60 * 60,
        ),
    )
    store_saved_photo(
        client,
        StoredSavedPhoto(
            photo_id="saved-photo-1",
            owner_id=TEST_OWNER_ID,
            label="Portrait",
            blob_name="photos/owner/saved-photo-1/original.png",
            blob_content_type="image/png",
            blob_sha256="photo-sha",
            blob_size_bytes=42,
            image_url_path="/my/photos/saved-photo-1/image",
            thumbnail_blob_name="photos/owner/saved-photo-1/thumb.png",
            thumbnail_image_url_path="/my/photos/saved-photo-1/thumbnail",
            created_at="2026-09-02T13:00:00Z",
            updated_at="2026-09-02T14:00:00Z",
        ),
    )
    store_saved_photo(
        client,
        StoredSavedPhoto(
            photo_id="saved-photo-2",
            owner_id=OTHER_OWNER_ID,
            label="Other portrait",
            blob_name="photos/other/saved-photo-2/original.png",
            blob_content_type="image/png",
            blob_sha256="photo-sha-other",
            blob_size_bytes=42,
            image_url_path="/my/photos/saved-photo-2/image",
            thumbnail_blob_name="photos/other/saved-photo-2/thumb.png",
            thumbnail_image_url_path="/my/photos/saved-photo-2/thumbnail",
            created_at="2026-09-02T13:00:00Z",
            updated_at="2026-09-02T14:00:00Z",
        ),
    )
    store_asset(client, "cards/account-card.png", b"owner-card-bytes", "image/png")
    store_asset(client, "cards/other-card.png", b"other-card-bytes", "image/png")
    store_photo_asset(
        client,
        "photos/owner/saved-photo-1/original.png",
        b"owner-photo-bytes",
        "image/png",
    )
    store_photo_asset(
        client,
        "photos/owner/saved-photo-1/thumb.png",
        b"owner-thumb-bytes",
        "image/png",
    )
    store_photo_asset(
        client,
        "photos/other/saved-photo-2/original.png",
        b"other-photo-bytes",
        "image/png",
    )
    store_photo_asset(
        client,
        "photos/other/saved-photo-2/thumb.png",
        b"other-thumb-bytes",
        "image/png",
    )

    response = client.delete("/api/v1/account", headers={"x-csrf-token": token})

    assert response.status_code == 204
    assert (
        asyncio.run(client.app.state.services.card_repository.get(TEST_OWNER_ID, "account-card"))
        is None
    )
    assert (
        asyncio.run(client.app.state.services.card_repository.get(OTHER_OWNER_ID, "other-card"))
        is not None
    )
    assert (
        asyncio.run(
            client.app.state.services.saved_photo_repository.get(TEST_OWNER_ID, "saved-photo-1")
        )
        is None
    )
    assert (
        asyncio.run(
            client.app.state.services.saved_photo_repository.get(OTHER_OWNER_ID, "saved-photo-2")
        )
        is not None
    )
    assert list_generation_audits(client, TEST_OWNER_ID) == []
    with pytest.raises(FileNotFoundError):
        asyncio.run(client.app.state.services.asset_store.download("cards/account-card.png"))
    with pytest.raises(FileNotFoundError):
        asyncio.run(
            client.app.state.services.photo_asset_store.download(
                "photos/owner/saved-photo-1/original.png"
            )
        )
    with pytest.raises(FileNotFoundError):
        asyncio.run(
            client.app.state.services.photo_asset_store.download(
                "photos/owner/saved-photo-1/thumb.png"
            )
        )
    assert asyncio.run(client.app.state.services.asset_store.download("cards/other-card.png")) == (
        b"other-card-bytes",
        "image/png",
    )
    assert asyncio.run(
        client.app.state.services.photo_asset_store.download(
            "photos/other/saved-photo-2/original.png"
        )
    ) == (b"other-photo-bytes", "image/png")

    audits = list_deletion_audits(client, TEST_OWNER_ID)
    assert len(audits) == 1
    audit = audits[0]
    assert audit.request_id == response.headers["x-request-id"]
    assert audit.ttl_seconds == 30 * 24 * 60 * 60
    assert audit.moderation_outcome == "allowed"
    assert audit.cost_estimate is None
    assert audit.timestamps.deleted_at
    assert audit.timestamps.asset_cleanup_completed_at

    follow_up = client.get("/my/cards")
    assert follow_up.status_code == 401


def test_delete_card_returns_404_for_missing_card(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_authenticated_client(monkeypatch)
    token = csrf_token(client)

    response = client.delete("/api/v1/cards/missing-card", headers={"x-csrf-token": token})

    assert response.status_code == 404
    assert response.json()["errorCode"] == "card_not_found"


def test_delete_card_returns_404_for_other_users_card(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_authenticated_client(monkeypatch)
    token = csrf_token(client)
    store_card(
        client,
        make_card(
            owner_id=OTHER_OWNER_ID,
            card_id="other-users-card",
            name="Foreign Card",
            blob_name="cards/other-users-card.png",
        ),
    )

    response = client.delete(
        "/api/v1/cards/other-users-card",
        headers={"x-csrf-token": token},
    )

    assert response.status_code == 404
    assert response.json()["errorCode"] == "card_not_found"


def test_delete_account_succeeds_when_user_has_no_cards_or_photos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_authenticated_client(monkeypatch)
    token = csrf_token(client)

    response = client.delete("/api/v1/account", headers={"x-csrf-token": token})

    assert response.status_code == 204
    audits = list_deletion_audits(client, TEST_OWNER_ID)
    assert len(audits) == 1
    audit = audits[0]
    assert audit.timestamps.deleted_at
    assert audit.timestamps.asset_cleanup_queued_at is None
    assert audit.timestamps.asset_cleanup_completed_at is None

    follow_up = client.get("/my/cards")
    assert follow_up.status_code == 401


def test_my_account_page_renders_deletion_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_authenticated_client(monkeypatch)

    response = client.get("/my/account")

    assert response.status_code == 200
    assert "Delete my account" in response.text
    assert "retained for 30 days" in response.text
    assert "data-confirm-modal-form" in response.text
    assert 'data-confirm-title="Delete your account permanently?"' in response.text
    assert 'data-confirm-confirm-label="Delete account"' in response.text
    assert "Delete your account and all generated content permanently?" in response.text
    assert (
        "I understand this permanently deletes my account and all generated content."
        in response.text
    )


def test_my_cards_page_renders_multi_select_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_authenticated_client(monkeypatch)
    store_card(
        client,
        make_card(
            owner_id=TEST_OWNER_ID,
            card_id="selectable-card",
            name="Selectable Card",
            blob_name="cards/selectable-card.png",
        ),
    )

    response = client.get("/my/cards")

    assert response.status_code == 200
    assert 'action="/my/cards/batch-delete"' in response.text
    assert "data-card-selection-toggle" in response.text
    assert "data-card-selection-all" in response.text
    assert "data-card-selection-count" in response.text
    assert "data-card-selection-delete" in response.text
    assert 'name="card_ids"' in response.text
    assert 'value="selectable-card"' in response.text
    assert 'aria-label="Select Selectable Card"' in response.text
    assert 'data-confirm-title="Delete selected cards?"' in response.text


def test_batch_delete_removes_every_selected_card_and_reports_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_authenticated_client(monkeypatch)
    token = csrf_token(client)
    for card_id in ("batch-one", "batch-two", "batch-three"):
        store_card(
            client,
            make_card(
                owner_id=TEST_OWNER_ID,
                card_id=card_id,
                name=card_id.replace("-", " ").title(),
                blob_name=f"cards/{card_id}.png",
            ),
        )
        store_asset(client, f"cards/{card_id}.png", b"bytes", "image/png")

    response = client.post(
        "/my/cards/batch-delete",
        data={
            "csrf_token": token,
            "card_ids": ["batch-one", "batch-two", "batch-three"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/my/cards?deleted=3&failed=0"
    for card_id in ("batch-one", "batch-two", "batch-three"):
        assert (
            asyncio.run(client.app.state.services.card_repository.get(TEST_OWNER_ID, card_id))
            is None
        )
        with pytest.raises(FileNotFoundError):
            asyncio.run(client.app.state.services.asset_store.download(f"cards/{card_id}.png"))
    assert len(list_deletion_audits(client, TEST_OWNER_ID)) == 3

    follow_up = client.get("/my/cards?deleted=3&failed=0")
    assert "3 cards permanently deleted." in follow_up.text
    assert "batch-one" not in follow_up.text


def test_batch_delete_select_all_deletes_every_displayed_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_authenticated_client(monkeypatch)
    token = csrf_token(client)
    store_card(
        client,
        make_card(owner_id=TEST_OWNER_ID, card_id="all-one", name="All One"),
    )
    store_card(
        client,
        make_card(owner_id=TEST_OWNER_ID, card_id="all-two", name="All Two"),
    )
    listing = client.get("/my/cards")
    displayed_card_ids = [
        card_id for card_id in ("all-one", "all-two") if f'value="{card_id}"' in listing.text
    ]
    assert displayed_card_ids == ["all-one", "all-two"]

    response = client.post(
        "/my/cards/batch-delete",
        data={"csrf_token": token, "card_ids": displayed_card_ids},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/my/cards?deleted=2&failed=0"
    assert asyncio.run(client.app.state.services.card_repository.list_by_owner(TEST_OWNER_ID)) == []


def test_batch_delete_without_selection_keeps_every_card(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_authenticated_client(monkeypatch)
    token = csrf_token(client)
    store_card(
        client,
        make_card(owner_id=TEST_OWNER_ID, card_id="untouched-card", name="Untouched Card"),
    )

    response = client.post(
        "/my/cards/batch-delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/my/cards"
    assert (
        asyncio.run(client.app.state.services.card_repository.get(TEST_OWNER_ID, "untouched-card"))
        is not None
    )
    assert list_deletion_audits(client, TEST_OWNER_ID) == []


def test_batch_delete_reports_partial_failure_and_keeps_failed_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_authenticated_client(monkeypatch)
    token = csrf_token(client)
    store_card(
        client,
        make_card(owner_id=TEST_OWNER_ID, card_id="partial-ok", name="Partial Ok"),
    )
    store_card(
        client,
        make_card(owner_id=TEST_OWNER_ID, card_id="partial-kept", name="Partial Kept"),
    )

    response = client.post(
        "/my/cards/batch-delete",
        data={
            "csrf_token": token,
            "card_ids": ["partial-ok", "partial-missing", "partial-ok"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/my/cards?deleted=1&failed=1"
    assert (
        asyncio.run(client.app.state.services.card_repository.get(TEST_OWNER_ID, "partial-ok"))
        is None
    )
    assert (
        asyncio.run(client.app.state.services.card_repository.get(TEST_OWNER_ID, "partial-kept"))
        is not None
    )
    assert len(list_deletion_audits(client, TEST_OWNER_ID)) == 1

    follow_up = client.get("/my/cards?deleted=1&failed=1")
    assert "1 card permanently deleted." in follow_up.text
    assert "1 card could not be deleted" in follow_up.text
    assert "Partial Kept" in follow_up.text


def test_batch_delete_ignores_cards_owned_by_other_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_authenticated_client(monkeypatch)
    token = csrf_token(client)
    store_card(
        client,
        make_card(owner_id=TEST_OWNER_ID, card_id="mine", name="Mine"),
    )
    store_card(
        client,
        make_card(owner_id=OTHER_OWNER_ID, card_id="not-mine", name="Not Mine"),
    )

    response = client.post(
        "/my/cards/batch-delete",
        data={"csrf_token": token, "card_ids": ["mine", "not-mine"]},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/my/cards?deleted=1&failed=1"
    assert (
        asyncio.run(client.app.state.services.card_repository.get(OTHER_OWNER_ID, "not-mine"))
        is not None
    )
    assert asyncio.run(client.app.state.services.card_repository.get(TEST_OWNER_ID, "mine")) is None


def test_batch_delete_rejects_missing_csrf_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_authenticated_client(monkeypatch)
    store_card(
        client,
        make_card(owner_id=TEST_OWNER_ID, card_id="csrf-card", name="Csrf Card"),
    )

    response = client.post(
        "/my/cards/batch-delete",
        data={"card_ids": ["csrf-card"]},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert (
        asyncio.run(client.app.state.services.card_repository.get(TEST_OWNER_ID, "csrf-card"))
        is not None
    )
