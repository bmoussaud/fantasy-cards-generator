from __future__ import annotations

import asyncio
from dataclasses import replace
from io import BytesIO
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from starlette.responses import RedirectResponse

from app import main as main_module
from app.generation import (
    AppServices,
    MockAIClient,
    ReferenceImageUpload,
    create_services,
)
from app.main import create_app
from app.photos import SavedPhotoResponseModel
from app.settings import load_app_settings

TEST_TENANT_ID = "11111111-1111-1111-1111-111111111111"
TEST_OBJECT_ID = "22222222-2222-2222-2222-222222222222"
TEST_OWNER_ID = f"{TEST_TENANT_ID}:{TEST_OBJECT_ID}"
OTHER_TENANT_ID = "33333333-3333-3333-3333-333333333333"
OTHER_OBJECT_ID = "44444444-4444-4444-4444-444444444444"
OTHER_OWNER_ID = f"{OTHER_TENANT_ID}:{OTHER_OBJECT_ID}"


class FakeOAuthClient:
    def __init__(self, *, tenant_id: str, object_id: str, name: str) -> None:
        self._tenant_id = tenant_id
        self._object_id = object_id
        self._name = name

    async def load_server_metadata(self) -> dict[str, str]:
        return {"issuer": "https://login.microsoftonline.com/{tenantid}/v2.0"}

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

    async def authorize_access_token(self, request, **_: object) -> dict[str, object]:
        assert request.query_params["code"] == "valid-code"
        return {
            "id_token": "signed-id-token",
            "access_token": "unused",
            "userinfo": {
                "sub": f"user-{self._object_id}",
                "name": self._name,
                "email": f"{self._name.lower()}@example.com",
                "tid": self._tenant_id,
                "oid": self._object_id,
            },
        }


class AllowAllPhotoModerationService:
    async def assert_allowed(self, photo: ReferenceImageUpload) -> list[dict[str, object]]:
        return [
            {"category": "Hate", "severity": 0},
            {"category": "SelfHarm", "severity": 0},
            {"category": "Sexual", "severity": 0},
            {"category": "Violence", "severity": 0},
        ]


class RejectingPhotoModerationService:
    async def assert_allowed(self, photo: ReferenceImageUpload) -> list[dict[str, object]]:
        raise main_module.ProblemDetails(
            status_code=422,
            title="Saved Photo Rejected",
            detail=(
                "The uploaded photo could not be saved because it exceeded "
                "the allowed safety threshold (Sexual=4)."
            ),
            type="/problems/saved-photo-rejected",
            error_code="saved_photo_rejected",
        )


def make_png_bytes(*, width: int = 500, height: int = 300, color: str = "navy") -> bytes:
    image = Image.new("RGB", (width, height), color=color)
    payload = BytesIO()
    image.save(payload, format="PNG")
    return payload.getvalue()


def make_authenticated_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    services: AppServices,
    tenant_id: str = TEST_TENANT_ID,
    object_id: str = TEST_OBJECT_ID,
    name: str = "Aragorn",
) -> TestClient:
    monkeypatch.setattr(
        main_module,
        "create_oauth_client",
        lambda settings: FakeOAuthClient(tenant_id=tenant_id, object_id=object_id, name=name),
    )
    client = TestClient(create_app(services=services), base_url="https://testserver")
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
    assert response.status_code == 200
    marker = 'name="csrf_token" value="'
    start = response.text.index(marker) + len(marker)
    end = response.text.index('"', start)
    return response.text[start:end]


def build_services(monkeypatch: pytest.MonkeyPatch, **env_overrides: str) -> AppServices:
    for key, value in env_overrides.items():
        monkeypatch.setenv(key, value)
    services = create_services(load_app_settings())
    services.photo_moderation_service = AllowAllPhotoModerationService()
    return services


def test_save_photo_generates_thumbnail_and_lists_newest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = build_services(monkeypatch)
    client = make_authenticated_client(monkeypatch, services=services)
    token = csrf_token(client)

    first = client.post(
        "/my/photos",
        data={"label": "First portrait", "csrf_token": token},
        files={"photo": ("first.png", make_png_bytes(color="green"), "image/png")},
    )
    second = client.post(
        "/my/photos",
        data={"label": "Second portrait", "csrf_token": token},
        files={"photo": ("second.png", make_png_bytes(color="purple"), "image/png")},
    )

    assert first.status_code == 201
    assert second.status_code == 201
    first_photo_id = first.json()["photoId"]
    second_photo_id = second.json()["photoId"]
    first_record = services.saved_photo_repository._records[(TEST_OWNER_ID, first_photo_id)]
    second_record = services.saved_photo_repository._records[(TEST_OWNER_ID, second_photo_id)]
    first_record.created_at = "2026-09-03T10:00:00Z"
    first_record.updated_at = "2026-09-03T10:00:00Z"
    second_record.created_at = "2026-09-03T10:05:00Z"
    second_record.updated_at = "2026-09-03T10:05:00Z"
    saved = SavedPhotoResponseModel.model_validate(second.json())
    assert saved.label == "Second portrait"
    assert saved.image.url == f"/my/photos/{saved.photoId}/image"
    assert saved.thumbnail.url == f"/my/photos/{saved.photoId}/thumbnail"

    listing = client.get("/my/photos")

    assert listing.status_code == 200
    payload = listing.json()
    assert payload["schemaVersion"] == 1
    assert [photo["label"] for photo in payload["photos"]] == ["Second portrait", "First portrait"]

    image_response = client.get(saved.image.url)
    thumbnail_response = client.get(saved.thumbnail.url)

    assert image_response.status_code == 200
    assert image_response.headers["content-type"] == "image/png"
    assert thumbnail_response.status_code == 200
    assert thumbnail_response.headers["content-type"] == "image/png"
    with Image.open(BytesIO(thumbnail_response.content)) as thumbnail:
        assert max(thumbnail.size) <= 200


def test_save_photo_rejects_moderation_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    services = build_services(monkeypatch)
    services.photo_moderation_service = RejectingPhotoModerationService()
    client = make_authenticated_client(monkeypatch, services=services)
    token = csrf_token(client)

    response = client.post(
        "/my/photos",
        data={"label": "Unsafe portrait", "csrf_token": token},
        files={"photo": ("unsafe.png", make_png_bytes(color="red"), "image/png")},
    )

    assert response.status_code == 422
    assert response.json()["errorCode"] == "saved_photo_rejected"
    assert asyncio.run(services.saved_photo_repository.count_by_owner(TEST_OWNER_ID)) == 0


def test_save_photo_enforces_per_user_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    services = build_services(monkeypatch, SAVED_PHOTO_MAX_COUNT="1")
    client = make_authenticated_client(monkeypatch, services=services)
    token = csrf_token(client)

    first = client.post(
        "/my/photos",
        data={"label": "First", "csrf_token": token},
        files={"photo": ("first.png", make_png_bytes(color="orange"), "image/png")},
    )
    second = client.post(
        "/my/photos",
        data={"label": "Second", "csrf_token": token},
        files={"photo": ("second.png", make_png_bytes(color="yellow"), "image/png")},
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["errorCode"] == "saved_photo_limit_reached"


def test_saved_photos_are_owner_scoped_for_listing_image_and_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = build_services(monkeypatch)
    owner_client = make_authenticated_client(monkeypatch, services=services)
    owner_token = csrf_token(owner_client)
    saved_response = owner_client.post(
        "/my/photos",
        data={"label": "Owner photo", "csrf_token": owner_token},
        files={"photo": ("owner.png", make_png_bytes(color="blue"), "image/png")},
    )
    saved_photo_id = saved_response.json()["photoId"]

    other_client = make_authenticated_client(
        monkeypatch,
        services=services,
        tenant_id=OTHER_TENANT_ID,
        object_id=OTHER_OBJECT_ID,
        name="Legolas",
    )
    other_token = csrf_token(other_client)

    listing = other_client.get("/my/photos")
    image = other_client.get(f"/my/photos/{saved_photo_id}/image")
    delete = other_client.delete(
        f"/my/photos/{saved_photo_id}",
        headers={"x-csrf-token": other_token},
    )

    assert listing.status_code == 200
    assert listing.json()["photos"] == []
    assert image.status_code == 404
    assert image.json()["errorCode"] == "saved_photo_not_found"
    assert delete.status_code == 404
    assert delete.json()["errorCode"] == "saved_photo_not_found"


def test_delete_photo_removes_metadata_and_blobs(monkeypatch: pytest.MonkeyPatch) -> None:
    services = build_services(monkeypatch)
    client = make_authenticated_client(monkeypatch, services=services)
    token = csrf_token(client)
    created = client.post(
        "/my/photos",
        data={"label": "Delete me", "csrf_token": token},
        files={"photo": ("delete.png", make_png_bytes(color="black"), "image/png")},
    )
    photo_id = created.json()["photoId"]

    response = client.delete(
        f"/my/photos/{photo_id}",
        headers={"x-csrf-token": token},
    )

    assert response.status_code == 204
    assert asyncio.run(services.saved_photo_repository.get(TEST_OWNER_ID, photo_id)) is None
    assert services.photo_asset_store._assets == {}


def test_generation_can_use_saved_photo_id(monkeypatch: pytest.MonkeyPatch) -> None:
    class TrackingAIClient(MockAIClient):
        def __init__(self, settings) -> None:
            super().__init__(settings)
            self.image_edit_calls = 0
            self.reference_image: ReferenceImageUpload | None = None

        async def generate_image_edit(
            self,
            art_prompt: str,
            *,
            reference_image: ReferenceImageUpload,
            request_id: str,
            image_quality=None,
        ):
            self.image_edit_calls += 1
            self.reference_image = replace(reference_image)
            return await super().generate_image(
                art_prompt,
                request_id=request_id,
                image_quality=image_quality,
            )

    services = build_services(monkeypatch)
    services.ai_client = TrackingAIClient(services.settings)
    client = make_authenticated_client(monkeypatch, services=services)
    token = csrf_token(client)
    photo_payload = make_png_bytes(color="teal")

    saved = client.post(
        "/my/photos",
        data={"label": "Reusable", "csrf_token": token},
        files={"photo": ("reusable.png", photo_payload, "image/png")},
    )
    saved_photo_id = saved.json()["photoId"]

    response = client.post(
        "/api/v1/cards/generate",
        json={
            "prompt": "create a safe fantasy knight with a moonlit shield",
            "idempotencyKey": "idem-saved-photo",
            "csrfToken": token,
            "savedPhotoId": saved_photo_id,
        },
    )

    assert response.status_code == 200
    assert services.ai_client.image_edit_calls == 1
    assert services.ai_client.reference_image is not None
    assert services.ai_client.reference_image.content == photo_payload
    assert services.ai_client.reference_image.content_type == "image/png"


def test_generate_rejects_photo_and_saved_photo_id_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = build_services(monkeypatch)
    client = make_authenticated_client(monkeypatch, services=services)
    token = csrf_token(client)

    response = client.post(
        "/api/v1/cards/generate",
        data={
            "prompt": "create a safe fantasy knight with a moonlit shield",
            "idempotency_key": "idem-photo-conflict",
            "csrf_token": token,
            "saved_photo_id": uuid4().hex,
        },
        files={"photo": ("portrait.png", make_png_bytes(color="white"), "image/png")},
    )

    assert response.status_code == 422
    assert response.json()["errorCode"] == "photo_reference_conflict"
