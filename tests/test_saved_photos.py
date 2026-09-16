from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from starlette.responses import RedirectResponse

from app import main as main_module
from app import photos as photos_module
from app.generation import (
    AppServices,
    AuthenticatedOwner,
    MockAIClient,
    ReferenceImageUpload,
    create_services,
)
from app.main import create_app
from app.photos import (
    PROFILE_PHOTO_IMPORT_SOURCE,
    ProfilePhotoImportState,
    SavedPhotoResponseModel,
    normalize_imported_profile_photo,
)
from app.settings import load_app_settings
from tests.conftest import begin_login

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
    login_response = begin_login(client)
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


@pytest.mark.parametrize(
    ("failure_stage", "expected_stage"),
    [
        ("blob-upload", "blob upload"),
        ("thumbnail-upload", "thumbnail upload"),
        ("cosmos-save", "cosmos save"),
    ],
)
def test_save_photo_logs_persistence_failures(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_stage: str,
    expected_stage: str,
) -> None:
    services = build_services(monkeypatch)
    monkeypatch.setattr(photos_module, "uuid4", lambda: SimpleNamespace(hex="photo-log-123"))

    original_upload = services.photo_asset_store.upload
    original_save = services.saved_photo_repository.save
    upload_calls = 0

    async def failing_upload(blob_name: str, payload: bytes, content_type: str):
        nonlocal upload_calls
        upload_calls += 1
        if failure_stage == "blob-upload":
            raise RuntimeError("blob upload exploded")
        if failure_stage == "thumbnail-upload" and upload_calls == 2:
            raise RuntimeError("thumbnail upload exploded")
        return await original_upload(blob_name, payload, content_type)

    async def failing_save(record):
        if failure_stage == "cosmos-save":
            raise RuntimeError("cosmos save exploded")
        return await original_save(record)

    monkeypatch.setattr(services.photo_asset_store, "upload", failing_upload)
    monkeypatch.setattr(services.saved_photo_repository, "save", failing_save)

    client = make_authenticated_client(monkeypatch, services=services)
    token = csrf_token(client)

    with caplog.at_level(logging.ERROR, logger="app.photos"):
        response = client.post(
            "/my/photos",
            data={"label": "Fails", "csrf_token": token},
            files={"photo": ("fails.png", make_png_bytes(color="gray"), "image/png")},
        )

    assert response.status_code == 503
    assert response.json()["errorCode"] == "saved_photo_persistence_failure"
    assert any(
        record.levelno == logging.ERROR
        and f"during {expected_stage}" in record.getMessage()
        and f"owner_id={TEST_OWNER_ID}" in record.getMessage()
        and "photo_id=photo-log-123" in record.getMessage()
        for record in caplog.records
    )


def test_delete_photo_logs_delete_failures(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    services = build_services(monkeypatch)
    monkeypatch.setattr(photos_module, "uuid4", lambda: SimpleNamespace(hex="photo-delete-123"))
    client = make_authenticated_client(monkeypatch, services=services)
    token = csrf_token(client)

    created = client.post(
        "/my/photos",
        data={"label": "Delete fails", "csrf_token": token},
        files={"photo": ("delete-fails.png", make_png_bytes(color="maroon"), "image/png")},
    )
    assert created.status_code == 201

    async def failing_delete(blob_name: str) -> None:
        raise RuntimeError("delete exploded")

    monkeypatch.setattr(services.photo_asset_store, "delete", failing_delete)

    with caplog.at_level(logging.ERROR, logger="app.photos"):
        response = client.delete(
            "/my/photos/photo-delete-123",
            headers={"x-csrf-token": token},
        )

    assert response.status_code == 503
    assert response.json()["errorCode"] == "saved_photo_delete_failure"
    assert any(
        record.levelno == logging.ERROR
        and "during blob delete" in record.getMessage()
        and f"owner_id={TEST_OWNER_ID}" in record.getMessage()
        and "photo_id=photo-delete-123" in record.getMessage()
        for record in caplog.records
    )


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


def test_imported_photo_deletion_persists_do_not_reimport_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = build_services(monkeypatch)
    client = make_authenticated_client(monkeypatch, services=services)
    owner = AuthenticatedOwner(
        owner_id=TEST_OWNER_ID,
        tenant_id=TEST_TENANT_ID,
        object_id=TEST_OBJECT_ID,
        subject="user-test",
        display_name="Aragorn",
        email="aragorn@example.com",
    )
    saved = asyncio.run(
        main_module.SavedPhotoService(
            settings=services.settings,
            repository=services.saved_photo_repository,
            asset_store=services.photo_asset_store,
            moderation_service=services.photo_moderation_service,
        ).save_photo(
            owner=owner,
            photo=ReferenceImageUpload(
                content=make_png_bytes(),
                content_type="image/png",
                filename="entra-profile-photo",
            ),
            label="Microsoft profile photo",
            source=PROFILE_PHOTO_IMPORT_SOURCE,
            source_key="me/photo",
        )
    )
    token = csrf_token(client)
    response = client.delete(
        f"/my/photos/{saved.photoId}",
        headers={"x-csrf-token": token},
    )

    assert response.status_code == 204
    state = asyncio.run(services.profile_photo_import_state_repository.get(TEST_OWNER_ID))
    assert state is not None
    assert state.status == "deleted_suppressed"


def test_import_claim_can_be_reset_and_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    services = build_services(monkeypatch)
    repository = services.profile_photo_import_state_repository

    assert asyncio.run(repository.claim(TEST_OWNER_ID))
    assert not asyncio.run(repository.claim(TEST_OWNER_ID))
    asyncio.run(repository.reset_claim(TEST_OWNER_ID))
    assert asyncio.run(repository.claim(TEST_OWNER_ID))


def test_import_claim_allows_only_one_concurrent_accept(monkeypatch: pytest.MonkeyPatch) -> None:
    services = build_services(monkeypatch)
    repository = services.profile_photo_import_state_repository
    asyncio.run(repository.save(ProfilePhotoImportState(owner_id=TEST_OWNER_ID, status="offered")))

    async def claim() -> bool:
        return await repository.claim(TEST_OWNER_ID)

    async def run_claims() -> list[bool]:
        return await asyncio.gather(*(claim() for _ in range(8)))

    results = asyncio.run(run_claims())

    assert results.count(True) == 1
    state = asyncio.run(repository.get(TEST_OWNER_ID))
    assert state is not None
    assert state.status == "accepted"


@pytest.mark.parametrize(
    ("existing_status", "expected"),
    [
        (None, True),
        ("not_offered", True),
        ("offered", True),
        ("declined", True),
        ("failed_retryable", True),
        ("no_photo", True),
        ("imported", False),
        ("deleted_suppressed", False),
        ("accepted", False),
    ],
)
@pytest.mark.parametrize("backend", ["memory", "cosmos"])
def test_sign_in_import_claims_new_and_retryable_accounts_without_restoring_deleted_photos(
    monkeypatch: pytest.MonkeyPatch,
    existing_status: str | None,
    expected: bool,
    backend: str,
) -> None:
    state = (
        ProfilePhotoImportState(owner_id=TEST_OWNER_ID, status=existing_status)
        if existing_status
        else None
    )

    class FakeContainer:
        async def create_item(self, document):
            nonlocal state
            assert state is None
            assert document["userId"] == TEST_OWNER_ID
            state = ProfilePhotoImportState.from_document(document)

        async def patch_item(self, item, *, partition_key, patch_operations, filter_predicate):
            assert partition_key == TEST_OWNER_ID
            assert f"'{existing_status}'" in filter_predicate
            assert "c.updatedAt <=" in filter_predicate
            assert state is not None
            state.status = patch_operations[0]["value"]

    if backend == "cosmos":
        repository = photos_module.AzureCosmosProfilePhotoImportStateRepository.__new__(
            photos_module.AzureCosmosProfilePhotoImportStateRepository
        )
        repository._container = FakeContainer()

        async def get(_):
            return state

        monkeypatch.setattr(repository, "get", get)
    else:
        repository = photos_module.InMemoryProfilePhotoImportStateRepository()
        if state is not None:
            asyncio.run(repository.save(state))

    assert asyncio.run(repository.claim(TEST_OWNER_ID)) is expected
    saved = asyncio.run(repository.get(TEST_OWNER_ID))
    assert saved.status == ("accepted" if expected else existing_status)
    assert "access_token" not in saved.to_document()
    assert not asyncio.run(repository.claim(TEST_OWNER_ID))


@pytest.mark.parametrize("status_code", [409, 403, 429])
def test_new_cosmos_import_claim_handles_races_without_hiding_service_errors(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    from azure.cosmos.exceptions import CosmosHttpResponseError

    class FakeContainer:
        async def create_item(self, document):
            raise CosmosHttpResponseError(status_code=status_code, message="Claim failed")

    repository = photos_module.AzureCosmosProfilePhotoImportStateRepository.__new__(
        photos_module.AzureCosmosProfilePhotoImportStateRepository
    )
    repository._container = FakeContainer()

    async def missing(_):
        return None

    monkeypatch.setattr(repository, "get", missing)
    if status_code == 409:
        assert not asyncio.run(repository.claim(TEST_OWNER_ID))
    else:
        with pytest.raises(CosmosHttpResponseError) as caught:
            asyncio.run(repository.claim(TEST_OWNER_ID))
        assert caught.value.status_code == status_code


def test_imported_original_is_reencoded_with_bounded_dimensions_and_no_metadata() -> None:
    image = Image.new("RGB", (3000, 1000), color="purple")
    payload = BytesIO()
    image.save(payload, format="PNG", pnginfo=None)

    normalized = normalize_imported_profile_photo(
        ReferenceImageUpload(
            content=payload.getvalue(),
            content_type="image/png",
            filename="entra-profile-photo",
        )
    )

    assert normalized.content_type == "image/jpeg"
    with Image.open(BytesIO(normalized.content)) as decoded:
        assert max(decoded.size) <= 2048
        assert decoded.getexif() == {}


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
