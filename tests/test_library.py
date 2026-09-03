from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.generation import StoredCard, create_services
from app.library import format_card_timestamp
from app.main import create_app
from app.settings import load_app_settings
from tests.conftest import TEST_OWNER_ID, FakeOAuthClient

OTHER_OWNER_ID = "00000000-0000-0000-0000-000000000999:11111111-1111-1111-1111-111111111999"


def make_authenticated_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    services = create_services(load_app_settings())
    monkeypatch.setattr(main_module, "create_oauth_client", lambda settings: FakeOAuthClient())
    client = TestClient(create_app(services=services), base_url="https://testserver")
    login_response = client.get("/auth/login", follow_redirects=False)
    assert login_response.status_code == 307
    callback_response = client.get(
        "/auth/callback?code=valid-code&state=opaque",
        follow_redirects=False,
    )
    assert callback_response.status_code == 303
    return client


def store_card(client: TestClient, record: StoredCard) -> None:
    asyncio.run(client.app.state.services.card_repository.save(record))


def make_card(
    *,
    owner_id: str,
    card_id: str,
    name: str,
    status: str = "completed",
    blob_name: str | None = None,
    image_url_path: str | None = None,
) -> StoredCard:
    return StoredCard(
        id=card_id,
        document_type="card",
        owner_id=owner_id,
        request_id=f"req-{card_id}",
        idempotency_key=f"idem-{card_id}",
        request_hash=f"hash-{card_id}",
        status=status,
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
        image_url_path=image_url_path,
        completed_at="2026-09-02T14:00:00Z" if status == "completed" else None,
        created_at="2026-09-02T13:00:00Z",
        updated_at="2026-09-02T14:00:00Z",
    )


def store_asset(client: TestClient, blob_name: str, payload: bytes, content_type: str) -> None:
    asyncio.run(client.app.state.services.asset_store.upload(blob_name, payload, content_type))


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        ("2026-09-03T08:04:01Z", "Sep 3, 2026, 8:04 AM UTC"),
        ("2026-09-03T20:15:00+02:00", "Sep 3, 2026, 6:15 PM UTC"),
        ("not-a-timestamp", "not-a-timestamp"),
        (None, None),
    ],
)
def test_format_card_timestamp_handles_expected_inputs(
    timestamp: str | None,
    expected: str | None,
) -> None:
    assert format_card_timestamp(timestamp) == expected


def test_my_cards_requires_authentication() -> None:
    client = TestClient(create_app(), base_url="https://testserver")

    response = client.get("/my/cards")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Session"


def test_my_cards_renders_only_signed_in_users_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_authenticated_client(monkeypatch)
    store_card(
        client,
        make_card(
            owner_id=TEST_OWNER_ID,
            card_id="own-card",
            name="Legolas of Ithilien",
            blob_name="cards/own-card.png",
            image_url_path="/cards/own-card/image",
        ),
    )
    store_card(
        client,
        make_card(
            owner_id=OTHER_OWNER_ID,
            card_id="other-card",
            name="Boromir of Gondor",
            blob_name="cards/other-card.png",
            image_url_path="/cards/other-card/image",
        ),
    )

    response = client.get("/my/cards")

    assert response.status_code == 200
    assert "Legolas of Ithilien" in response.text
    assert "Boromir of Gondor" not in response.text
    assert "/my/cards/own-card" in response.text
    assert "/cards/own-card/image" in response.text
    assert "cards/other-card.png" not in response.text
    assert (
        'Created <time datetime="2026-09-02T13:00:00Z">Sep 2, 2026, 1:00 PM UTC</time>'
        in response.text
    )


def test_my_cards_shows_empty_state(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_authenticated_client(monkeypatch)

    response = client.get("/my/cards")

    assert response.status_code == 200
    assert "No cards yet" in response.text
    assert "Your library is empty." in response.text


def test_my_photos_library_requires_authentication() -> None:
    client = TestClient(create_app(), base_url="https://testserver")

    response = client.get("/my/photos/library")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Session"


def test_my_photos_library_renders_management_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_authenticated_client(monkeypatch)

    response = client.get("/my/photos/library")

    assert response.status_code == 200
    assert "My Photos" in response.text
    assert "Saved reference photos" in response.text
    assert "data-photo-library-manager" in response.text
    assert 'data-photo-library-endpoint="/my/photos"' in response.text
    assert "Delete any photo you no longer want to keep" in response.text


def test_my_card_detail_requires_authentication() -> None:
    client = TestClient(create_app(), base_url="https://testserver")

    response = client.get("/my/cards/any-card")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Session"


def test_my_card_detail_renders_owned_card(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_authenticated_client(monkeypatch)
    store_card(
        client,
        make_card(
            owner_id=TEST_OWNER_ID,
            card_id="detail-card",
            name="Detail Ranger",
            blob_name="cards/detail-card.png",
            image_url_path="/cards/detail-card/image",
        ),
    )

    response = client.get("/my/cards/detail-card")

    assert response.status_code == 200
    assert "Detail Ranger" in response.text
    assert "Prompt for Detail Ranger" in response.text
    assert "moonlit precision" in response.text
    assert "/cards/detail-card/image" in response.text
    assert '<time datetime="2026-09-02T13:00:00Z">Sep 2, 2026, 1:00 PM UTC</time>' in response.text
    assert '<time datetime="2026-09-02T14:00:00Z">Sep 2, 2026, 2:00 PM UTC</time>' in response.text
    assert "data-confirm-modal-form" in response.text
    assert 'data-confirm-title="Delete this card?"' in response.text
    assert 'data-confirm-confirm-label="Delete card"' in response.text
    assert "I understand this permanently deletes this card." in response.text


def test_my_card_detail_omits_completed_date_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_authenticated_client(monkeypatch)
    store_card(
        client,
        make_card(
            owner_id=TEST_OWNER_ID,
            card_id="pending-card",
            name="Pending Ranger",
            status="pending",
        ),
    )

    response = client.get("/my/cards/pending-card")

    assert response.status_code == 200
    assert "Pending Ranger" in response.text
    assert "Completed" not in response.text


def test_my_card_detail_returns_404_for_other_users_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_authenticated_client(monkeypatch)
    store_card(
        client,
        make_card(
            owner_id=OTHER_OWNER_ID,
            card_id="foreign-card",
            name="Hidden Enemy",
            blob_name="cards/foreign-card.png",
            image_url_path="/cards/foreign-card/image",
        ),
    )

    response = client.get("/my/cards/foreign-card")

    assert response.status_code == 404
    assert response.json()["detail"] == "No card was found for this user."


def test_card_image_requires_authentication() -> None:
    client = TestClient(create_app(), base_url="https://testserver")

    response = client.get("/cards/any-card/image")

    assert response.status_code == 401


def test_card_image_returns_404_for_other_users_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_authenticated_client(monkeypatch)
    store_card(
        client,
        make_card(
            owner_id=OTHER_OWNER_ID,
            card_id="foreign-card-image",
            name="Hidden Enemy",
            blob_name="cards/foreign-card-image.png",
            image_url_path="/cards/foreign-card-image/image",
        ),
    )
    store_asset(client, "cards/foreign-card-image.png", b"fake-bytes", "image/png")

    response = client.get("/cards/foreign-card-image/image")

    assert response.status_code == 404


def test_card_image_streams_bytes_for_owned_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_authenticated_client(monkeypatch)
    store_card(
        client,
        make_card(
            owner_id=TEST_OWNER_ID,
            card_id="owned-card-image",
            name="Owned Ranger",
            blob_name="cards/owned-card-image.png",
            image_url_path="/cards/owned-card-image/image",
        ),
    )
    store_asset(client, "cards/owned-card-image.png", b"fake-png-bytes", "image/png")

    response = client.get("/cards/owned-card-image/image")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == b"fake-png-bytes"
