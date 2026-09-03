from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.health import (
    AzureBlobHealthProbe,
    AzureCosmosHealthProbe,
    HealthDependencyProbe,
    NotApplicableHealthProbe,
)
from app.problems import ProblemDetails
from app.settings import AppSettings, RateLimitSettings
from app.telemetry import (
    add_event,
    instrument_generation,
    normalize_error_code,
    record_dependency_attempt,
    record_moderation,
    record_partial,
    record_persistence,
    record_retry,
    record_token_usage,
    safe_persistence_log,
    telemetry_span,
)

PNG_1X1_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO6r2w0AAAAASUVORK5CYII="
)
CARD_DOCUMENT_ID_PREFIX = "card:"
AUDIT_DOCUMENT_ID_PREFIX = "audit:"


def _exception_diagnostic(exc: Exception) -> tuple[str, int | None, str | None]:
    status_code = getattr(exc, "status_code", None)
    error_code = getattr(exc, "error_code", None)
    response = getattr(exc, "response", None)
    if error_code is None and response is not None:
        headers = getattr(response, "headers", None)
        if headers is not None:
            error_code = headers.get("x-ms-error-code")
    return (
        type(exc).__name__,
        status_code if isinstance(status_code, int) else None,
        str(error_code) if error_code else None,
    )


def _log_persistence_exception(
    *,
    event: str,
    request_id: str,
    stage: str,
    exc: Exception,
) -> None:
    _, status_code, error_code = _exception_diagnostic(exc)
    safe_persistence_log(
        event=event,
        request_id=request_id,
        stage=stage,
        status_code=status_code,
        azure_error_code=error_code,
    )
    operation = "save_failure"
    if stage in {"compensation-delete", "cosmos-delete"}:
        operation = "compensate"
    store = "audit" if stage == "audit-write" else "card"
    if stage in {"blob-upload", "compensation-delete"}:
        store = "blob"
    elif stage in {"cosmos-write", "cosmos-delete"}:
        store = "cosmos"
    record_persistence(
        store=store,
        operation=operation,
        outcome="failed",
        request_id=request_id,
        error_code=error_code or "persistence_failure",
    )


def _card_document_id(card_id: str) -> str:
    return f"{CARD_DOCUMENT_ID_PREFIX}{card_id}"


def _audit_document_id(card_id: str) -> str:
    return f"{AUDIT_DOCUMENT_ID_PREFIX}{card_id}"


def _logical_card_id(document_id: str, stored_card_id: Any) -> str:
    if isinstance(stored_card_id, str) and stored_card_id:
        return stored_card_id
    if document_id.startswith(CARD_DOCUMENT_ID_PREFIX):
        return document_id.removeprefix(CARD_DOCUMENT_ID_PREFIX)
    if document_id.startswith(AUDIT_DOCUMENT_ID_PREFIX):
        return document_id.removeprefix(AUDIT_DOCUMENT_ID_PREFIX)
    return document_id


class UpstreamServiceError(RuntimeError):
    def __init__(
        self,
        service: str,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        error_code: str | None = None,
        diagnostic_message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.service = service
        self.status_code = status_code
        self.retryable = retryable
        self.error_code = error_code
        self.diagnostic_message = diagnostic_message


class GeneratedCardModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schemaVersion: Literal[1]
    name: str = Field(min_length=3, max_length=80)
    cardType: Literal["hero", "creature", "artifact", "spell"]
    rarity: Literal["common", "uncommon", "rare", "legendary"]
    manaCost: int = Field(ge=0, le=12)
    attack: int = Field(ge=0, le=20)
    health: int = Field(ge=1, le=20)
    rulesText: str = Field(min_length=12, max_length=400)
    flavorText: str = Field(min_length=0, max_length=280)
    artBrief: str = Field(min_length=12, max_length=300)


class CardGenerateBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    prompt: str = Field(min_length=12, max_length=400)
    idempotencyKey: str | None = Field(default=None, min_length=8, max_length=128)
    csrfToken: str | None = Field(default=None, min_length=8, max_length=256)


class ArtworkRetryBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotencyKey: str | None = Field(default=None, min_length=8, max_length=128)
    csrfToken: str | None = Field(default=None, min_length=8, max_length=256)


class ActionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["retry_artwork"]
    method: Literal["POST"] = "POST"
    href: str


class CardResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schemaVersion: Literal[1] = 1
    cardId: str
    status: Literal["completed", "awaiting_artwork_retry"]
    requestId: str
    idempotencyKey: str
    ownerId: str
    name: str
    cardType: str
    rarity: str
    manaCost: int
    attack: int
    health: int
    rulesText: str
    flavorText: str
    imageUrl: str | None = None
    actions: list[ActionModel] = Field(default_factory=list)


class ModerationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: Literal["pre_prompt", "post_text", "post_art_prompt", "post_image"]
    allowed: bool
    reasonCode: str
    details: str


class UsageAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inputTokens: int = Field(ge=0)
    outputTokens: int = Field(ge=0)
    totalTokens: int = Field(ge=0)
    latencyMs: int = Field(ge=0)


class ModelMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    deployment: str
    model: str
    mode: Literal["mock", "live"]


@dataclass(slots=True)
class AuthenticatedOwner:
    owner_id: str
    tenant_id: str | None
    object_id: str | None
    subject: str
    display_name: str | None
    email: str | None


@dataclass(slots=True)
class StoredCard:
    id: str
    document_type: str
    owner_id: str
    request_id: str
    idempotency_key: str
    request_hash: str
    status: str
    prompt: str | None = None
    prompt_hash: str | None = None
    validated_payload: dict[str, Any] | None = None
    derived_art_prompt: str | None = None
    blob_name: str | None = None
    blob_content_type: str | None = None
    blob_sha256: str | None = None
    blob_size_bytes: int | None = None
    image_url_path: str | None = None
    moderation: list[dict[str, Any]] = field(default_factory=list)
    text_model: dict[str, Any] | None = None
    image_model: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    created_at: str = field(default_factory=lambda: now_iso())
    updated_at: str = field(default_factory=lambda: now_iso())
    completed_at: str | None = None
    error_code: str | None = None
    failure_status_code: int | None = None
    failure_error_code: str | None = None
    failure_title: str | None = None
    failure_detail: str | None = None
    failure_type: str | None = None
    failure_headers: dict[str, str] = field(default_factory=dict)
    retryable: bool = False
    ttl_seconds: int | None = None
    image_quality: Literal["low", "medium", "high"] | None = None

    def to_document(self) -> dict[str, Any]:
        document = {
            "id": self.id,
            "cardId": self.id,
            "documentType": self.document_type,
            "userId": self.owner_id,
            "schemaVersion": 1,
            "owner": {
                "ownerId": self.owner_id,
            },
            "requestId": self.request_id,
            "idempotencyKey": self.idempotency_key,
            "requestHash": self.request_hash,
            "status": self.status,
            "prompt": self.prompt,
            "promptHash": self.prompt_hash,
            "validatedPayload": self.validated_payload,
            "derivedArtPrompt": self.derived_art_prompt,
            "blob": {
                "name": self.blob_name,
                "contentType": self.blob_content_type,
                "sha256": self.blob_sha256,
                "sizeBytes": self.blob_size_bytes,
                "imageUrlPath": self.image_url_path,
            },
            "moderation": self.moderation,
            "textModel": self.text_model,
            "imageModel": self.image_model,
            "usage": self.usage,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "completedAt": self.completed_at,
            "errorCode": self.error_code,
            "failureStatusCode": self.failure_status_code,
            "failureErrorCode": self.failure_error_code,
            "failureTitle": self.failure_title,
            "failureDetail": self.failure_detail,
            "failureType": self.failure_type,
            "failureHeaders": self.failure_headers,
            "retryable": self.retryable,
        }
        if self.ttl_seconds is not None:
            document["ttl"] = self.ttl_seconds
        if self.image_quality is not None:
            document["imageQuality"] = self.image_quality
        return document

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "StoredCard":
        blob = document.get("blob") or {}
        document_id = str(document["id"])
        return cls(
            id=_logical_card_id(document_id, document.get("cardId")),
            document_type=str(document.get("documentType", "card")),
            owner_id=str(document["userId"]),
            request_id=str(document.get("requestId", "")),
            idempotency_key=str(document.get("idempotencyKey", "")),
            request_hash=str(document.get("requestHash", "")),
            status=str(document.get("status", "")),
            prompt=document.get("prompt"),
            prompt_hash=document.get("promptHash"),
            validated_payload=document.get("validatedPayload"),
            derived_art_prompt=document.get("derivedArtPrompt"),
            blob_name=blob.get("name"),
            blob_content_type=blob.get("contentType"),
            blob_sha256=blob.get("sha256"),
            blob_size_bytes=blob.get("sizeBytes"),
            image_url_path=blob.get("imageUrlPath"),
            moderation=list(document.get("moderation") or []),
            text_model=document.get("textModel"),
            image_model=document.get("imageModel"),
            usage=document.get("usage"),
            created_at=str(document.get("createdAt", now_iso())),
            updated_at=str(document.get("updatedAt", now_iso())),
            completed_at=document.get("completedAt"),
            error_code=document.get("errorCode"),
            failure_status_code=document.get("failureStatusCode"),
            failure_error_code=document.get("failureErrorCode"),
            failure_title=document.get("failureTitle"),
            failure_detail=document.get("failureDetail"),
            failure_type=document.get("failureType"),
            failure_headers=dict(document.get("failureHeaders") or {}),
            retryable=bool(document.get("retryable", False)),
            ttl_seconds=document.get("ttl"),
            image_quality=document.get("imageQuality"),
        )


@dataclass(slots=True)
class ImageResult:
    content: bytes
    content_type: str
    revised_prompt: str | None
    labels: set[str] = field(default_factory=set)


@dataclass(slots=True)
class ReferenceImageUpload:
    content: bytes
    content_type: str
    filename: str | None = None


@dataclass(slots=True)
class AITextResult:
    payload: dict[str, Any]
    metadata: ModelMetadata
    usage: UsageAudit


@dataclass(slots=True)
class AIImageResult:
    image: ImageResult
    metadata: ModelMetadata
    usage: UsageAudit


@dataclass(slots=True)
class GenerationProgress:
    stage: str = "reserved"
    validated_payload: GeneratedCardModel | None = None
    derived_art_prompt: str | None = None
    moderation: list[ModerationDecision] = field(default_factory=list)
    text_result: AITextResult | None = None


class RateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def enforce(self, key: str, settings: RateLimitSettings, *, error_suffix: str) -> None:
        async with self._lock:
            now = time.monotonic()
            bucket = self._buckets[key]
            cutoff = now - settings.window_seconds
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= settings.requests:
                raise ProblemDetails(
                    status_code=429,
                    title="Too Many Requests",
                    detail=(
                        f"Rate limit exceeded for {error_suffix}. Retry after "
                        f"{settings.window_seconds} seconds."
                    ),
                    type="/problems/rate-limit",
                    error_code="rate_limit_exceeded",
                    headers={"Retry-After": str(settings.window_seconds)},
                )
            bucket.append(now)


class CsrfProtector:
    SESSION_KEY = "csrf_token"
    HEADER_NAME = "x-csrf-token"

    def issue(self, request) -> str:
        token = request.session.get(self.SESSION_KEY)
        if not isinstance(token, str) or not token:
            token = uuid4().hex + uuid4().hex
            request.session[self.SESSION_KEY] = token
        return token

    def validate(self, request, submitted_token: str | None) -> None:
        session_token = request.session.get(self.SESSION_KEY)
        header_token = request.headers.get(self.HEADER_NAME)
        candidate = submitted_token or header_token
        if (
            not isinstance(session_token, str)
            or not candidate
            or not hmac.compare_digest(
                session_token,
                candidate,
            )
        ):
            raise ProblemDetails(
                status_code=403,
                title="Forbidden",
                detail="A valid CSRF token is required for this action.",
                type="/problems/csrf",
                error_code="csrf_failed",
            )


class HeuristicModerationService:
    def __init__(self, policy_name: str) -> None:
        self.policy_name = policy_name
        self._blocked_text_patterns = (
            ("in the style of", "living-artist-imitation"),
            ("living artist", "living-artist-imitation"),
            ("copyrighted logo", "copyrighted-logo"),
            ("trademark", "trademark-request"),
            ("disney", "copyrighted-character"),
            ("pokemon", "copyrighted-character"),
            ("graphic gore", "graphic-violence"),
            ("sexual minor", "sexual-content-minor"),
            ("self-harm", "self-harm"),
        )

    async def moderate_text(self, text: str, *, stage: str) -> ModerationDecision:
        lowered = text.lower()
        for pattern, reason_code in self._blocked_text_patterns:
            if pattern in lowered:
                decision = ModerationDecision(
                    stage=stage,  # type: ignore[arg-type]
                    allowed=False,
                    reasonCode=reason_code,
                    details=f"Policy {self.policy_name} blocked the request at {stage}.",
                )
                self._record_decision(decision)
                return decision
        decision = ModerationDecision(
            stage=stage,  # type: ignore[arg-type]
            allowed=True,
            reasonCode="allowed",
            details=f"Policy {self.policy_name} allowed the request.",
        )
        self._record_decision(decision)
        return decision

    async def moderate_image(self, image: ImageResult) -> ModerationDecision:
        if "post-image-block" in image.labels:
            decision = ModerationDecision(
                stage="post_image",
                allowed=False,
                reasonCode="unsafe-generated-image",
                details=f"Policy {self.policy_name} rejected the generated image.",
            )
        elif image.content_type != "image/png" or not image.content.startswith(b"\x89PNG"):
            decision = ModerationDecision(
                stage="post_image",
                allowed=False,
                reasonCode="invalid-image-payload",
                details=f"Policy {self.policy_name} rejected the generated image payload.",
            )
        else:
            decision = ModerationDecision(
                stage="post_image",
                allowed=True,
                reasonCode="allowed",
                details=f"Policy {self.policy_name} allowed the generated image.",
            )
        self._record_decision(decision)
        return decision

    def _record_decision(self, decision: ModerationDecision) -> None:
        record_moderation(
            stage=decision.stage,
            allowed=decision.allowed,
            reason=decision.reasonCode,
            policy=self.policy_name,
        )


class AbstractCardRepository:
    async def reserve_document(
        self,
        *,
        owner_id: str,
        card_id: str,
        request_hash: str,
        idempotency_key: str,
        request_id: str,
    ) -> tuple[StoredCard | None, bool]:
        raise NotImplementedError

    async def save(self, record: StoredCard) -> StoredCard:
        raise NotImplementedError

    async def get(self, owner_id: str, card_id: str) -> StoredCard | None:
        raise NotImplementedError

    async def list_by_owner(self, owner_id: str) -> list[StoredCard]:
        raise NotImplementedError

    async def delete(self, owner_id: str, card_id: str) -> None:
        raise NotImplementedError


class AbstractAuditRepository:
    async def reserve_audit(
        self,
        *,
        owner_id: str,
        card_id: str,
        request_hash: str,
        idempotency_key: str,
        request_id: str,
    ) -> tuple[StoredCard | None, bool]:
        raise NotImplementedError

    async def save_audit(self, record: StoredCard) -> None:
        raise NotImplementedError

    async def get_audit(self, owner_id: str, card_id: str) -> StoredCard | None:
        raise NotImplementedError

    async def delete_audit(self, owner_id: str, card_id: str) -> None:
        raise NotImplementedError


class AbstractAssetStore:
    async def upload(self, blob_name: str, payload: bytes, content_type: str) -> dict[str, Any]:
        raise NotImplementedError

    async def download(self, blob_name: str) -> tuple[bytes, str]:
        raise NotImplementedError

    async def delete(self, blob_name: str) -> None:
        raise NotImplementedError


class InMemoryCardRepository(AbstractCardRepository):
    def __init__(self) -> None:
        self._records: dict[tuple[str, str], StoredCard] = {}
        self._lock = asyncio.Lock()

    async def reserve_document(
        self,
        *,
        owner_id: str,
        card_id: str,
        request_hash: str,
        idempotency_key: str,
        request_id: str,
    ) -> tuple[StoredCard | None, bool]:
        async with self._lock:
            key = (owner_id, card_id)
            existing = self._records.get(key)
            if existing is not None:
                return existing, False
            placeholder = StoredCard(
                id=card_id,
                document_type="card",
                owner_id=owner_id,
                request_id=request_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                status="processing",
            )
            self._records[key] = placeholder
            return placeholder, True

    async def save(self, record: StoredCard) -> StoredCard:
        async with self._lock:
            record.updated_at = now_iso()
            self._records[(record.owner_id, record.id)] = record
            return record

    async def get(self, owner_id: str, card_id: str) -> StoredCard | None:
        async with self._lock:
            return self._records.get((owner_id, card_id))

    async def list_by_owner(self, owner_id: str) -> list[StoredCard]:
        async with self._lock:
            records = [
                record
                for (stored_owner_id, _), record in self._records.items()
                if stored_owner_id == owner_id and record.document_type == "card"
            ]
        return sorted(records, key=lambda record: (record.created_at, record.id), reverse=True)

    async def delete(self, owner_id: str, card_id: str) -> None:
        async with self._lock:
            self._records.pop((owner_id, card_id), None)


class InMemoryAuditRepository(AbstractAuditRepository):
    def __init__(self, *, audit_ttl_seconds: int = 30 * 24 * 60 * 60) -> None:
        self._records: dict[tuple[str, str], StoredCard] = {}
        self._lock = asyncio.Lock()
        self._audit_ttl_seconds = audit_ttl_seconds

    async def reserve_audit(
        self,
        *,
        owner_id: str,
        card_id: str,
        request_hash: str,
        idempotency_key: str,
        request_id: str,
    ) -> tuple[StoredCard | None, bool]:
        async with self._lock:
            key = (owner_id, card_id)
            existing = self._records.get(key)
            if existing is not None:
                return existing, False
            placeholder = StoredCard(
                id=card_id,
                document_type="generation-audit",
                owner_id=owner_id,
                request_id=request_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                status="audit_processing",
                ttl_seconds=self._audit_ttl_seconds,
            )
            self._records[key] = placeholder
            return placeholder, True

    async def save_audit(self, record: StoredCard) -> None:
        async with self._lock:
            self._records[(record.owner_id, record.id)] = record

    async def get_audit(self, owner_id: str, card_id: str) -> StoredCard | None:
        async with self._lock:
            return self._records.get((owner_id, card_id))

    async def delete_audit(self, owner_id: str, card_id: str) -> None:
        async with self._lock:
            self._records.pop((owner_id, card_id), None)


class InMemorySharedCardAuditRepository(AbstractCardRepository, AbstractAuditRepository):
    def __init__(self, *, audit_ttl_seconds: int = 30 * 24 * 60 * 60) -> None:
        self._records: dict[tuple[str, str], StoredCard] = {}
        self._lock = asyncio.Lock()
        self._audit_ttl_seconds = audit_ttl_seconds

    async def reserve_document(
        self,
        *,
        owner_id: str,
        card_id: str,
        request_hash: str,
        idempotency_key: str,
        request_id: str,
    ) -> tuple[StoredCard | None, bool]:
        async with self._lock:
            key = (owner_id, _card_document_id(card_id))
            existing = self._records.get(key)
            if existing is not None:
                return existing, False
            placeholder = StoredCard(
                id=card_id,
                document_type="card",
                owner_id=owner_id,
                request_id=request_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                status="processing",
            )
            self._records[key] = placeholder
            return placeholder, True

    async def save(self, record: StoredCard) -> StoredCard:
        async with self._lock:
            record.updated_at = now_iso()
            self._records[(record.owner_id, _card_document_id(record.id))] = record
            return record

    async def get(self, owner_id: str, card_id: str) -> StoredCard | None:
        async with self._lock:
            return self._records.get((owner_id, _card_document_id(card_id)))

    async def list_by_owner(self, owner_id: str) -> list[StoredCard]:
        async with self._lock:
            records = [
                record
                for (stored_owner_id, _), record in self._records.items()
                if stored_owner_id == owner_id and record.document_type == "card"
            ]
        return sorted(records, key=lambda record: (record.created_at, record.id), reverse=True)

    async def delete(self, owner_id: str, card_id: str) -> None:
        async with self._lock:
            self._records.pop((owner_id, _card_document_id(card_id)), None)

    async def reserve_audit(
        self,
        *,
        owner_id: str,
        card_id: str,
        request_hash: str,
        idempotency_key: str,
        request_id: str,
    ) -> tuple[StoredCard | None, bool]:
        async with self._lock:
            key = (owner_id, _audit_document_id(card_id))
            existing = self._records.get(key)
            if existing is not None:
                return existing, False
            placeholder = StoredCard(
                id=card_id,
                document_type="generation-audit",
                owner_id=owner_id,
                request_id=request_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                status="audit_processing",
                ttl_seconds=self._audit_ttl_seconds,
            )
            self._records[key] = placeholder
            return placeholder, True

    async def save_audit(self, record: StoredCard) -> None:
        async with self._lock:
            self._records[(record.owner_id, _audit_document_id(record.id))] = record

    async def get_audit(self, owner_id: str, card_id: str) -> StoredCard | None:
        async with self._lock:
            return self._records.get((owner_id, _audit_document_id(card_id)))

    async def delete_audit(self, owner_id: str, card_id: str) -> None:
        async with self._lock:
            self._records.pop((owner_id, _audit_document_id(card_id)), None)


class InMemoryAssetStore(AbstractAssetStore):
    def __init__(self) -> None:
        self._assets: dict[str, tuple[bytes, str]] = {}
        self._lock = asyncio.Lock()

    async def upload(self, blob_name: str, payload: bytes, content_type: str) -> dict[str, Any]:
        async with self._lock:
            self._assets[blob_name] = (payload, content_type)
            return {
                "blobName": blob_name,
                "contentType": content_type,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "sizeBytes": len(payload),
            }

    async def download(self, blob_name: str) -> tuple[bytes, str]:
        async with self._lock:
            payload = self._assets.get(blob_name)
            if payload is None:
                raise FileNotFoundError(blob_name)
            return payload

    async def delete(self, blob_name: str) -> None:
        async with self._lock:
            self._assets.pop(blob_name, None)


class AzureCosmosCardRepository(AbstractCardRepository, AbstractAuditRepository):
    def __init__(self, settings: AppSettings) -> None:
        from azure.cosmos.aio import CosmosClient

        self._client = CosmosClient(
            settings.cosmos_endpoint,
            credential=_default_azure_credential(),
        )
        self._database_name = settings.cosmos_database_name or "appdb"
        self._container_name = settings.cosmos_container_name or "cards"
        self._audit_ttl_seconds = settings.audit_retention_days * 24 * 60 * 60
        self._container = None

    async def _get_container(self):
        if self._container is None:
            database = self._client.get_database_client(self._database_name)
            self._container = database.get_container_client(self._container_name)
        return self._container

    async def get_health_container_client(self):
        return await self._get_container()

    def _record_document(self, record: StoredCard, *, document_id: str) -> dict[str, Any]:
        document = record.to_document()
        document["id"] = document_id
        return document

    async def _read_raw_document(self, owner_id: str, document_id: str) -> dict[str, Any] | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        container = await self._get_container()
        try:
            return await container.read_item(document_id, partition_key=owner_id)
        except CosmosResourceNotFoundError:
            return None

    def _match_document_type(
        self,
        document: dict[str, Any] | None,
        *,
        expected_document_type: str,
    ) -> StoredCard | None:
        if document is None:
            return None
        if str(document.get("documentType", "card")) != expected_document_type:
            return None
        return StoredCard.from_document(document)

    async def _get_typed_record(
        self,
        owner_id: str,
        logical_card_id: str,
        *,
        document_id: str,
        expected_document_type: str,
    ) -> StoredCard | None:
        record = self._match_document_type(
            await self._read_raw_document(owner_id, document_id),
            expected_document_type=expected_document_type,
        )
        if record is not None:
            return record
        if document_id == logical_card_id:
            return None
        return self._match_document_type(
            await self._read_raw_document(owner_id, logical_card_id),
            expected_document_type=expected_document_type,
        )

    async def _delete_typed_record(
        self,
        owner_id: str,
        logical_card_id: str,
        *,
        document_id: str,
        expected_document_type: str,
    ) -> None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        container = await self._get_container()
        try:
            await container.delete_item(document_id, partition_key=owner_id)
            return
        except CosmosResourceNotFoundError:
            pass

        if document_id == logical_card_id:
            return

        legacy = await self._read_raw_document(owner_id, logical_card_id)
        if legacy is None or str(legacy.get("documentType", "card")) != expected_document_type:
            return
        try:
            await container.delete_item(logical_card_id, partition_key=owner_id)
        except CosmosResourceNotFoundError:
            return

    async def reserve_document(
        self,
        *,
        owner_id: str,
        card_id: str,
        request_hash: str,
        idempotency_key: str,
        request_id: str,
    ) -> tuple[StoredCard | None, bool]:
        from azure.cosmos.exceptions import CosmosResourceExistsError

        existing = await self.get(owner_id, card_id)
        if existing is not None:
            return existing, False

        record = StoredCard(
            id=card_id,
            document_type="card",
            owner_id=owner_id,
            request_id=request_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            status="processing",
        )
        container = await self._get_container()
        try:
            await container.create_item(
                self._record_document(record, document_id=_card_document_id(card_id))
            )
            return record, True
        except CosmosResourceExistsError:
            existing = await self.get(owner_id, card_id)
            return existing, False

    async def save(self, record: StoredCard) -> StoredCard:
        container = await self._get_container()
        record.updated_at = now_iso()
        await container.upsert_item(
            self._record_document(record, document_id=_card_document_id(record.id))
        )
        return record

    async def get(self, owner_id: str, card_id: str) -> StoredCard | None:
        return await self._get_typed_record(
            owner_id,
            card_id,
            document_id=_card_document_id(card_id),
            expected_document_type="card",
        )

    async def list_by_owner(self, owner_id: str) -> list[StoredCard]:
        container = await self._get_container()
        query = (
            "SELECT * FROM c "
            "WHERE c.userId = @ownerId "
            "AND (NOT IS_DEFINED(c.documentType) OR c.documentType = @documentType) "
            "ORDER BY c.createdAt DESC"
        )
        iterator = container.query_items(
            query=query,
            parameters=[
                {"name": "@ownerId", "value": owner_id},
                {"name": "@documentType", "value": "card"},
            ],
            partition_key=owner_id,
        )
        records: list[StoredCard] = []
        async for document in iterator:
            record = self._match_document_type(document, expected_document_type="card")
            if record is not None:
                records.append(record)
        return records

    async def delete(self, owner_id: str, card_id: str) -> None:
        await self._delete_typed_record(
            owner_id,
            card_id,
            document_id=_card_document_id(card_id),
            expected_document_type="card",
        )

    async def save_audit(self, record: StoredCard) -> None:
        container = await self._get_container()
        await container.upsert_item(
            self._record_document(record, document_id=_audit_document_id(record.id))
        )

    async def get_audit(self, owner_id: str, card_id: str) -> StoredCard | None:
        return await self._get_typed_record(
            owner_id,
            card_id,
            document_id=_audit_document_id(card_id),
            expected_document_type="generation-audit",
        )

    async def reserve_audit(
        self,
        *,
        owner_id: str,
        card_id: str,
        request_hash: str,
        idempotency_key: str,
        request_id: str,
    ) -> tuple[StoredCard | None, bool]:
        from azure.cosmos.exceptions import CosmosResourceExistsError

        existing = await self.get_audit(owner_id, card_id)
        if existing is not None:
            return existing, False

        record = StoredCard(
            id=card_id,
            document_type="generation-audit",
            owner_id=owner_id,
            request_id=request_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            status="audit_processing",
            ttl_seconds=self._audit_ttl_seconds,
        )
        container = await self._get_container()
        try:
            await container.create_item(
                self._record_document(record, document_id=_audit_document_id(card_id))
            )
            return record, True
        except CosmosResourceExistsError:
            existing = await self.get_audit(owner_id, card_id)
            return existing, False

    async def delete_audit(self, owner_id: str, card_id: str) -> None:
        await self._delete_typed_record(
            owner_id,
            card_id,
            document_id=_audit_document_id(card_id),
            expected_document_type="generation-audit",
        )


class AzureBlobAssetStore(AbstractAssetStore):
    def __init__(self, settings: AppSettings) -> None:
        from azure.storage.blob import ContentSettings
        from azure.storage.blob.aio import BlobServiceClient

        self._content_settings_class = ContentSettings
        self._container_name = settings.blob_container_name or "card-assets"
        self._service = BlobServiceClient(
            settings.blob_endpoint,
            credential=_default_azure_credential(),
        )
        self._container = self._service.get_container_client(self._container_name)

    def get_health_container_client(self):
        return self._container

    async def upload(self, blob_name: str, payload: bytes, content_type: str) -> dict[str, Any]:
        blob = self._container.get_blob_client(blob_name)
        await blob.upload_blob(
            payload,
            overwrite=True,
            content_settings=self._content_settings_class(content_type=content_type),
        )
        return {
            "blobName": blob_name,
            "contentType": content_type,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "sizeBytes": len(payload),
        }

    async def download(self, blob_name: str) -> tuple[bytes, str]:
        blob = self._container.get_blob_client(blob_name)
        stream = await blob.download_blob()
        props = await blob.get_blob_properties()
        return await stream.readall(), props.content_settings.content_type or "image/png"

    async def delete(self, blob_name: str) -> None:
        blob = self._container.get_blob_client(blob_name)
        try:
            await blob.delete_blob(delete_snapshots="include")
        except Exception:
            raise


class MockAIClient:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._attempts: dict[str, int] = defaultdict(int)

    async def generate_card(self, prompt: str, *, request_id: str) -> AITextResult:
        flags = parse_mock_flags(prompt)
        self._attempts[f"text:{request_id}"] += 1
        attempt = self._attempts[f"text:{request_id}"]
        if "text-429-once" in flags and attempt == 1:
            raise UpstreamServiceError(
                "foundry-text",
                "Transient text throttle.",
                status_code=429,
                retryable=True,
            )
        if "text-500" in flags:
            raise UpstreamServiceError(
                "foundry-text",
                "Text generation failed.",
                status_code=503,
                retryable=False,
            )
        if "text-timeout" in flags:
            await asyncio.sleep(self.settings.retry.text_timeout_seconds + 0.2)
        normalized = strip_mock_flags(prompt)
        payload: dict[str, Any] = {
            "schemaVersion": 1,
            "name": title_from_prompt(normalized),
            "cardType": pick_from_hash(normalized, ["hero", "creature", "artifact", "spell"]),
            "rarity": pick_from_hash(
                normalized + "rarity",
                ["common", "uncommon", "rare", "legendary"],
            ),
            "manaCost": number_from_hash(normalized + "mana", 0, 12),
            "attack": number_from_hash(normalized + "atk", 1, 12),
            "health": number_from_hash(normalized + "hp", 2, 14),
            "rulesText": (
                f"When played, {normalized[:80].lower()} reshapes the battle in your favor."
            ),
            "flavorText": f"Forged from a single idea: {normalized[:60]}",
            "artBrief": (
                "Epic fantasy trading card illustration of "
                f"{normalized[:70]}, dramatic lighting, painterly detail."
            ),
        }
        for image_flag in (
            "image-429-once",
            "image-500",
            "image-timeout",
            "post-image-block",
        ):
            if image_flag in flags:
                payload["artBrief"] = f'{payload["artBrief"]} [[mock:{image_flag}]]'
        if "post-text-block" in flags:
            payload["rulesText"] = "In the style of a living artist with graphic gore."
        if "text-invalid-extra" in flags:
            payload["unexpected"] = "extra"
        if "text-invalid-bounds" in flags:
            payload["attack"] = 999
        latency_ms = 45
        usage = UsageAudit(inputTokens=120, outputTokens=240, totalTokens=360, latencyMs=latency_ms)
        return AITextResult(
            payload=payload,
            metadata=ModelMetadata(
                provider="azure-openai",
                deployment=self.settings.foundry_text_deployment or "mock-gpt-5-5",
                model="gpt-5.5",
                mode="mock",
            ),
            usage=usage,
        )

    async def generate_image(
        self,
        art_prompt: str,
        *,
        request_id: str,
        image_quality: Literal["low", "medium", "high"] | None = None,
    ) -> AIImageResult:
        flags = parse_mock_flags(art_prompt)
        self._attempts[f"image:{request_id}"] += 1
        attempt = self._attempts[f"image:{request_id}"]
        if "image-429-once" in flags and attempt == 1:
            raise UpstreamServiceError(
                "foundry-image",
                "Transient image throttle.",
                status_code=429,
                retryable=True,
            )
        if "image-500" in flags:
            raise UpstreamServiceError(
                "foundry-image",
                "Image generation failed.",
                status_code=502,
                retryable=False,
            )
        if "image-timeout" in flags:
            await asyncio.sleep(self.settings.retry.image_timeout_seconds + 0.2)
        labels: set[str] = set()
        if "post-image-block" in flags:
            labels.add("post-image-block")
        usage = UsageAudit(inputTokens=40, outputTokens=180, totalTokens=220, latencyMs=70)
        return AIImageResult(
            image=ImageResult(
                content=base64.b64decode(PNG_1X1_BASE64),
                content_type="image/png",
                revised_prompt=strip_mock_flags(art_prompt),
                labels=labels,
            ),
            metadata=ModelMetadata(
                provider="azure-openai",
                deployment=self.settings.foundry_image_deployment or "mock-gpt-image-2",
                model="gpt-image-2",
                mode="mock",
            ),
            usage=usage,
        )

    async def generate_image_edit(
        self,
        art_prompt: str,
        *,
        reference_image: ReferenceImageUpload,
        request_id: str,
        image_quality: Literal["low", "medium", "high"] | None = None,
    ) -> AIImageResult:
        return await self.generate_image(
            art_prompt,
            request_id=request_id,
            image_quality=image_quality,
        )


class AzureFoundryAIClient:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._credential = _default_azure_credential()

    async def _access_token(self) -> str:
        token = await asyncio.to_thread(
            self._credential.get_token,
            "https://cognitiveservices.azure.com/.default",
        )
        return token.token

    async def generate_card(self, prompt: str, *, request_id: str) -> AITextResult:
        schema = GeneratedCardModel.model_json_schema()
        payload = {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You generate a safe fantasy trading card as strict JSON only. "
                        "Respect schemaVersion 1 and never add extra fields."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "fantasy_card",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        started_at = time.perf_counter()
        response = await self._post(
            f"/openai/deployments/{self.settings.foundry_text_deployment}/chat/completions",
            payload,
            api_version=self.settings.foundry_api_version,
            service_name="foundry-text",
        )
        choices = response.get("choices") or []
        if not choices:
            raise UpstreamServiceError("foundry-text", "Text generation returned no choices.")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part) for part in content
            )
        if not isinstance(content, str):
            raise UpstreamServiceError("foundry-text", "Text generation returned invalid content.")
        import json

        usage_payload = response.get("usage") or {}
        usage = UsageAudit(
            inputTokens=int(usage_payload.get("prompt_tokens", 0)),
            outputTokens=int(usage_payload.get("completion_tokens", 0)),
            totalTokens=int(usage_payload.get("total_tokens", 0)),
            latencyMs=int((time.perf_counter() - started_at) * 1000),
        )
        return AITextResult(
            payload=json.loads(content),
            metadata=ModelMetadata(
                provider="azure-openai",
                deployment=self.settings.foundry_text_deployment or "",
                model="gpt-5.5",
                mode="live",
            ),
            usage=usage,
        )

    async def generate_image(
        self,
        art_prompt: str,
        *,
        request_id: str,
        image_quality: Literal["low", "medium", "high"] | None = None,
    ) -> AIImageResult:
        quality = image_quality if image_quality is not None else self.settings.image_quality
        if quality not in {"low", "medium", "high"}:
            raise ValueError(f"image_quality must be low, medium, or high; got {quality!r}")
        started_at = time.perf_counter()
        response = await self._post(
            f"/openai/deployments/{self.settings.foundry_image_deployment}/images/generations",
            {
                "prompt": art_prompt,
                "size": self.settings.image_size,
                "quality": quality,
            },
            api_version="2025-04-01-preview",
            service_name="foundry-image",
        )
        data = response.get("data") or []
        if not data:
            raise UpstreamServiceError("foundry-image", "Image generation returned no data.")
        first = data[0]
        b64_image = first.get("b64_json")
        if not isinstance(b64_image, str):
            raise UpstreamServiceError("foundry-image", "Image generation payload was invalid.")
        usage = UsageAudit(
            inputTokens=0,
            outputTokens=0,
            totalTokens=0,
            latencyMs=int((time.perf_counter() - started_at) * 1000),
        )
        return AIImageResult(
            image=ImageResult(
                content=base64.b64decode(b64_image),
                content_type="image/png",
                revised_prompt=first.get("revised_prompt"),
            ),
            metadata=ModelMetadata(
                provider="azure-openai",
                deployment=self.settings.foundry_image_deployment or "",
                model="gpt-image-2",
                mode="live",
            ),
            usage=usage,
        )

    async def generate_image_edit(
        self,
        art_prompt: str,
        *,
        reference_image: ReferenceImageUpload,
        request_id: str,
        image_quality: Literal["low", "medium", "high"] | None = None,
    ) -> AIImageResult:
        quality = image_quality if image_quality is not None else self.settings.image_quality
        if quality not in {"low", "medium", "high"}:
            raise ValueError(f"image_quality must be low, medium, or high; got {quality!r}")
        started_at = time.perf_counter()
        response = await self._post_multipart(
            f"/openai/deployments/{self.settings.foundry_image_deployment}/images/edits",
            data={
                "prompt": art_prompt,
                "size": self.settings.image_size,
                "quality": quality,
            },
            files={
                "image": (
                    reference_image.filename or "reference-image",
                    reference_image.content,
                    reference_image.content_type,
                )
            },
            api_version="2025-04-01-preview",
            service_name="foundry-image",
        )
        data = response.get("data") or []
        if not data:
            raise UpstreamServiceError("foundry-image", "Image edit returned no data.")
        first = data[0]
        b64_image = first.get("b64_json")
        if not isinstance(b64_image, str):
            raise UpstreamServiceError("foundry-image", "Image edit payload was invalid.")
        usage = UsageAudit(
            inputTokens=0,
            outputTokens=0,
            totalTokens=0,
            latencyMs=int((time.perf_counter() - started_at) * 1000),
        )
        return AIImageResult(
            image=ImageResult(
                content=base64.b64decode(b64_image),
                content_type="image/png",
                revised_prompt=first.get("revised_prompt"),
            ),
            metadata=ModelMetadata(
                provider="azure-openai",
                deployment=self.settings.foundry_image_deployment or "",
                model="gpt-image-2",
                mode="live",
            ),
            usage=usage,
        )

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        api_version: str,
        service_name: str,
    ) -> dict[str, Any]:
        token = await self._access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(
            base_url=self.settings.foundry_endpoint,
            timeout=None,
        ) as client:
            response = await client.post(
                f"{path}?api-version={api_version}",
                headers=headers,
                json=payload,
            )
        if response.is_error:
            error_code, diagnostic_message = _azure_error_diagnostic(response, payload)
            raise UpstreamServiceError(
                service_name,
                (
                    f"{service_name} request failed with Azure error "
                    f"{error_code}: {diagnostic_message}"
                ),
                status_code=response.status_code,
                retryable=response.status_code == 429 or response.status_code >= 500,
                error_code=error_code,
                diagnostic_message=diagnostic_message,
            )
        return response.json()

    async def _post_multipart(
        self,
        path: str,
        *,
        data: dict[str, str],
        files: dict[str, tuple[str, bytes, str]],
        api_version: str,
        service_name: str,
    ) -> dict[str, Any]:
        await self._access_token()
        headers = {
            "Authorization": "******",
        }
        async with httpx.AsyncClient(
            base_url=self.settings.foundry_endpoint,
            timeout=None,
        ) as client:
            response = await client.post(
                f"{path}?api-version={api_version}",
                headers=headers,
                data=data,
                files=files,
            )
        if response.is_error:
            error_code, diagnostic_message = _azure_error_diagnostic(response, dict(data))
            raise UpstreamServiceError(
                service_name,
                (
                    f"{service_name} request failed with Azure error "
                    f"{error_code}: {diagnostic_message}"
                ),
                status_code=response.status_code,
                retryable=response.status_code == 429 or response.status_code >= 500,
                error_code=error_code,
                diagnostic_message=diagnostic_message,
            )
        return response.json()


def _azure_error_diagnostic(
    response: httpx.Response,
    request_payload: dict[str, Any],
) -> tuple[str, str]:
    error_code = "unknown_error"
    diagnostic_message = "No Azure error message was returned."
    try:
        payload = response.json()
    except ValueError:
        return error_code, diagnostic_message
    if not isinstance(payload, dict):
        return error_code, diagnostic_message
    error = payload.get("error")
    if not isinstance(error, dict):
        return error_code, diagnostic_message
    if isinstance(error.get("code"), str) and error["code"].strip():
        error_code = _sanitize_diagnostic(error["code"], limit=100)
    if isinstance(error.get("message"), str) and error["message"].strip():
        diagnostic_message = _sanitize_diagnostic(
            _redact_request_content(error["message"], request_payload),
            limit=500,
        )
    return error_code, diagnostic_message


def _sanitize_diagnostic(value: str, *, limit: int) -> str:
    return " ".join(value.split())[:limit]


def _redact_request_content(message: str, payload: dict[str, Any]) -> str:
    sensitive_values: list[str] = []
    prompt = payload.get("prompt")
    if isinstance(prompt, str):
        sensitive_values.append(prompt)
    input_value = payload.get("input")
    if isinstance(input_value, str):
        sensitive_values.append(input_value)
    messages = payload.get("messages")
    if isinstance(messages, list):
        for item in messages:
            if isinstance(item, dict) and isinstance(item.get("content"), str):
                sensitive_values.append(item["content"])
    for value in sensitive_values:
        if len(value) >= 8:
            message = message.replace(value, "<redacted>")
    return message


@dataclass(slots=True)
class AppServices:
    settings: AppSettings
    card_repository: AbstractCardRepository
    audit_repository: AbstractAuditRepository
    asset_store: AbstractAssetStore
    ai_client: Any
    moderation_service: HeuristicModerationService
    rate_limiter: RateLimiter
    csrf_protector: CsrfProtector
    cosmos_health_probe: HealthDependencyProbe | None = None
    blob_health_probe: HealthDependencyProbe | None = None


class CardGenerationService:
    def __init__(self, services: AppServices) -> None:
        self.services = services

    @instrument_generation("generate")
    async def generate_card(
        self,
        *,
        owner: AuthenticatedOwner,
        prompt: str,
        idempotency_key: str,
        request_id: str,
        client_ip: str,
        image_quality: Literal["low", "medium", "high"] | None = None,
        reference_image: ReferenceImageUpload | None = None,
    ) -> CardResponseModel:
        resolved_quality: Literal["low", "medium", "high"] = (
            image_quality if image_quality is not None else self.services.settings.image_quality
        )
        normalized_prompt = normalize_prompt(prompt)
        request_hash = digest_generation_request(normalized_prompt, reference_image)
        card_id = deterministic_card_id(owner.owner_id, idempotency_key)
        reservation, created = await self.services.card_repository.reserve_document(
            owner_id=owner.owner_id,
            card_id=card_id,
            request_hash=request_hash,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
        if reservation and not created:
            if reservation.request_hash != request_hash:
                raise ProblemDetails(
                    status_code=409,
                    title="Conflict",
                    detail=(
                        "The idempotency key has already been used with "
                        "different request content."
                    ),
                    type="/problems/idempotency-conflict",
                    error_code="idempotency_conflict",
                )
            return await self._replay_existing_or_wait(owner.owner_id, card_id)

        try:
            await self._enforce_rate_limits(owner.owner_id, client_ip)
        except ProblemDetails as exc:
            await self._save_audit_failure(
                owner.owner_id,
                card_id,
                request_id,
                idempotency_key,
                request_hash,
                exc.error_code,
                problem=exc,
            )
            await self.services.card_repository.delete(owner.owner_id, card_id)
            raise

        try:
            progress = GenerationProgress()
            result = await asyncio.wait_for(
                self._run_generation(
                    owner=owner,
                    card_id=card_id,
                    prompt=normalized_prompt,
                    request_hash=request_hash,
                    idempotency_key=idempotency_key,
                    request_id=request_id,
                    progress=progress,
                    image_quality=resolved_quality,
                    reference_image=reference_image,
                ),
                timeout=self.services.settings.retry.overall_timeout_seconds,
            )
            return result
        except asyncio.TimeoutError as exc:
            if (
                reference_image is None
                and progress.stage == "foundry-image"
                and progress.validated_payload is not None
                and progress.derived_art_prompt is not None
                and progress.text_result is not None
            ):
                partial = await self._persist_partial(
                    owner=owner,
                    card_id=card_id,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    prompt=normalized_prompt,
                    validated_payload=progress.validated_payload,
                    derived_art_prompt=progress.derived_art_prompt,
                    moderation=progress.moderation,
                    text_result=progress.text_result,
                    image_quality=resolved_quality,
                    partial_reason="image_timeout",
                )
                return self._as_response(partial)
            problem = (
                ProblemDetails(
                    status_code=504,
                    title="Reference Photo Generation Timed Out",
                    detail=(
                        "Azure AI Foundry did not finish the reference-photo " "image edit in time."
                    ),
                    type="/problems/reference-image-timeout",
                    error_code="reference_image_timeout",
                )
                if reference_image is not None
                else ProblemDetails(
                    status_code=504,
                    title="Gateway Timeout",
                    detail="The generation request exceeded the overall timeout.",
                    type="/problems/upstream-timeout",
                    error_code="upstream_timeout",
                )
            )
            await self.services.card_repository.delete(owner.owner_id, card_id)
            await self._save_audit_failure(
                owner.owner_id,
                card_id,
                request_id,
                idempotency_key,
                request_hash,
                problem.error_code,
                problem=problem,
            )
            raise problem from exc

    @instrument_generation("artwork_retry")
    async def retry_artwork(
        self,
        *,
        owner: AuthenticatedOwner,
        card_id: str,
        idempotency_key: str,
        request_id: str,
        client_ip: str,
    ) -> CardResponseModel:
        record = await self.services.card_repository.get(owner.owner_id, card_id)
        if record is None or record.status != "awaiting_artwork_retry":
            raise ProblemDetails(
                status_code=404,
                title="Not Found",
                detail="No retryable card was found for this user.",
                type="/problems/card-not-found",
                error_code="card_not_found",
            )
        request_hash = digest_text(f"retry:{card_id}")
        retry_card_id = deterministic_card_id(owner.owner_id, idempotency_key)
        reservation, created = await self.services.audit_repository.reserve_audit(
            owner_id=owner.owner_id,
            card_id=retry_card_id,
            request_hash=request_hash,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
        if reservation is not None and not created:
            if reservation.request_hash != request_hash:
                raise ProblemDetails(
                    status_code=409,
                    title="Conflict",
                    detail="The artwork retry idempotency key has already been used differently.",
                    type="/problems/idempotency-conflict",
                    error_code="idempotency_conflict",
                )
            return await self._replay_artwork_retry_or_wait(
                owner_id=owner.owner_id,
                card_id=card_id,
                retry_card_id=retry_card_id,
                fallback_record=record,
            )

        try:
            await self._enforce_rate_limits(owner.owner_id, client_ip)
        except ProblemDetails as exc:
            await self._save_audit_failure(
                owner.owner_id,
                retry_card_id,
                request_id,
                idempotency_key,
                request_hash,
                exc.error_code,
                problem=exc,
            )
            raise

        try:
            return await asyncio.wait_for(
                self._complete_artwork(
                    record,
                    request_id=request_id,
                    retry_idempotency_key=idempotency_key,
                ),
                timeout=self.services.settings.retry.overall_timeout_seconds,
            )
        except ProblemDetails as exc:
            await self._save_audit_failure(
                owner.owner_id,
                retry_card_id,
                request_id,
                idempotency_key,
                request_hash,
                exc.error_code,
                problem=exc,
            )
            raise
        except asyncio.TimeoutError:
            problem = ProblemDetails(
                status_code=200,
                title="Artwork Pending",
                detail="Artwork generation timed out and can be retried.",
                type="/problems/artwork-pending",
                error_code="artwork_retry_available",
            )
            await self._save_audit_failure(
                owner.owner_id,
                retry_card_id,
                request_id,
                idempotency_key,
                request_hash,
                problem.error_code,
                problem=problem,
            )
            return self._as_response(record)

    async def fetch_image(self, owner: AuthenticatedOwner, card_id: str) -> tuple[bytes, str]:
        record = await self.services.card_repository.get(owner.owner_id, card_id)
        if record is None or record.status != "completed" or not record.blob_name:
            raise ProblemDetails(
                status_code=404,
                title="Not Found",
                detail="No generated image was found for this user.",
                type="/problems/image-not-found",
                error_code="image_not_found",
            )
        try:
            with telemetry_span(
                "fcg.persistence",
                attributes={
                    "fcg.store": "blob",
                    "fcg.persistence_operation": "download",
                },
            ):
                result = await self.services.asset_store.download(record.blob_name)
            record_persistence(
                store="blob",
                operation="download",
                outcome="completed",
                request_id=None,
            )
            return result
        except FileNotFoundError as exc:
            record_persistence(
                store="blob",
                operation="download",
                outcome="failed",
                request_id=None,
                error_code="image_not_found",
            )
            raise ProblemDetails(
                status_code=404,
                title="Not Found",
                detail="The generated image is unavailable.",
                type="/problems/image-not-found",
                error_code="image_not_found",
            ) from exc

    async def _run_generation(
        self,
        *,
        owner: AuthenticatedOwner,
        card_id: str,
        prompt: str,
        request_hash: str,
        idempotency_key: str,
        request_id: str,
        progress: GenerationProgress,
        image_quality: Literal["low", "medium", "high"],
        reference_image: ReferenceImageUpload | None = None,
    ) -> CardResponseModel:
        moderation: list[ModerationDecision] = []
        progress.stage = "pre-moderation"
        pre_decision = await self.services.moderation_service.moderate_text(
            prompt,
            stage="pre_prompt",
        )
        moderation.append(pre_decision)
        if not pre_decision.allowed:
            problem = ProblemDetails(
                status_code=422,
                title="Prompt Rejected",
                detail="The prompt was rejected by the moderation policy.",
                type="/problems/prompt-rejected",
                error_code="prompt_rejected",
            )
            await self.services.card_repository.delete(owner.owner_id, card_id)
            await self._save_audit_failure(
                owner.owner_id,
                card_id,
                request_id,
                idempotency_key,
                request_hash,
                pre_decision.reasonCode,
                problem=problem,
            )
            raise problem

        progress.stage = "foundry-text"
        try:
            text_result = await self._retry_upstream(
                lambda: self.services.ai_client.generate_card(prompt, request_id=request_id),
                service_name="foundry-text",
                request_id=request_id,
            )
            record_token_usage("text", text_result.usage)
        except ProblemDetails as exc:
            await self.services.card_repository.delete(owner.owner_id, card_id)
            await self._save_audit_failure(
                owner.owner_id,
                card_id,
                request_id,
                idempotency_key,
                request_hash,
                exc.error_code,
                problem=exc,
            )
            raise
        try:
            validated_payload = GeneratedCardModel.model_validate(text_result.payload)
        except ValidationError as exc:
            problem = ProblemDetails(
                status_code=502,
                title="Bad Gateway",
                detail="The text model returned invalid structured output.",
                type="/problems/invalid-model-output",
                error_code="invalid_model_output",
            )
            await self.services.card_repository.delete(owner.owner_id, card_id)
            await self._save_audit_failure(
                owner.owner_id,
                card_id,
                request_id,
                idempotency_key,
                request_hash,
                problem.error_code,
                problem=problem,
            )
            raise problem from exc

        progress.stage = "post-text-moderation"
        post_text = await self.services.moderation_service.moderate_text(
            " ".join(
                [
                    validated_payload.name,
                    validated_payload.rulesText,
                    validated_payload.flavorText,
                    validated_payload.artBrief,
                ]
            ),
            stage="post_text",
        )
        moderation.append(post_text)
        if not post_text.allowed:
            problem = ProblemDetails(
                status_code=422,
                title="Generated Content Rejected",
                detail="The generated card text was rejected by moderation.",
                type="/problems/generated-text-rejected",
                error_code="generated_text_rejected",
            )
            await self.services.card_repository.delete(owner.owner_id, card_id)
            await self._save_audit_failure(
                owner.owner_id,
                card_id,
                request_id,
                idempotency_key,
                request_hash,
                post_text.reasonCode,
                problem=problem,
            )
            raise problem

        derived_art_prompt = derive_art_prompt(validated_payload)
        progress.stage = "art-prompt-moderation"
        post_art_prompt = await self.services.moderation_service.moderate_text(
            derived_art_prompt,
            stage="post_art_prompt",
        )
        moderation.append(post_art_prompt)
        if not post_art_prompt.allowed:
            problem = ProblemDetails(
                status_code=422,
                title="Artwork Prompt Rejected",
                detail="The derived artwork prompt was rejected by moderation.",
                type="/problems/generated-art-rejected",
                error_code="generated_art_rejected",
            )
            await self.services.card_repository.delete(owner.owner_id, card_id)
            await self._save_audit_failure(
                owner.owner_id,
                card_id,
                request_id,
                idempotency_key,
                request_hash,
                post_art_prompt.reasonCode,
                problem=problem,
            )
            raise problem

        progress.validated_payload = validated_payload
        progress.derived_art_prompt = derived_art_prompt
        progress.moderation = moderation
        progress.text_result = text_result
        progress.stage = "foundry-image"
        try:
            if reference_image is None:
                image_result = await self._retry_upstream(
                    lambda q=image_quality: self.services.ai_client.generate_image(
                        derived_art_prompt,
                        request_id=request_id,
                        image_quality=q,
                    ),
                    service_name="foundry-image",
                    request_id=request_id,
                )
            else:
                image_result = await self._retry_upstream(
                    lambda q=image_quality: self.services.ai_client.generate_image_edit(
                        derived_art_prompt,
                        reference_image=reference_image,
                        request_id=request_id,
                        image_quality=q,
                    ),
                    service_name="foundry-image",
                    request_id=request_id,
                    on_foundry_image_failure="error",
                )
            record_token_usage("image", image_result.usage)
        except ProblemDetails as exc:
            if reference_image is not None:
                await self.services.card_repository.delete(owner.owner_id, card_id)
                await self._save_audit_failure(
                    owner.owner_id,
                    card_id,
                    request_id,
                    idempotency_key,
                    request_hash,
                    exc.error_code,
                    problem=exc,
                )
                raise
            partial = await self._persist_partial(
                owner=owner,
                card_id=card_id,
                request_id=request_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                prompt=prompt,
                validated_payload=validated_payload,
                derived_art_prompt=derived_art_prompt,
                moderation=moderation,
                text_result=text_result,
                partial_reason="image_failure",
                image_quality=image_quality,
            )
            return self._as_response(partial)

        progress.stage = "post-image-moderation"
        post_image = await self.services.moderation_service.moderate_image(image_result.image)
        moderation.append(post_image)
        if not post_image.allowed:
            partial = await self._persist_partial(
                owner=owner,
                card_id=card_id,
                request_id=request_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                prompt=prompt,
                validated_payload=validated_payload,
                derived_art_prompt=derived_art_prompt,
                moderation=moderation,
                text_result=text_result,
                partial_reason="moderation_rejection",
                image_quality=image_quality,
            )
            await self._save_audit_failure(
                owner.owner_id,
                f"{card_id}:unsafe-image",
                request_id,
                idempotency_key,
                request_hash,
                post_image.reasonCode,
            )
            return self._as_response(partial)

        progress.stage = "persistence"
        completed = await self._persist_completed(
            owner=owner,
            card_id=card_id,
            request_id=request_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            prompt=prompt,
            validated_payload=validated_payload,
            derived_art_prompt=derived_art_prompt,
            moderation=moderation,
            text_result=text_result,
            image_result=image_result,
        )
        return self._as_response(completed)

    async def _complete_artwork(
        self,
        record: StoredCard,
        *,
        request_id: str,
        retry_idempotency_key: str,
    ) -> CardResponseModel:
        retry_card_id = deterministic_card_id(record.owner_id, retry_idempotency_key)
        request_hash = digest_text(f"retry:{record.id}")
        if record.validated_payload is None or record.derived_art_prompt is None:
            raise ProblemDetails(
                status_code=409,
                title="Conflict",
                detail="The card is missing validated content required for an artwork retry.",
                type="/problems/retry-conflict",
                error_code="retry_conflict",
            )
        retry_quality: Literal["low", "medium", "high"] = (
            record.image_quality
            if record.image_quality is not None
            else self.services.settings.image_quality
        )
        try:
            image_result = await self._retry_upstream(
                lambda q=retry_quality: self.services.ai_client.generate_image(
                    record.derived_art_prompt or "", request_id=request_id, image_quality=q
                ),
                service_name="foundry-image",
                request_id=request_id,
            )
            record_token_usage("image", image_result.usage)
        except ProblemDetails as exc:
            await self._save_audit_failure(
                record.owner_id,
                retry_card_id,
                request_id,
                retry_idempotency_key,
                request_hash,
                exc.error_code,
                problem=exc,
            )
            return self._as_response(record)
        post_image = await self.services.moderation_service.moderate_image(image_result.image)
        moderation = [ModerationDecision.model_validate(item) for item in record.moderation]
        moderation.append(post_image)
        if not post_image.allowed:
            await self._save_audit_failure(
                record.owner_id,
                retry_card_id,
                request_id,
                retry_idempotency_key,
                request_hash,
                post_image.reasonCode,
            )
            return self._as_response(record)
        payload = GeneratedCardModel.model_validate(record.validated_payload)
        completed = await self._persist_completed(
            owner=AuthenticatedOwner(
                owner_id=record.owner_id,
                tenant_id=None,
                object_id=None,
                subject="",
                display_name=None,
                email=None,
            ),
            card_id=record.id,
            request_id=request_id,
            idempotency_key=record.idempotency_key,
            request_hash=record.request_hash,
            prompt=record.prompt or "",
            validated_payload=payload,
            derived_art_prompt=record.derived_art_prompt,
            moderation=moderation,
            text_result=AITextResult(
                payload=record.validated_payload,
                metadata=ModelMetadata.model_validate(record.text_model or {}),
                usage=UsageAudit.model_validate((record.usage or {}).get("text", {})),
            ),
            image_result=image_result,
        )
        await self.services.audit_repository.save_audit(
            StoredCard(
                id=retry_card_id,
                document_type="generation-audit",
                owner_id=record.owner_id,
                request_id=request_id,
                idempotency_key=retry_idempotency_key,
                request_hash=request_hash,
                status="audit_completed",
                ttl_seconds=self.services.settings.audit_retention_days * 24 * 60 * 60,
            )
        )
        return self._as_response(completed)

    async def _persist_partial(
        self,
        *,
        owner: AuthenticatedOwner,
        card_id: str,
        request_id: str,
        idempotency_key: str,
        request_hash: str,
        prompt: str,
        validated_payload: GeneratedCardModel,
        derived_art_prompt: str,
        moderation: list[ModerationDecision],
        text_result: AITextResult,
        image_quality: Literal["low", "medium", "high"],
        partial_reason: str,
    ) -> StoredCard:
        record = StoredCard(
            id=card_id,
            document_type="card",
            owner_id=owner.owner_id,
            request_id=request_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            status="awaiting_artwork_retry",
            prompt=prompt,
            prompt_hash=digest_text(prompt),
            validated_payload=validated_payload.model_dump(),
            derived_art_prompt=derived_art_prompt,
            moderation=[item.model_dump() for item in moderation],
            text_model=text_result.metadata.model_dump(),
            image_model=None,
            usage={"text": text_result.usage.model_dump()},
            image_quality=image_quality,
        )
        with telemetry_span(
            "fcg.persistence",
            request_id=request_id,
            attributes={
                "fcg.store": "card",
                "fcg.persistence_operation": "save_partial",
            },
        ):
            try:
                saved = await self.services.card_repository.save(record)
            except Exception as exc:
                record_persistence(
                    store="card",
                    operation="save_partial",
                    outcome="failed",
                    request_id=request_id,
                    error_code=normalize_error_code(type(exc).__name__),
                )
                raise
            record_partial(partial_reason)
            record_persistence(
                store="card",
                operation="save_partial",
                outcome="completed",
                request_id=request_id,
            )
            return saved

    async def _persist_completed(
        self,
        *,
        owner: AuthenticatedOwner,
        card_id: str,
        request_id: str,
        idempotency_key: str,
        request_hash: str,
        prompt: str,
        validated_payload: GeneratedCardModel,
        derived_art_prompt: str,
        moderation: list[ModerationDecision],
        text_result: AITextResult,
        image_result: AIImageResult,
    ) -> StoredCard:
        blob_name = build_blob_name(owner.owner_id, card_id)
        blob_uploaded = False
        blob_metadata: dict[str, Any] | None = None
        persistence_stage = "blob-upload"
        try:
            with telemetry_span(
                "fcg.persistence",
                request_id=request_id,
                attributes={
                    "fcg.store": "blob",
                    "fcg.persistence_operation": "upload",
                },
            ):
                blob_metadata = await self.services.asset_store.upload(
                    blob_name,
                    image_result.image.content,
                    image_result.image.content_type,
                )
                record_persistence(
                    store="blob",
                    operation="upload",
                    outcome="completed",
                    request_id=request_id,
                )
            blob_uploaded = True
            persistence_stage = "cosmos-write"
            record = StoredCard(
                id=card_id,
                document_type="card",
                owner_id=owner.owner_id,
                request_id=request_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                status="completed",
                prompt=prompt,
                prompt_hash=digest_text(prompt),
                validated_payload=validated_payload.model_dump(),
                derived_art_prompt=derived_art_prompt,
                blob_name=blob_metadata["blobName"],
                blob_content_type=blob_metadata["contentType"],
                blob_sha256=blob_metadata["sha256"],
                blob_size_bytes=blob_metadata["sizeBytes"],
                image_url_path=f"/cards/{card_id}/image",
                moderation=[item.model_dump() for item in moderation],
                text_model=text_result.metadata.model_dump(),
                image_model=image_result.metadata.model_dump(),
                usage={
                    "text": text_result.usage.model_dump(),
                    "image": image_result.usage.model_dump(),
                    "totalLatencyMs": text_result.usage.latencyMs + image_result.usage.latencyMs,
                },
                completed_at=now_iso(),
            )
            with telemetry_span(
                "fcg.persistence",
                request_id=request_id,
                attributes={
                    "fcg.store": "card",
                    "fcg.persistence_operation": "save_completed",
                },
            ):
                saved = await self.services.card_repository.save(record)
                record_persistence(
                    store="card",
                    operation="save_completed",
                    outcome="completed",
                    request_id=request_id,
                )
                return saved
        except Exception as exc:
            _log_persistence_exception(
                event="persistence-failed",
                request_id=request_id,
                stage=persistence_stage,
                exc=exc,
            )
            if blob_uploaded and blob_metadata is not None:
                try:
                    await self.services.asset_store.delete(blob_name)
                    record_persistence(
                        store="blob",
                        operation="compensate",
                        outcome="completed",
                        request_id=request_id,
                    )
                    safe_persistence_log(
                        event="compensation-succeeded",
                        request_id=request_id,
                        stage="compensation-delete",
                        status_code=None,
                        azure_error_code=None,
                    )
                    add_event(
                        "compensation.completed",
                        {
                            "fcg.store": "blob",
                            "fcg.persistence_operation": "compensate",
                            "fcg.outcome": "completed",
                        },
                    )
                except Exception as cleanup_exc:
                    _log_persistence_exception(
                        event="compensation-failed",
                        request_id=request_id,
                        stage="compensation-delete",
                        exc=cleanup_exc,
                    )
            try:
                await self.services.card_repository.delete(owner.owner_id, card_id)
            except Exception as cleanup_exc:
                _log_persistence_exception(
                    event="compensation-failed",
                    request_id=request_id,
                    stage="cosmos-delete",
                    exc=cleanup_exc,
                )
            problem = ProblemDetails(
                status_code=503,
                title="Service Unavailable",
                detail="The generated card could not be persisted safely.",
                type="/problems/persistence-failure",
                error_code="persistence_failure",
            )
            try:
                await self._save_audit_failure(
                    owner.owner_id,
                    card_id,
                    request_id,
                    idempotency_key,
                    request_hash,
                    "persistence_failure",
                    problem=problem,
                )
            except Exception as audit_exc:
                _log_persistence_exception(
                    event="persistence-failed",
                    request_id=request_id,
                    stage="audit-write",
                    exc=audit_exc,
                )
            raise problem from exc

    async def _save_audit_failure(
        self,
        owner_id: str,
        card_id: str,
        request_id: str,
        idempotency_key: str,
        request_hash: str,
        error_code: str,
        *,
        problem: ProblemDetails | None = None,
    ) -> None:
        with telemetry_span(
            "fcg.persistence",
            request_id=request_id,
            attributes={
                "fcg.store": "audit",
                "fcg.persistence_operation": "save_failure",
            },
        ):
            try:
                await self.services.audit_repository.save_audit(
                    StoredCard(
                        id=card_id,
                        document_type="generation-audit",
                        owner_id=owner_id,
                        request_id=request_id,
                        idempotency_key=idempotency_key,
                        request_hash=request_hash,
                        status="audit_failed",
                        error_code=error_code,
                        failure_status_code=problem.status_code if problem is not None else None,
                        failure_error_code=problem.error_code if problem is not None else None,
                        failure_title=problem.title if problem is not None else None,
                        failure_detail=problem.detail if problem is not None else None,
                        failure_type=problem.type if problem is not None else None,
                        failure_headers=dict(problem.headers) if problem is not None else {},
                        ttl_seconds=self.services.settings.audit_retention_days * 24 * 60 * 60,
                    )
                )
            except Exception as exc:
                record_persistence(
                    store="audit",
                    operation="save_failure",
                    outcome="failed",
                    request_id=request_id,
                    error_code=normalize_error_code(type(exc).__name__),
                )
                raise
            record_persistence(
                store="audit",
                operation="save_failure",
                outcome="completed",
                request_id=request_id,
                error_code=error_code,
            )

    async def _replay_existing_or_wait(self, owner_id: str, card_id: str) -> CardResponseModel:
        deadline = time.monotonic() + self.services.settings.retry.overall_timeout_seconds + 0.5
        while time.monotonic() < deadline:
            record = await self.services.card_repository.get(owner_id, card_id)
            if record is not None and record.status in {"completed", "awaiting_artwork_retry"}:
                return self._as_response(record)

            audit = await self.services.audit_repository.get_audit(owner_id, card_id)
            if audit is not None and audit.status == "audit_failed":
                raise self._problem_from_audit(audit)
            await asyncio.sleep(0.05)

        raise ProblemDetails(
            status_code=504,
            title="Gateway Timeout",
            detail="An identical request did not complete before the replay timeout.",
            type="/problems/request-replay-timeout",
            error_code="request_replay_timeout",
        )

    async def _replay_artwork_retry_or_wait(
        self,
        *,
        owner_id: str,
        card_id: str,
        retry_card_id: str,
        fallback_record: StoredCard,
    ) -> CardResponseModel:
        deadline = time.monotonic() + self.services.settings.retry.overall_timeout_seconds + 0.5
        while time.monotonic() < deadline:
            audit = await self.services.audit_repository.get_audit(owner_id, retry_card_id)
            if audit is not None:
                if audit.status in {"audit_completed", "completed"}:
                    refreshed = await self.services.card_repository.get(owner_id, card_id)
                    if refreshed is not None:
                        return self._as_response(refreshed)
                if audit.status == "audit_failed":
                    if audit.failure_status_code is not None and audit.failure_status_code >= 400:
                        raise self._problem_from_audit(audit)
                    refreshed = await self.services.card_repository.get(owner_id, card_id)
                    return self._as_response(refreshed or fallback_record)
            await asyncio.sleep(0.05)
        raise ProblemDetails(
            status_code=504,
            title="Gateway Timeout",
            detail="An identical artwork retry did not complete before the replay timeout.",
            type="/problems/request-replay-timeout",
            error_code="request_replay_timeout",
        )

    def _problem_from_audit(self, audit: StoredCard) -> ProblemDetails:
        if (
            audit.failure_status_code is not None
            and audit.failure_title is not None
            and audit.failure_detail is not None
            and audit.failure_type is not None
        ):
            return ProblemDetails(
                status_code=audit.failure_status_code,
                title=audit.failure_title,
                detail=audit.failure_detail,
                type=audit.failure_type,
                error_code=audit.failure_error_code or audit.error_code or "generation_failed",
                headers=dict(audit.failure_headers),
            )

        error_code = audit.error_code or "generation_failed"
        if error_code in {
            "prompt_rejected",
            "living-artist-imitation",
            "copyrighted-character",
        }:
            return ProblemDetails(
                status_code=422,
                title="Prompt Rejected",
                detail="The prompt was rejected by the moderation policy.",
                type="/problems/prompt-rejected",
                error_code="prompt_rejected",
            )
        return ProblemDetails(
            status_code=503,
            title="Service Unavailable",
            detail=(
                "The same request previously failed safely and can be "
                "retried with a new idempotency key."
            ),
            type="/problems/replayed-failure",
            error_code=error_code,
        )

    async def _retry_upstream(
        self,
        operation: Callable[[], Awaitable[Any]],
        *,
        service_name: str,
        request_id: str,
        on_foundry_image_failure: Literal["pending", "error"] = "pending",
    ) -> Any:
        timeout_seconds, max_retries = self._upstream_policy(service_name)
        for attempt in range(max_retries + 1):
            started = time.perf_counter()
            with telemetry_span(
                "fcg.dependency",
                request_id=request_id,
                attributes={"fcg.dependency": service_name},
            ):
                attempt_name = (
                    ("first", "retry_1", "retry_2")[attempt] if attempt <= 2 else "retry_many"
                )
                add_event(
                    "dependency.started",
                    {
                        "fcg.dependency": service_name,
                        "fcg.attempt": attempt_name,
                    },
                )
                try:
                    result = await asyncio.wait_for(operation(), timeout=timeout_seconds)
                except asyncio.TimeoutError as exc:
                    record_dependency_attempt(
                        dependency=service_name,
                        attempt=attempt + 1,
                        outcome="timed_out",
                        duration_ms=(time.perf_counter() - started) * 1000,
                        request_id=request_id,
                        error_code="upstream_timeout",
                        retryable=attempt < max_retries,
                    )
                    if attempt >= max_retries:
                        if (
                            service_name == "foundry-image"
                            and on_foundry_image_failure == "pending"
                        ):
                            raise ProblemDetails(
                                status_code=200,
                                title="Artwork Pending",
                                detail="Artwork generation timed out after text generation.",
                                type="/problems/artwork-pending",
                                error_code="artwork_retry_available",
                            ) from exc
                        if service_name == "foundry-image":
                            raise ProblemDetails(
                                status_code=504,
                                title="Reference Photo Generation Timed Out",
                                detail=(
                                    "Azure AI Foundry did not finish the reference-photo "
                                    "image edit in time."
                                ),
                                type="/problems/reference-image-timeout",
                                error_code="reference_image_timeout",
                            ) from exc
                        raise ProblemDetails(
                            status_code=504,
                            title="Gateway Timeout",
                            detail="An upstream dependency timed out.",
                            type="/problems/upstream-timeout",
                            error_code="upstream_timeout",
                        ) from exc
                except UpstreamServiceError as exc:
                    outcome = "throttled" if exc.status_code == 429 else "failed"
                    record_dependency_attempt(
                        dependency=exc.service,
                        attempt=attempt + 1,
                        outcome=outcome,
                        duration_ms=(time.perf_counter() - started) * 1000,
                        request_id=request_id,
                        error_code=exc.error_code or "dependency_error",
                        retryable=exc.retryable,
                    )
                    if not exc.retryable or attempt >= max_retries:
                        if exc.service == "foundry-image" and on_foundry_image_failure == "pending":
                            raise ProblemDetails(
                                status_code=200,
                                title="Artwork Pending",
                                detail="Artwork generation failed after text generation.",
                                type="/problems/artwork-pending",
                                error_code="artwork_retry_available",
                            ) from exc
                        if exc.service == "foundry-image":
                            raise self._reference_image_problem(exc) from exc
                        raise ProblemDetails(
                            status_code=502,
                            title="Bad Gateway",
                            detail="An upstream dependency returned a non-retryable error.",
                            type="/problems/upstream-failure",
                            error_code="upstream_failure",
                        ) from exc
                else:
                    record_dependency_attempt(
                        dependency=service_name,
                        attempt=attempt + 1,
                        outcome="completed",
                        duration_ms=(time.perf_counter() - started) * 1000,
                        request_id=request_id,
                    )
                    return result
            record_retry(
                dependency=service_name,
                attempt=attempt + 2,
                request_id=request_id,
            )
            await asyncio.sleep(self.services.settings.retry.base_backoff_seconds * (2**attempt))
        raise AssertionError("unreachable")

    def _reference_image_problem(self, exc: UpstreamServiceError) -> ProblemDetails:
        error_code = (exc.error_code or "").lower()
        unsupported = exc.status_code in {400, 404, 405, 415, 422, 501} or any(
            marker in error_code
            for marker in ("unsupported", "notsupported", "not_supported", "invalidimage")
        )
        if unsupported:
            return ProblemDetails(
                status_code=502,
                title="Reference Photo Unsupported",
                detail=(
                    "Azure AI Foundry image edits are not available on the configured "
                    "deployment or region."
                ),
                type="/problems/reference-image-unsupported",
                error_code="reference_image_unsupported",
            )
        return ProblemDetails(
            status_code=502,
            title="Reference Photo Generation Failed",
            detail="Azure AI Foundry failed to generate artwork from the uploaded photo.",
            type="/problems/reference-image-upstream-failure",
            error_code="reference_image_generation_failed",
        )

    def _upstream_policy(self, service_name: str) -> tuple[float, int]:
        retry = self.services.settings.retry
        if service_name == "foundry-image":
            return retry.image_timeout_seconds, retry.image_max_retries
        return retry.text_timeout_seconds, retry.max_retries

    async def _enforce_rate_limits(self, owner_id: str, client_ip: str) -> None:
        await self.services.rate_limiter.enforce(
            f"user:{owner_id}",
            self.services.settings.user_rate_limit,
            error_suffix="this user",
        )
        await self.services.rate_limiter.enforce(
            f"ip:{client_ip}",
            self.services.settings.ip_rate_limit,
            error_suffix="this IP",
        )

    def _as_response(self, record: StoredCard) -> CardResponseModel:
        payload = GeneratedCardModel.model_validate(record.validated_payload or {})
        actions: list[ActionModel] = []
        if record.status == "awaiting_artwork_retry":
            actions.append(
                ActionModel(
                    type="retry_artwork",
                    href=f"/api/v1/cards/{record.id}/artwork/retry",
                )
            )
        return CardResponseModel(
            cardId=record.id,
            status="completed" if record.status == "completed" else "awaiting_artwork_retry",
            requestId=record.request_id,
            idempotencyKey=record.idempotency_key,
            ownerId=record.owner_id,
            name=payload.name,
            cardType=payload.cardType,
            rarity=payload.rarity,
            manaCost=payload.manaCost,
            attack=payload.attack,
            health=payload.health,
            rulesText=payload.rulesText,
            flavorText=payload.flavorText,
            imageUrl=record.image_url_path,
            actions=actions,
        )


def create_services(settings: AppSettings) -> AppServices:
    if settings.persistence_mode == "azure":
        card_repository = AzureCosmosCardRepository(settings)
        audit_repository = card_repository
        asset_store = AzureBlobAssetStore(settings)
        cosmos_health_probe: HealthDependencyProbe | None = AzureCosmosHealthProbe(
            get_container_client=card_repository.get_health_container_client,
            endpoint=settings.cosmos_endpoint,
            database_name=settings.cosmos_database_name,
            container_name=settings.cosmos_container_name,
        )
        blob_health_probe: HealthDependencyProbe | None = AzureBlobHealthProbe(
            get_container_client=asset_store.get_health_container_client,
            endpoint=settings.blob_endpoint,
            container_name=settings.blob_container_name,
        )
    else:
        card_repository = InMemoryCardRepository()
        audit_repository = InMemoryAuditRepository(
            audit_ttl_seconds=settings.audit_retention_days * 24 * 60 * 60
        )
        asset_store = InMemoryAssetStore()
        cosmos_health_probe = NotApplicableHealthProbe("cosmos")
        blob_health_probe = NotApplicableHealthProbe("blob")

    ai_client = (
        MockAIClient(settings) if settings.ai_mode == "mock" else AzureFoundryAIClient(settings)
    )
    return AppServices(
        settings=settings,
        card_repository=card_repository,
        audit_repository=audit_repository,
        asset_store=asset_store,
        ai_client=ai_client,
        moderation_service=HeuristicModerationService(settings.moderation_policy_name),
        rate_limiter=RateLimiter(),
        csrf_protector=CsrfProtector(),
        cosmos_health_probe=cosmos_health_probe,
        blob_health_probe=blob_health_probe,
    )


def derive_art_prompt(payload: GeneratedCardModel) -> str:
    return (
        f"Create a safe original fantasy trading card illustration for '{payload.name}'. "
        f"Type: {payload.cardType}. Rarity: {payload.rarity}. "
        f"Art direction: {payload.artBrief}"
    )


def normalize_prompt(prompt: str) -> str:
    normalized = " ".join(prompt.split())
    if len(normalized) < 12:
        raise ProblemDetails(
            status_code=422,
            title="Invalid Prompt",
            detail="The prompt must be at least 12 characters after trimming.",
            type="/problems/invalid-prompt",
            error_code="invalid_prompt",
        )
    if len(normalized) > 400:
        raise ProblemDetails(
            status_code=422,
            title="Invalid Prompt",
            detail="The prompt must be 400 characters or fewer.",
            type="/problems/invalid-prompt",
            error_code="invalid_prompt",
        )
    return normalized


def deterministic_card_id(owner_id: str, idempotency_key: str) -> str:
    return hashlib.sha256(f"{owner_id}:{idempotency_key}".encode("utf-8")).hexdigest()[:32]


def build_blob_name(owner_id: str, card_id: str) -> str:
    owner_hash = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:16]
    return f"cards/{owner_hash}/{card_id}.png"


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_generation_request(
    prompt: str,
    reference_image: ReferenceImageUpload | None = None,
) -> str:
    digest = hashlib.sha256()
    digest.update(prompt.encode("utf-8"))
    digest.update(b"\0photo:")
    if reference_image is None:
        digest.update(b"none")
    else:
        digest.update(reference_image.content_type.encode("utf-8"))
        digest.update(b"\0")
        digest.update(reference_image.content)
    return digest.hexdigest()


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_mock_flags(prompt: str) -> set[str]:
    flags: set[str] = set()
    for segment in prompt.split("[[mock:"):
        if "]]" not in segment:
            continue
        flag, *_ = segment.split("]]", 1)
        flags.add(flag.strip())
    return flags


def strip_mock_flags(prompt: str) -> str:
    cleaned = prompt
    for flag in parse_mock_flags(prompt):
        cleaned = cleaned.replace(f"[[mock:{flag}]]", "")
    return " ".join(cleaned.split())


def pick_from_hash(seed: str, values: list[str]) -> str:
    return values[int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16) % len(values)]


def number_from_hash(seed: str, minimum: int, maximum: int) -> int:
    span = maximum - minimum + 1
    return minimum + (int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16) % span)


def title_from_prompt(prompt: str) -> str:
    words = [word.capitalize() for word in prompt.split()[:4]]
    return " ".join(words)[:80] or "Unnamed Hero"


def client_ip_from_request(request, *, trusted_proxy_hops: int = 0) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    forwarded_proto = request.headers.get("x-forwarded-proto")
    if trusted_proxy_hops > 0 and forwarded and forwarded_proto:
        hops = [segment.strip() for segment in forwarded.split(",") if segment.strip()]
        if len(hops) >= trusted_proxy_hops:
            return hops[-trusted_proxy_hops]
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _default_azure_credential():
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential(exclude_interactive_browser_credential=False)
