from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, Literal
from uuid import uuid4

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from app.generation import (
    AbstractAssetStore,
    AuthenticatedOwner,
    ReferenceImageUpload,
    UpstreamServiceError,
    _default_azure_credential,
    build_owner_hash,
    now_iso,
)
from app.problems import ProblemDetails
from app.settings import AppSettings

SAVED_PHOTO_DOCUMENT_ID_PREFIX = "photo:"
CONTENT_SAFETY_CATEGORIES = ("Hate", "SelfHarm", "Sexual", "Violence")
THUMBNAIL_BLOB_CONTENT_TYPE = "image/png"
logger = logging.getLogger(__name__)


def _log_saved_photo_exception(
    *,
    action: str,
    stage: str,
    exc: Exception,
    owner_id: str | None = None,
    photo_id: str | None = None,
) -> None:
    exception_type = type(exc).__name__
    logger.exception(
        "Saved photo %s failed during %s "
        "(owner_id=%s, photo_id=%s, exc_type=%s, exc_message=%s)",
        action,
        stage,
        owner_id or "unknown",
        photo_id or "unknown",
        exception_type,
        str(exc),
        extra={
            "saved_photo_action": action,
            "saved_photo_stage": stage,
            "owner_id": owner_id,
            "photo_id": photo_id,
            "exception_type": exception_type,
        },
    )


def _save_photo_failure_stage(
    *,
    uploaded_original: bool,
    uploaded_thumbnail: bool,
) -> str:
    if not uploaded_original:
        return "blob upload"
    if not uploaded_thumbnail:
        return "thumbnail upload"
    return "cosmos save"


class SavedPhotoImageModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contentType: str
    sizeBytes: int = Field(ge=1)
    url: str


class SavedPhotoThumbnailModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contentType: str = THUMBNAIL_BLOB_CONTENT_TYPE
    url: str


class SavedPhotoResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schemaVersion: Literal[1] = 1
    photoId: str
    label: str | None = None
    createdAt: str
    updatedAt: str
    image: SavedPhotoImageModel
    thumbnail: SavedPhotoThumbnailModel


class SavedPhotoListResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schemaVersion: Literal[1] = 1
    photos: list[SavedPhotoResponseModel]


@dataclass(slots=True)
class StoredSavedPhoto:
    photo_id: str
    owner_id: str
    label: str | None
    blob_name: str
    blob_content_type: str
    blob_sha256: str
    blob_size_bytes: int
    image_url_path: str
    thumbnail_blob_name: str
    thumbnail_image_url_path: str
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    @property
    def document_id(self) -> str:
        return f"{SAVED_PHOTO_DOCUMENT_ID_PREFIX}{self.photo_id}"

    def to_document(self) -> dict[str, Any]:
        return {
            "id": self.document_id,
            "photoId": self.photo_id,
            "documentType": "saved-photo",
            "userId": self.owner_id,
            "schemaVersion": 1,
            "owner": {"ownerId": self.owner_id},
            "label": self.label,
            "blob": {
                "name": self.blob_name,
                "contentType": self.blob_content_type,
                "sha256": self.blob_sha256,
                "sizeBytes": self.blob_size_bytes,
                "imageUrlPath": self.image_url_path,
            },
            "thumbnail": {
                "blobName": self.thumbnail_blob_name,
                "contentType": THUMBNAIL_BLOB_CONTENT_TYPE,
                "imageUrlPath": self.thumbnail_image_url_path,
            },
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "StoredSavedPhoto | None":
        if str(document.get("documentType")) != "saved-photo":
            return None
        blob = document.get("blob") or {}
        thumbnail = document.get("thumbnail") or {}
        photo_id = document.get("photoId")
        if not isinstance(photo_id, str) or not photo_id:
            return None
        blob_name = blob.get("name")
        thumbnail_blob_name = thumbnail.get("blobName")
        if not isinstance(blob_name, str) or not isinstance(thumbnail_blob_name, str):
            return None
        return cls(
            photo_id=photo_id,
            owner_id=str(document["userId"]),
            label=_optional_label(document.get("label")),
            blob_name=blob_name,
            blob_content_type=str(blob.get("contentType") or "image/png"),
            blob_sha256=str(blob.get("sha256") or ""),
            blob_size_bytes=int(blob.get("sizeBytes") or 0),
            image_url_path=str(blob.get("imageUrlPath") or f"/my/photos/{photo_id}/image"),
            thumbnail_blob_name=thumbnail_blob_name,
            thumbnail_image_url_path=str(
                thumbnail.get("imageUrlPath") or f"/my/photos/{photo_id}/thumbnail"
            ),
            created_at=str(document.get("createdAt") or now_iso()),
            updated_at=str(document.get("updatedAt") or now_iso()),
        )

    def as_response(self) -> SavedPhotoResponseModel:
        return SavedPhotoResponseModel(
            photoId=self.photo_id,
            label=self.label,
            createdAt=self.created_at,
            updatedAt=self.updated_at,
            image=SavedPhotoImageModel(
                contentType=self.blob_content_type,
                sizeBytes=self.blob_size_bytes,
                url=self.image_url_path,
            ),
            thumbnail=SavedPhotoThumbnailModel(
                contentType=THUMBNAIL_BLOB_CONTENT_TYPE,
                url=self.thumbnail_image_url_path,
            ),
        )


class AbstractSavedPhotoRepository:
    async def save(self, record: StoredSavedPhoto) -> StoredSavedPhoto:
        raise NotImplementedError

    async def get(self, owner_id: str, photo_id: str) -> StoredSavedPhoto | None:
        raise NotImplementedError

    async def list_by_owner(self, owner_id: str) -> list[StoredSavedPhoto]:
        raise NotImplementedError

    async def delete(self, owner_id: str, photo_id: str) -> None:
        raise NotImplementedError

    async def count_by_owner(self, owner_id: str) -> int:
        raise NotImplementedError


class InMemorySavedPhotoRepository(AbstractSavedPhotoRepository):
    def __init__(self) -> None:
        self._records: dict[tuple[str, str], StoredSavedPhoto] = {}
        self._lock = asyncio.Lock()

    async def save(self, record: StoredSavedPhoto) -> StoredSavedPhoto:
        async with self._lock:
            record.updated_at = now_iso()
            self._records[(record.owner_id, record.photo_id)] = record
            return record

    async def get(self, owner_id: str, photo_id: str) -> StoredSavedPhoto | None:
        async with self._lock:
            return self._records.get((owner_id, photo_id))

    async def list_by_owner(self, owner_id: str) -> list[StoredSavedPhoto]:
        async with self._lock:
            records = [
                record
                for (stored_owner_id, _), record in self._records.items()
                if stored_owner_id == owner_id
            ]
        return sorted(
            records,
            key=lambda record: (record.created_at, record.photo_id),
            reverse=True,
        )

    async def delete(self, owner_id: str, photo_id: str) -> None:
        async with self._lock:
            self._records.pop((owner_id, photo_id), None)

    async def count_by_owner(self, owner_id: str) -> int:
        async with self._lock:
            return sum(1 for stored_owner_id, _ in self._records if stored_owner_id == owner_id)


class AzureCosmosSavedPhotoRepository(AbstractSavedPhotoRepository):
    def __init__(self, settings: AppSettings) -> None:
        from azure.cosmos.aio import CosmosClient

        self._client = CosmosClient(
            settings.cosmos_endpoint,
            credential=_default_azure_credential(),
        )
        self._database_name = settings.cosmos_database_name or "appdb"
        self._container_name = settings.cosmos_container_name or "cards"
        self._container = None

    async def _get_container(self):
        if self._container is None:
            database = self._client.get_database_client(self._database_name)
            self._container = database.get_container_client(self._container_name)
        return self._container

    async def _read_raw_document(self, owner_id: str, photo_id: str) -> dict[str, Any] | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        container = await self._get_container()
        try:
            return await container.read_item(
                f"{SAVED_PHOTO_DOCUMENT_ID_PREFIX}{photo_id}",
                partition_key=owner_id,
            )
        except CosmosResourceNotFoundError:
            return None

    async def save(self, record: StoredSavedPhoto) -> StoredSavedPhoto:
        container = await self._get_container()
        record.updated_at = now_iso()
        await container.upsert_item(record.to_document())
        return record

    async def get(self, owner_id: str, photo_id: str) -> StoredSavedPhoto | None:
        document = await self._read_raw_document(owner_id, photo_id)
        return StoredSavedPhoto.from_document(document or {})

    async def list_by_owner(self, owner_id: str) -> list[StoredSavedPhoto]:
        container = await self._get_container()
        iterator = container.query_items(
            query=(
                "SELECT * FROM c WHERE c.userId = @ownerId AND c.documentType = @documentType "
                "ORDER BY c.createdAt DESC"
            ),
            parameters=[
                {"name": "@ownerId", "value": owner_id},
                {"name": "@documentType", "value": "saved-photo"},
            ],
            partition_key=owner_id,
        )
        records: list[StoredSavedPhoto] = []
        async for document in iterator:
            record = StoredSavedPhoto.from_document(document)
            if record is not None:
                records.append(record)
        return records

    async def delete(self, owner_id: str, photo_id: str) -> None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        container = await self._get_container()
        try:
            await container.delete_item(
                f"{SAVED_PHOTO_DOCUMENT_ID_PREFIX}{photo_id}",
                partition_key=owner_id,
            )
        except CosmosResourceNotFoundError:
            return

    async def count_by_owner(self, owner_id: str) -> int:
        container = await self._get_container()
        iterator = container.query_items(
            query=(
                "SELECT VALUE COUNT(1) FROM c WHERE c.userId = @ownerId "
                "AND c.documentType = @documentType"
            ),
            parameters=[
                {"name": "@ownerId", "value": owner_id},
                {"name": "@documentType", "value": "saved-photo"},
            ],
            partition_key=owner_id,
        )
        async for value in iterator:
            return int(value)
        return 0


@dataclass(frozen=True, slots=True)
class ContentSafetyCategoryResult:
    category: str
    severity: int


@dataclass(frozen=True, slots=True)
class SavedPhotoVariant:
    record: StoredSavedPhoto
    blob_name: str


class ContentSafetyPhotoModerationService:
    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._credential = _default_azure_credential()

    async def assert_allowed(
        self,
        photo: ReferenceImageUpload,
    ) -> list[ContentSafetyCategoryResult]:
        if not self._settings.content_safety_endpoint:
            raise ProblemDetails(
                status_code=503,
                title="Photo Library Unavailable",
                detail=(
                    "Saved photos are unavailable because Azure AI Content Safety "
                    "is not configured."
                ),
                type="/problems/photo-moderation-unconfigured",
                error_code="photo_moderation_unconfigured",
            )
        try:
            response = await self._post(photo)
        except UpstreamServiceError as exc:
            raise ProblemDetails(
                status_code=503,
                title="Photo Safety Check Unavailable",
                detail="The uploaded photo could not be safety-checked. Please try again later.",
                type="/problems/photo-moderation-unavailable",
                error_code="photo_moderation_unavailable",
            ) from exc

        analysis = response.get("categoriesAnalysis") or []
        results = [
            ContentSafetyCategoryResult(
                category=str(item.get("category") or ""),
                severity=int(item.get("severity") or 0),
            )
            for item in analysis
            if isinstance(item, dict)
        ]
        rejected = [
            result
            for result in results
            if result.severity > self._threshold_for_category(result.category)
        ]
        if rejected:
            rejected_summary = ", ".join(
                f"{result.category}={result.severity}" for result in rejected
            )
            raise ProblemDetails(
                status_code=422,
                title="Saved Photo Rejected",
                detail=(
                    "The uploaded photo could not be saved because it exceeded the allowed "
                    f"safety threshold ({rejected_summary})."
                ),
                type="/problems/saved-photo-rejected",
                error_code="saved_photo_rejected",
                extra={
                    "categoriesAnalysis": [
                        {"category": result.category, "severity": result.severity}
                        for result in results
                    ]
                },
            )
        return results

    async def _post(self, photo: ReferenceImageUpload) -> dict[str, Any]:
        token = await asyncio.to_thread(
            self._credential.get_token,
            "https://cognitiveservices.azure.com/.default",
        )
        headers = {
            "Authorization": "Bearer " + token.token,
            "Content-Type": "application/json",
        }
        payload = {
            "image": {"content": base64.b64encode(photo.content).decode("ascii")},
            "categories": list(CONTENT_SAFETY_CATEGORIES),
            "outputType": "FourSeverityLevels",
        }
        async with httpx.AsyncClient(base_url=self._settings.content_safety_endpoint) as client:
            response = await client.post(
                "/contentsafety/image:analyze",
                params={"api-version": self._settings.content_safety_api_version},
                json=payload,
                headers=headers,
                timeout=30.0,
            )
        if response.status_code >= 400:
            error_payload = {}
            try:
                error_payload = response.json()
            except ValueError:
                error_payload = {}
            error = error_payload.get("error") if isinstance(error_payload, dict) else {}
            raise UpstreamServiceError(
                "content-safety",
                str((error or {}).get("message") or "Azure AI Content Safety request failed."),
                status_code=response.status_code,
                retryable=response.status_code in {408, 429, 500, 502, 503, 504},
                error_code=str((error or {}).get("code") or "content_safety_failed"),
            )
        return response.json()

    def _threshold_for_category(self, category: str) -> int:
        return {
            "Hate": self._settings.content_safety_max_hate_severity,
            "SelfHarm": self._settings.content_safety_max_self_harm_severity,
            "Sexual": self._settings.content_safety_max_sexual_severity,
            "Violence": self._settings.content_safety_max_violence_severity,
        }.get(category, 0)


class SavedPhotoService:
    def __init__(
        self,
        *,
        settings: AppSettings,
        repository,
        asset_store,
        moderation_service,
    ) -> None:
        self._settings = settings
        self._repository: AbstractSavedPhotoRepository = repository
        self._asset_store: AbstractAssetStore = asset_store
        self._moderation_service = moderation_service

    async def save_photo(
        self,
        *,
        owner: AuthenticatedOwner,
        photo: ReferenceImageUpload,
        label: str | None,
    ) -> SavedPhotoResponseModel:
        normalized_label = normalize_photo_label(label)
        if len(photo.content) > self._settings.saved_photo_max_bytes:
            raise ProblemDetails(
                status_code=413,
                title="Saved Photo Too Large",
                detail=(
                    "Saved photos must be 4 MB or smaller so they can be safety-checked "
                    "before storage."
                ),
                type="/problems/saved-photo-too-large",
                error_code="saved_photo_too_large",
            )
        photo_count = await self._repository.count_by_owner(owner.owner_id)
        if photo_count >= self._settings.saved_photo_max_count:
            raise ProblemDetails(
                status_code=409,
                title="Saved Photo Limit Reached",
                detail=(
                    f"You have reached the saved-photo limit of "
                    f"{self._settings.saved_photo_max_count}. Delete one before saving another."
                ),
                type="/problems/saved-photo-limit",
                error_code="saved_photo_limit_reached",
            )
        await self._moderation_service.assert_allowed(photo)
        try:
            thumbnail_bytes = build_thumbnail(
                photo.content,
                size=self._settings.saved_photo_thumbnail_size,
            )
        except InvalidSavedPhotoError as exc:
            raise ProblemDetails(
                status_code=422,
                title="Invalid Photo Upload",
                detail=str(exc),
                type="/problems/invalid-photo-upload",
                error_code="invalid_photo_upload",
            ) from exc

        photo_id = uuid4().hex
        blob_name = build_saved_photo_blob_name(owner.owner_id, photo_id, photo.content_type)
        thumbnail_blob_name = build_saved_photo_thumbnail_blob_name(owner.owner_id, photo_id)

        uploaded_original = False
        uploaded_thumbnail = False
        try:
            blob_metadata = await self._asset_store.upload(
                blob_name,
                photo.content,
                photo.content_type,
            )
            uploaded_original = True
            await self._asset_store.upload(
                thumbnail_blob_name,
                thumbnail_bytes,
                THUMBNAIL_BLOB_CONTENT_TYPE,
            )
            uploaded_thumbnail = True
            stored = await self._repository.save(
                StoredSavedPhoto(
                    photo_id=photo_id,
                    owner_id=owner.owner_id,
                    label=normalized_label,
                    blob_name=blob_metadata["blobName"],
                    blob_content_type=blob_metadata["contentType"],
                    blob_sha256=blob_metadata["sha256"],
                    blob_size_bytes=int(blob_metadata["sizeBytes"]),
                    image_url_path=f"/my/photos/{photo_id}/image",
                    thumbnail_blob_name=thumbnail_blob_name,
                    thumbnail_image_url_path=f"/my/photos/{photo_id}/thumbnail",
                )
            )
        except Exception as exc:
            _log_saved_photo_exception(
                action="save",
                stage=_save_photo_failure_stage(
                    uploaded_original=uploaded_original,
                    uploaded_thumbnail=uploaded_thumbnail,
                ),
                owner_id=owner.owner_id,
                photo_id=photo_id,
                exc=exc,
            )
            if uploaded_thumbnail:
                await self._safe_delete_blob(
                    thumbnail_blob_name,
                    owner_id=owner.owner_id,
                    photo_id=photo_id,
                    stage="thumbnail cleanup",
                )
            if uploaded_original:
                await self._safe_delete_blob(
                    blob_name,
                    owner_id=owner.owner_id,
                    photo_id=photo_id,
                    stage="blob cleanup",
                )
            raise ProblemDetails(
                status_code=503,
                title="Photo Library Unavailable",
                detail="The uploaded photo could not be saved safely.",
                type="/problems/saved-photo-persistence-failure",
                error_code="saved_photo_persistence_failure",
            ) from exc
        return stored.as_response()

    async def list_photos(self, owner: AuthenticatedOwner) -> SavedPhotoListResponseModel:
        records = await self._repository.list_by_owner(owner.owner_id)
        return SavedPhotoListResponseModel(photos=[record.as_response() for record in records])

    async def fetch_original(self, owner: AuthenticatedOwner, photo_id: str) -> tuple[bytes, str]:
        record = await self._require_photo(owner.owner_id, photo_id)
        return await self._fetch_blob(record.blob_name)

    async def fetch_thumbnail(self, owner: AuthenticatedOwner, photo_id: str) -> tuple[bytes, str]:
        record = await self._require_photo(owner.owner_id, photo_id)
        return await self._fetch_blob(record.thumbnail_blob_name)

    async def load_reference_image(
        self,
        owner: AuthenticatedOwner,
        photo_id: str,
    ) -> ReferenceImageUpload:
        record = await self._require_photo(owner.owner_id, photo_id)
        payload, content_type = await self._fetch_blob(record.blob_name)
        return ReferenceImageUpload(
            content=payload,
            content_type=content_type,
            filename=record.blob_name.rsplit("/", 1)[-1],
        )

    async def delete_photo(self, owner: AuthenticatedOwner, photo_id: str) -> None:
        record = await self._require_photo(owner.owner_id, photo_id)
        await self._delete_blob(
            record.blob_name,
            owner_id=owner.owner_id,
            photo_id=photo_id,
            stage="blob delete",
        )
        await self._delete_blob(
            record.thumbnail_blob_name,
            owner_id=owner.owner_id,
            photo_id=photo_id,
            stage="thumbnail delete",
        )
        await self._repository.delete(owner.owner_id, photo_id)

    async def _require_photo(self, owner_id: str, photo_id: str) -> StoredSavedPhoto:
        record = await self._repository.get(owner_id, photo_id)
        if record is None:
            raise ProblemDetails(
                status_code=404,
                title="Not Found",
                detail="No saved photo was found for this user.",
                type="/problems/saved-photo-not-found",
                error_code="saved_photo_not_found",
            )
        return record

    async def _fetch_blob(self, blob_name: str) -> tuple[bytes, str]:
        try:
            return await self._asset_store.download(blob_name)
        except FileNotFoundError as exc:
            raise ProblemDetails(
                status_code=404,
                title="Not Found",
                detail="The saved photo is unavailable.",
                type="/problems/saved-photo-image-not-found",
                error_code="saved_photo_image_not_found",
            ) from exc

    async def _safe_delete_blob(
        self,
        blob_name: str,
        *,
        owner_id: str | None = None,
        photo_id: str | None = None,
        stage: str = "blob cleanup",
    ) -> None:
        try:
            await self._delete_blob(
                blob_name,
                owner_id=owner_id,
                photo_id=photo_id,
                stage=stage,
            )
        except ProblemDetails:
            return

    async def _delete_blob(
        self,
        blob_name: str,
        *,
        owner_id: str | None = None,
        photo_id: str | None = None,
        stage: str = "blob delete",
    ) -> None:
        try:
            await self._asset_store.delete(blob_name)
        except Exception as exc:
            if _is_not_found_exception(exc):
                return
            _log_saved_photo_exception(
                action="delete",
                stage=stage,
                owner_id=owner_id,
                photo_id=photo_id,
                exc=exc,
            )
            raise ProblemDetails(
                status_code=503,
                title="Photo Library Unavailable",
                detail="The saved photo could not be deleted safely.",
                type="/problems/saved-photo-delete-failure",
                error_code="saved_photo_delete_failure",
            ) from exc


class InvalidSavedPhotoError(ValueError):
    pass


def normalize_photo_label(label: str | None) -> str | None:
    normalized = _optional_label(label)
    if normalized is None:
        return None
    if len(normalized) > 80:
        raise ProblemDetails(
            status_code=422,
            title="Invalid Photo Label",
            detail="Photo labels must be 80 characters or fewer.",
            type="/problems/invalid-photo-label",
            error_code="invalid_photo_label",
        )
    return normalized


def build_saved_photo_blob_name(owner_id: str, photo_id: str, content_type: str) -> str:
    extension = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }.get(content_type)
    if extension is None:
        raise ProblemDetails(
            status_code=415,
            title="Unsupported Photo Type",
            detail="Photo uploads must be JPEG, PNG, or WebP images.",
            type="/problems/unsupported-photo-type",
            error_code="unsupported_photo_type",
        )
    return f"photos/{build_owner_hash(owner_id)}/{photo_id}/original{extension}"


def build_saved_photo_thumbnail_blob_name(owner_id: str, photo_id: str) -> str:
    return f"photos/{build_owner_hash(owner_id)}/{photo_id}/thumb.png"


def build_thumbnail(payload: bytes, *, size: int) -> bytes:
    try:
        with Image.open(BytesIO(payload)) as image:
            working = ImageOps.exif_transpose(image)
            resampling = getattr(Image, "Resampling", Image)
            thumbnail = ImageOps.contain(working.convert("RGBA"), (size, size), resampling.LANCZOS)
            output = BytesIO()
            thumbnail.save(output, format="PNG")
            return output.getvalue()
    except UnidentifiedImageError as exc:
        raise InvalidSavedPhotoError(
            "The uploaded photo could not be decoded as an image."
        ) from exc
    except OSError as exc:
        raise InvalidSavedPhotoError("The uploaded photo could not be processed safely.") from exc


def _optional_label(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized or None


def _is_not_found_exception(exc: Exception) -> bool:
    if isinstance(exc, FileNotFoundError):
        return True
    return type(exc).__name__ == "ResourceNotFoundError"
