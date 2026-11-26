from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
import warnings
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from typing import TYPE_CHECKING, Annotated, Any, Literal
from uuid import uuid4

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app import agent_detail
from app.card_artwork import (
    compose_card_artwork,
    inscribe_card_abilities,
    normalize_scene,
    validate_subject_cutout,
)
from app.card_styles import (
    DEFAULT_LAYOUT,
    RECIPE_LAYOUTS,
    ArtworkLayout,
    CardStyle,
    load_recipe,
    normalize_accent,
)

if TYPE_CHECKING:
    from app.profiles import ProfileRepository

from app.health import (
    AzureBlobHealthProbe,
    AzureCosmosHealthProbe,
    HealthDependencyProbe,
    NotApplicableHealthProbe,
)
from app.problems import ProblemDetails
from app.settings import AppSettings, RateLimitSettings, parse_image_size
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
    safe_log,
    safe_persistence_log,
    set_generation_path,
    telemetry_span,
)

PNG_1X1_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO6r2w0AAAAASUVORK5CYII="
)
CARD_DOCUMENT_ID_PREFIX = "card:"
AUDIT_DOCUMENT_ID_PREFIX = "audit:"


class _GenerationRefusal(ProblemDetails):
    pass


class _RetrySuperseded(Exception):
    """Another retry completed (or the card was deleted) before this one committed."""


@dataclass(slots=True)
class _GenerationFlight:
    request_hash: str
    completed: asyncio.Event = field(default_factory=asyncio.Event)
    response: CardResponseModel | None = None
    error: BaseException | None = None


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
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.service = service
        self.status_code = status_code
        self.retryable = retryable
        self.error_code = error_code
        self.diagnostic_message = diagnostic_message
        self.retry_after_seconds = retry_after_seconds


class CardAbility(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=80)
    description: str = Field(min_length=20, max_length=180)


def _require_abilities_in_output_schema(schema: dict[str, Any]) -> None:
    # Strict model output requires the key; historical stored cards may omit it.
    schema["required"].extend(["abilities", "accentColor", "inscription"])


class CardInscription(BaseModel):
    """Hosted metadata: a printed subtype and compatibility-only ability labels."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    subtype: str = Field(min_length=2, max_length=32, pattern=r"^[A-Za-z]+([ -][A-Za-z]+)*$")
    abilityNames: list[Annotated[str, Field(min_length=2, max_length=48)]] = Field(max_length=5)


class CardTextFields(BaseModel):
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


class CardPayload(CardTextFields):
    """Additive storage contract: historical cards keep their original text bounds."""

    abilities: list[CardAbility] = Field(default_factory=list, max_length=5)
    accentColor: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    inscription: CardInscription | None = None

    @model_validator(mode="after")
    def print_labels_match_selection(self):
        if self.inscription and len(self.inscription.abilityNames) != len(self.abilities):
            raise ValueError("ability_print_label_count_mismatch")
        return self

    def printed_abilities(self) -> list[CardAbility]:
        return self.abilities

    def printed_type(self) -> str | None:
        if not self.inscription:
            return None
        kind = "Creature" if self.cardType in {"creature", "hero"} else self.cardType.capitalize()
        return f"{kind} — {self.inscription.subtype}"

    def matches_expertise(self, selected: list[str] | None) -> bool:
        return [ability.name for ability in self.abilities] == (selected or [])


class GeneratedCardAbility(CardAbility):
    description: str = Field(min_length=20, max_length=120)


class GeneratedCardModel(CardPayload):
    model_config = ConfigDict(json_schema_extra=_require_abilities_in_output_schema)

    # The Web route caps selections at three; five keeps the hosted wire compatible with
    # previously released Web revisions that may still request up to five expertises.
    abilities: list[GeneratedCardAbility] = Field(default_factory=list, max_length=5)
    flavorText: str = Field(min_length=0, max_length=90)


def payload_fits_recipe(payload: CardPayload) -> bool:
    """Whether generated text satisfies the recipe bounds (older hosted versions may not)."""
    try:
        GeneratedCardModel.model_validate(payload.model_dump())
    except ValidationError:
        return False
    return len(payload.abilities) <= load_recipe().generation_limits.abilities_max


def stored_payload(payload: CardPayload) -> dict[str, Any]:
    # Earlier Web revisions reject unknown validatedPayload keys.
    return payload.model_dump(exclude={"accentColor", "inscription"})


class CardGenerateBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    prompt: str = Field(min_length=12, max_length=2500)
    idempotencyKey: str | None = Field(default=None, min_length=8, max_length=128)
    csrfToken: str | None = Field(default=None, min_length=8, max_length=256)
    savedPhotoId: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("idempotencyKey", "csrfToken", "savedPhotoId", mode="before")
    @classmethod
    def _normalize_optional_strings(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


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
    abilities: list[CardAbility] = Field(default_factory=list, exclude=True)
    abilitiesEmbedded: bool = Field(default=False, exclude=True)
    cardFrameComposed: bool = Field(default=False, exclude=True)
    typeLine: str | None = Field(default=None, exclude=True)


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
    abilities_embedded: bool = False
    artwork_layout: ArtworkLayout | None = None
    card_style: CardStyle | None = None
    inscription: CardInscription | None = None
    artwork_assets: dict[str, dict[str, Any]] = field(default_factory=dict)

    def card_payload(self) -> CardPayload:
        return CardPayload.model_validate(
            (self.validated_payload or {}) | {"inscription": self.inscription}
        )

    @property
    def card_frame_composed(self) -> bool:
        return self.status == "completed" and (
            self.artwork_layout in RECIPE_LAYOUTS
            or (self.abilities_embedded and self.artwork_layout == "illustration_only_v1")
        )

    @property
    def asset_names(self) -> list[str]:
        return list(
            dict.fromkeys(
                name
                for name in [
                    self.blob_name,
                    *(asset.get("blobName") for asset in self.artwork_assets.values()),
                ]
                if isinstance(name, str) and name
            )
        )

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
        if self.abilities_embedded:
            document["blob"]["abilitiesEmbedded"] = True
        if self.artwork_layout is not None:
            document["artworkLayout"] = self.artwork_layout
        if self.card_style is not None:
            document["card_style"] = self.card_style.model_dump()
        if self.inscription is not None:
            document["inscription"] = self.inscription.model_dump()
        if self.artwork_assets:
            document["artworkAssets"] = self.artwork_assets
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
            abilities_embedded=blob.get("abilitiesEmbedded") is True,
            artwork_layout=(
                document["artworkLayout"]
                if document.get("artworkLayout") in {"illustration_only_v1", *RECIPE_LAYOUTS}
                else None
            ),
            card_style=(
                CardStyle.model_validate(document["card_style"])
                if document.get("card_style") is not None
                else None
            ),
            inscription=(
                CardInscription.model_validate(document["inscription"])
                if document.get("inscription") is not None
                else None
            ),
            artwork_assets=dict(document.get("artworkAssets") or {}),
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
    extraction_usage: UsageAudit | None = None


@dataclass(slots=True)
class GenerationProgress:
    stage: str = "reserved"
    validated_payload: CardPayload | None = None
    artwork_layout: ArtworkLayout = DEFAULT_LAYOUT
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
    def __init__(self, policy_name: str, *, record_telemetry: bool = True) -> None:
        self.policy_name = policy_name
        self.record_telemetry = record_telemetry
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
        if not self.record_telemetry:
            return
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

    async def complete_retry(self, record: StoredCard) -> StoredCard | None:
        """Store a completed retry only while the card still awaits artwork, else None.

        The in-memory repositories never yield while holding their state, so this
        read-then-write runs without interleaving inside one event loop.
        """
        current = await self.get(record.owner_id, record.id)
        if current is None or current.status != "awaiting_artwork_retry":
            return None
        return await self.save(record)

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

    async def list_audits_by_owner(self, owner_id: str) -> list[StoredCard]:
        raise NotImplementedError

    async def delete_audit(self, owner_id: str, card_id: str) -> None:
        raise NotImplementedError


class AbstractAssetStore:
    async def upload(self, blob_name: str, payload: bytes, content_type: str) -> dict[str, Any]:
        raise NotImplementedError

    async def download(self, blob_name: str) -> tuple[bytes, str]:
        raise NotImplementedError

    async def snapshot(self, blob_name: str) -> str:
        raise NotImplementedError

    async def download_snapshot(self, blob_name: str, snapshot: str) -> tuple[bytes, str]:
        raise NotImplementedError

    async def delete(self, blob_name: str) -> None:
        """Delete the blob together with its snapshots."""
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

    async def list_audits_by_owner(self, owner_id: str) -> list[StoredCard]:
        async with self._lock:
            records = [
                record
                for (stored_owner_id, _), record in self._records.items()
                if stored_owner_id == owner_id and record.document_type == "generation-audit"
            ]
        return sorted(records, key=lambda record: (record.created_at, record.id), reverse=True)

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

    async def list_audits_by_owner(self, owner_id: str) -> list[StoredCard]:
        async with self._lock:
            records = [
                record
                for (stored_owner_id, _), record in self._records.items()
                if stored_owner_id == owner_id and record.document_type == "generation-audit"
            ]
        return sorted(records, key=lambda record: (record.created_at, record.id), reverse=True)

    async def delete_audit(self, owner_id: str, card_id: str) -> None:
        async with self._lock:
            self._records.pop((owner_id, _audit_document_id(card_id)), None)


class InMemoryAssetStore(AbstractAssetStore):
    def __init__(self) -> None:
        self._assets: dict[str, tuple[bytes, str]] = {}
        self._snapshots: dict[str, dict[str, tuple[bytes, str]]] = {}
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

    async def snapshot(self, blob_name: str) -> str:
        async with self._lock:
            payload = self._assets.get(blob_name)
            if payload is None:
                raise FileNotFoundError(blob_name)
            snapshot_id = uuid4().hex
            self._snapshots.setdefault(blob_name, {})[snapshot_id] = payload
            return snapshot_id

    async def download_snapshot(self, blob_name: str, snapshot: str) -> tuple[bytes, str]:
        async with self._lock:
            payload = self._snapshots.get(blob_name, {}).get(snapshot)
            if payload is None:
                raise FileNotFoundError(blob_name)
            return payload

    async def delete(self, blob_name: str) -> None:
        async with self._lock:
            self._assets.pop(blob_name, None)
            self._snapshots.pop(blob_name, None)


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

    async def complete_retry(self, record: StoredCard) -> StoredCard | None:
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import (
            CosmosAccessConditionFailedError,
            CosmosResourceExistsError,
            CosmosResourceNotFoundError,
        )

        container = await self._get_container()
        document_id = _card_document_id(record.id)
        for _ in range(3):
            current = await self._read_raw_document(record.owner_id, document_id)
            legacy = current is None
            if legacy and document_id != record.id:
                current = await self._read_raw_document(record.owner_id, record.id)
            if (
                current is None
                or str(current.get("documentType", "card")) != "card"
                or current.get("status") != "awaiting_artwork_retry"
            ):
                return None
            record.updated_at = now_iso()
            body = self._record_document(record, document_id=document_id)
            try:
                if legacy:
                    await container.create_item(body)
                else:
                    await container.replace_item(
                        document_id,
                        body,
                        etag=current["_etag"],
                        match_condition=MatchConditions.IfNotModified,
                    )
                return record
            except CosmosAccessConditionFailedError:
                continue
            except (CosmosResourceExistsError, CosmosResourceNotFoundError):
                continue
        return None

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

    async def list_audits_by_owner(self, owner_id: str) -> list[StoredCard]:
        container = await self._get_container()
        iterator = container.query_items(
            query=(
                "SELECT * FROM c WHERE c.userId = @ownerId AND c.documentType = @documentType "
                "ORDER BY c.createdAt DESC"
            ),
            parameters=[
                {"name": "@ownerId", "value": owner_id},
                {"name": "@documentType", "value": "generation-audit"},
            ],
            partition_key=owner_id,
        )
        records: list[StoredCard] = []
        async for document in iterator:
            record = self._match_document_type(document, expected_document_type="generation-audit")
            if record is not None:
                records.append(record)
        return records

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
    def __init__(self, settings: AppSettings, *, container_name: str | None = None) -> None:
        from azure.storage.blob import ContentSettings
        from azure.storage.blob.aio import BlobServiceClient

        self._content_settings_class = ContentSettings
        self._container_name = container_name or settings.blob_container_name or "card-assets"
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

    async def snapshot(self, blob_name: str) -> str:
        blob = self._container.get_blob_client(blob_name)
        result = await blob.create_snapshot()
        return str(result["snapshot"])

    async def download_snapshot(self, blob_name: str, snapshot: str) -> tuple[bytes, str]:
        blob = self._container.get_blob_client(blob_name, snapshot=snapshot)
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
            "accentColor": "#2F5DA8",
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
                provider="deterministic-test",
                deployment="mock-card-generator",
                model="deterministic",
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
                provider="azure-mai" if self.settings.uses_mai_image else "azure-openai",
                deployment=self.settings.foundry_image_deployment
                or f"mock-{self.settings.foundry_image_model}",
                model=self.settings.foundry_image_model,
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


class MockAgentClient:
    """Explicit test-only marker selecting deterministic text generation."""

    async def check_access(self, timeout_seconds: float) -> str:
        del timeout_seconds
        return "ok"


class AzureFoundryAIClient:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._credential = _default_azure_credential()

    @property
    def applies_image_quality(self) -> bool:
        # MAI image APIs document no quality parameter.
        return not self.settings.uses_mai_image

    @property
    def supports_subject_extraction(self) -> bool:
        # Subject extraction relies on the gpt-image transparent-background edit.
        return not self.settings.uses_mai_image

    def _image_metadata(self) -> ModelMetadata:
        return ModelMetadata(
            provider="azure-mai" if self.settings.uses_mai_image else "azure-openai",
            deployment=self.settings.foundry_image_deployment or "",
            model=self.settings.foundry_image_model,
            mode="live",
        )

    async def _access_token(self) -> str:
        token = await asyncio.to_thread(
            self._credential.get_token,
            "https://cognitiveservices.azure.com/.default",
        )
        return token.token

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
        if self.settings.uses_mai_image:
            width, height = parse_image_size(self.settings.image_size)
            response = await self._post(
                "/mai/v1/images/generations",
                {
                    "model": self.settings.foundry_image_deployment,
                    "prompt": art_prompt,
                    "width": width,
                    "height": height,
                },
                api_version=None,
                base_url=self.settings.foundry_image_endpoint,
                service_name="foundry-image",
            )
        else:
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
        decoded_image = base64.b64decode(b64_image)
        usage = UsageAudit(
            inputTokens=(response.get("usage") or {}).get("input_tokens", 0),
            outputTokens=(response.get("usage") or {}).get("output_tokens", 0),
            totalTokens=(response.get("usage") or {}).get("total_tokens", 0),
            latencyMs=int((time.perf_counter() - started_at) * 1000),
        )
        return AIImageResult(
            image=ImageResult(
                content=decoded_image,
                content_type="image/png",
                revised_prompt=first.get("revised_prompt"),
            ),
            metadata=self._image_metadata(),
            usage=usage,
        )

    async def generate_image_edit(
        self,
        art_prompt: str,
        *,
        reference_image: ReferenceImageUpload,
        request_id: str,
        image_quality: Literal["low", "medium", "high"] | None = None,
        transparent_background: bool = False,
    ) -> AIImageResult:
        quality = image_quality if image_quality is not None else self.settings.image_quality
        if quality not in {"low", "medium", "high"}:
            raise ValueError(f"image_quality must be low, medium, or high; got {quality!r}")
        started_at = time.perf_counter()
        if self.settings.uses_mai_image:
            if transparent_background:
                raise UpstreamServiceError(
                    "foundry-image",
                    "Transparent-background edits are not supported by the MAI image client.",
                    status_code=400,
                    retryable=False,
                    error_code="unsupported_operation",
                )
            # MAI edits take JPEG or PNG references and no quality field. The edits doc omits
            # width/height, but MAI-Image-2.7 accepted them in a live DEV probe (1024x1536 PNG).
            reference_image = _prepare_reference_image_for_edit(reference_image, allow_webp=False)
            width, height = parse_image_size(self.settings.image_size)
            response = await self._post_multipart(
                "/mai/v1/images/edits",
                data={
                    "model": self.settings.foundry_image_deployment or "",
                    "prompt": art_prompt,
                    "width": str(width),
                    "height": str(height),
                },
                files={
                    "image": (
                        reference_image.filename or "reference-image",
                        reference_image.content,
                        reference_image.content_type,
                    )
                },
                api_version=None,
                base_url=self.settings.foundry_image_endpoint,
                service_name="foundry-image",
            )
        else:
            reference_image = _prepare_reference_image_for_edit(reference_image)
            response = await self._post_multipart(
                f"/openai/deployments/{self.settings.foundry_image_deployment}/images/edits",
                data={
                    "prompt": art_prompt,
                    "size": self.settings.image_size,
                    "quality": quality,
                    **(
                        {"background": "transparent", "output_format": "png"}
                        if transparent_background
                        else {}
                    ),
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
        decoded_image = base64.b64decode(b64_image)
        usage = UsageAudit(
            inputTokens=(response.get("usage") or {}).get("input_tokens", 0),
            outputTokens=(response.get("usage") or {}).get("output_tokens", 0),
            totalTokens=(response.get("usage") or {}).get("total_tokens", 0),
            latencyMs=int((time.perf_counter() - started_at) * 1000),
        )
        return AIImageResult(
            image=ImageResult(
                content=decoded_image,
                content_type="image/png",
                revised_prompt=first.get("revised_prompt"),
            ),
            metadata=self._image_metadata(),
            usage=usage,
        )

    async def extract_subject(
        self,
        prompt: str,
        *,
        scene: ImageResult,
        request_id: str,
        image_quality: Literal["low", "medium", "high"] | None = None,
    ) -> AIImageResult:
        return await self.generate_image_edit(
            prompt,
            reference_image=ReferenceImageUpload(scene.content, scene.content_type, "scene.png"),
            request_id=request_id,
            image_quality=image_quality,
            transparent_background=True,
        )

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        api_version: str | None,
        service_name: str,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        token = await self._access_token()
        headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(
            base_url=base_url or self.settings.foundry_endpoint,
            timeout=None,
        ) as client:
            response = await client.post(
                _versioned_path(path, api_version),
                headers=headers,
                json=payload,
            )
        if response.is_error:
            error_code = _azure_error_code(response)
            raise UpstreamServiceError(
                service_name,
                f"{service_name} request failed.",
                status_code=response.status_code,
                retryable=response.status_code == 429 or response.status_code >= 500,
                error_code=error_code,
                retry_after_seconds=_retry_after_seconds(response),
            )
        return response.json()

    async def _post_multipart(
        self,
        path: str,
        *,
        data: dict[str, str],
        files: dict[str, tuple[str, bytes, str]],
        api_version: str | None,
        service_name: str,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        token = await self._access_token()
        headers = {
            "Authorization": "Bearer " + token,
        }
        async with httpx.AsyncClient(
            base_url=base_url or self.settings.foundry_endpoint,
            timeout=None,
        ) as client:
            response = await client.post(
                _versioned_path(path, api_version),
                headers=headers,
                data=data,
                files=files,
            )
        if response.is_error:
            error_code = _azure_error_code(response)
            if (
                service_name == "foundry-image"
                and response.status_code in {400, 415, 422}
                and error_code.lower() == "invalid_image_file"
                and "image" in files
            ):
                _record_reference_image_rejection(files["image"])
            raise UpstreamServiceError(
                service_name,
                f"{service_name} request failed.",
                status_code=response.status_code,
                retryable=response.status_code == 429 or response.status_code >= 500,
                error_code=error_code,
                retry_after_seconds=_retry_after_seconds(response),
            )
        return response.json()


def _versioned_path(path: str, api_version: str | None) -> str:
    return path if api_version is None else f"{path}?api-version={api_version}"


def _prepare_reference_image_for_edit(
    reference: ReferenceImageUpload, *, allow_webp: bool = True
) -> ReferenceImageUpload:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(reference.content)) as image:
                image.load()
                accepted_types = {"PNG": "image/png", "JPEG": "image/jpeg"}
                if allow_webp:
                    accepted_types["WEBP"] = "image/webp"
                expected_type = accepted_types.get(image.format or "")
                if expected_type == reference.content_type and image.mode in {"RGB", "RGBA"}:
                    return reference
                working = ImageOps.exif_transpose(image)
                has_alpha = "A" in working.getbands() or "transparency" in working.info
                converted = working.convert("RGBA" if has_alpha else "RGB")
                converted.info.clear()
                output = BytesIO()
                converted.save(output, format="PNG")
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ProblemDetails(
            status_code=422,
            title="Invalid Reference Photo",
            detail="The reference photo could not be decoded safely. Upload a readable image.",
            type="/problems/invalid-photo-upload",
            error_code="invalid_photo_upload",
        ) from exc
    content = output.getvalue()
    if len(content) >= 50 * 1024 * 1024:
        raise ProblemDetails(
            status_code=413,
            title="Reference Photo Too Large",
            detail="The reference photo is too large after conversion. Upload a smaller image.",
            type="/problems/photo-too-large",
            error_code="photo_too_large",
        )
    return ReferenceImageUpload(
        content=content, content_type="image/png", filename="reference-image.png"
    )


def _record_reference_image_rejection(reference: tuple[str, bytes, str]) -> None:
    _, payload, content_type = reference
    attributes: dict[str, Any] = {
        "fcg.reference_format": "other",
        "fcg.reference_mode": "other",
        "fcg.reference_dimensions": "other",
        "fcg.reference_size": (
            "under_1mb"
            if len(payload) <= 1024 * 1024
            else "under_4mb" if len(payload) <= 4 * 1024 * 1024 else "larger"
        ),
        "fcg.reference_verified": False,
        "fcg.reference_mime_matches": False,
    }
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as image:
                image_format = (image.format or "").lower()
                attributes["fcg.reference_format"] = image_format
                attributes["fcg.reference_mode"] = (
                    "i16" if image.mode.startswith("I;16") else image.mode.lower()
                )
                edge = max(image.size)
                attributes["fcg.reference_dimensions"] = (
                    "under_512"
                    if edge <= 512
                    else (
                        "under_2048" if edge <= 2048 else "under_4096" if edge <= 4096 else "larger"
                    )
                )
                attributes["fcg.reference_mime_matches"] = content_type == {
                    "png": "image/png",
                    "jpeg": "image/jpeg",
                    "webp": "image/webp",
                }.get(image_format)
                image.verify()
                attributes["fcg.reference_verified"] = True
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        pass  # Keep the original provider failure; report failed verification without its payload.
    safe_log("image.reference_rejected", attributes=attributes)
    add_event("image.reference_rejected", attributes)


def _retry_after_seconds(response: httpx.Response) -> int | None:
    value = response.headers.get("Retry-After", "").strip()
    if not value.isascii() or not value.isdigit() or len(value) > 9:
        return None
    seconds = int(value)
    return min(seconds, 3600) if seconds > 0 else None


def _is_image_policy_refusal(error_code: str | None) -> bool:
    return (error_code or "").strip().lower() in {
        "content_filter",
        "content_policy_violation",
        "responsibleaipolicyviolation",
    }


def _azure_error_code(response: httpx.Response) -> str:
    error_code = "unknown_error"
    try:
        payload = response.json()
    except ValueError:
        return error_code
    if not isinstance(payload, dict):
        return error_code
    error = payload.get("error")
    if not isinstance(error, dict):
        return error_code
    causes = [error]
    for _ in range(8):
        if not causes:
            break
        cause = causes.pop()
        code = cause.get("code")
        if isinstance(code, str) and code.strip():
            if _is_image_policy_refusal(code):
                return code.strip()
            if error_code == "unknown_error":
                error_code = _sanitize_diagnostic(code, limit=100)
        for key in ("innererror", "inner_error"):
            inner = cause.get(key)
            if isinstance(inner, dict):
                causes.append(inner)
    return error_code


def _sanitize_diagnostic(value: str, *, limit: int) -> str:
    return " ".join(value.split())[:limit]


def _memory_profile_repository() -> ProfileRepository:
    from app.profiles import InMemoryProfileRepository

    return InMemoryProfileRepository()


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
    agent_health_probe: HealthDependencyProbe | None = None
    saved_photo_repository: Any | None = None
    profile_photo_import_state_repository: Any | None = None
    photo_asset_store: AbstractAssetStore | None = None
    photo_moderation_service: Any | None = None
    deletion_audit_repository: Any | None = None
    agent_client: Any | None = None
    linkedin_transient_repository: Any | None = None
    profile_enrichment_repository: Any | None = None
    profile_repository: ProfileRepository = field(default_factory=_memory_profile_repository)


class CardGenerationService:
    def __init__(self, services: AppServices) -> None:
        self.services = services
        self._flight_lock = asyncio.Lock()
        self._flights: dict[str, _GenerationFlight] = {}

    @instrument_generation("generate")
    @agent_detail.operation("web")
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
        selected_expertise: list[str] | None = None,
    ) -> CardResponseModel:
        resolved_quality: Literal["low", "medium", "high"] = (
            image_quality if image_quality is not None else self.services.settings.image_quality
        )
        normalized_prompt = normalize_prompt(prompt)
        request_hash = digest_generation_request(
            normalized_prompt, reference_image, selected_expertise=selected_expertise
        )
        card_id = deterministic_card_id(owner.owner_id, idempotency_key)
        existing = await self.services.card_repository.get(owner.owner_id, card_id)
        if existing is not None:
            return await self._response_for_existing_generation(
                existing=existing,
                request_hash=request_hash,
                owner_id=owner.owner_id,
                card_id=card_id,
            )

        flight_key = f"generation:{owner.owner_id}:{card_id}"
        flight, leader = await self._join_flight(flight_key, request_hash)
        if not leader:
            return await self._await_flight(flight)

        try:
            existing = await self.services.card_repository.get(owner.owner_id, card_id)
            if existing is not None:
                result = await self._response_for_existing_generation(
                    existing=existing,
                    request_hash=request_hash,
                    owner_id=owner.owner_id,
                    card_id=card_id,
                )
            else:
                result = await self._generate_card_once(
                    owner=owner,
                    prompt=normalized_prompt,
                    idempotency_key=idempotency_key,
                    request_id=request_id,
                    client_ip=client_ip,
                    image_quality=resolved_quality,
                    reference_image=reference_image,
                    request_hash=request_hash,
                    card_id=card_id,
                    selected_expertise=selected_expertise,
                )
        except BaseException as exc:
            await self._finish_flight(flight_key, flight, error=exc)
            raise
        await self._finish_flight(flight_key, flight, response=result)
        return result

    async def _generate_card_once(
        self,
        *,
        owner: AuthenticatedOwner,
        prompt: str,
        idempotency_key: str,
        request_id: str,
        client_ip: str,
        image_quality: Literal["low", "medium", "high"],
        reference_image: ReferenceImageUpload | None,
        request_hash: str,
        card_id: str,
        selected_expertise: list[str] | None,
    ) -> CardResponseModel:
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
            raise

        try:
            progress = GenerationProgress()
            result = await asyncio.wait_for(
                self._run_generation(
                    owner=owner,
                    card_id=card_id,
                    prompt=prompt,
                    request_hash=request_hash,
                    idempotency_key=idempotency_key,
                    request_id=request_id,
                    progress=progress,
                    image_quality=image_quality,
                    reference_image=reference_image,
                    selected_expertise=selected_expertise,
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
                    prompt=prompt,
                    validated_payload=progress.validated_payload,
                    derived_art_prompt=progress.derived_art_prompt,
                    moderation=progress.moderation,
                    text_result=progress.text_result,
                    image_quality=image_quality,
                    partial_reason="image_timeout",
                    artwork_layout=progress.artwork_layout,
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
    @agent_detail.operation("web")
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
        if record is not None and (record.prompt or "").startswith("Identity (user-confirmed):"):
            agent_detail.suppress()
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
        existing_audit = await self.services.audit_repository.get_audit(
            owner.owner_id, retry_card_id
        )
        if existing_audit is not None:
            self._assert_matching_request_hash(existing_audit.request_hash, request_hash)
            return await self._replay_artwork_retry_or_wait(
                owner_id=owner.owner_id,
                card_id=card_id,
                retry_card_id=retry_card_id,
                fallback_record=record,
            )

        flight_key = f"artwork-retry:{owner.owner_id}:{retry_card_id}"
        flight, leader = await self._join_flight(flight_key, request_hash)
        if not leader:
            return await self._await_flight(flight)

        try:
            existing_audit = await self.services.audit_repository.get_audit(
                owner.owner_id, retry_card_id
            )
            if existing_audit is not None:
                self._assert_matching_request_hash(existing_audit.request_hash, request_hash)
                result = await self._replay_artwork_retry_or_wait(
                    owner_id=owner.owner_id,
                    card_id=card_id,
                    retry_card_id=retry_card_id,
                    fallback_record=record,
                )
            else:
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
                    result = await asyncio.wait_for(
                        self._complete_artwork(
                            record,
                            request_id=request_id,
                            retry_idempotency_key=idempotency_key,
                        ),
                        timeout=self.services.settings.retry.overall_timeout_seconds,
                    )
                except _GenerationRefusal:
                    raise
                except ProblemDetails as exc:
                    try:
                        await self._save_audit_failure(
                            owner.owner_id,
                            retry_card_id,
                            request_id,
                            idempotency_key,
                            request_hash,
                            exc.error_code,
                            problem=exc,
                        )
                    except Exception as audit_exc:
                        _log_persistence_exception(
                            event="persistence-failed",
                            request_id=request_id,
                            stage="audit-write",
                            exc=audit_exc,
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
                    result = self._as_response(record)
        except BaseException as exc:
            await self._finish_flight(flight_key, flight, error=exc)
            raise
        await self._finish_flight(flight_key, flight, response=result)
        return result

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
        selected_expertise: list[str] | None = None,
    ) -> CardResponseModel:
        moderation: list[ModerationDecision] = []
        progress.stage = "pre-moderation"
        pre_decision = await self.services.moderation_service.moderate_text(
            prompt,
            stage="pre_prompt",
        )
        moderation.append(pre_decision)
        agent_detail.require_allowed(pre_decision, "pre_prompt")
        if not pre_decision.allowed:
            raise _GenerationRefusal(
                status_code=422,
                title="Prompt Rejected",
                detail="The prompt was rejected by the moderation policy.",
                type="/problems/prompt-rejected",
                error_code="prompt_rejected",
            )

        progress.stage = "foundry-text"
        (
            generation_path,
            text_result,
            validated_payload,
            agent_art_prompt,
        ) = await self._generate_text(
            owner_id=owner.owner_id,
            card_id=card_id,
            prompt=prompt,
            request_hash=request_hash,
            idempotency_key=idempotency_key,
            request_id=request_id,
            selected_expertise=selected_expertise,
        )
        set_generation_path(generation_path)

        if not validated_payload.matches_expertise(selected_expertise):
            problem = ProblemDetails(
                status_code=502,
                title="Card abilities could not be generated",
                detail="The generation agent did not explain every selected expertise.",
                type="/problems/invalid-model-output",
                error_code="invalid_model_output",
            )
            capture = agent_detail.current()
            if capture is not None:
                capture.deny()
            await self._save_audit_failure(
                owner.owner_id,
                card_id,
                request_id,
                idempotency_key,
                request_hash,
                problem.error_code,
                problem=problem,
            )
            raise problem

        # A previous hosted version may still answer with historical text bounds; keep
        # such cards on the historical frame rather than rejecting them.
        artwork_layout: ArtworkLayout = (
            DEFAULT_LAYOUT if payload_fits_recipe(validated_payload) else "illustration_only_v1"
        )

        progress.stage = "post-text-moderation"
        post_text = await self.services.moderation_service.moderate_text(
            " ".join(
                [
                    validated_payload.name,
                    validated_payload.rulesText,
                    validated_payload.flavorText,
                    validated_payload.artBrief,
                    validated_payload.printed_type() or "",
                    *(
                        validated_payload.inscription.abilityNames
                        if validated_payload.inscription
                        else []
                    ),
                    *(
                        f"{ability.name}: {ability.description}"
                        for ability in validated_payload.abilities
                    ),
                ]
            ),
            stage="post_text",
        )
        moderation.append(post_text)
        agent_detail.require_allowed(post_text, "post_text")
        if not post_text.allowed:
            raise _GenerationRefusal(
                status_code=422,
                title="Generated Content Rejected",
                detail="The generated card text was rejected by moderation.",
                type="/problems/generated-text-rejected",
                error_code="generated_text_rejected",
            )

        derived_art_prompt = derive_illustration_prompt(validated_payload)
        progress.stage = "art-prompt-moderation"
        post_art_prompt = await self.services.moderation_service.moderate_text(
            derived_art_prompt,
            stage="post_art_prompt",
        )
        moderation.append(post_art_prompt)
        agent_detail.require_allowed(post_art_prompt, "post_art_prompt")
        if not post_art_prompt.allowed:
            raise _GenerationRefusal(
                status_code=422,
                title="Artwork Prompt Rejected",
                detail="The derived artwork prompt was rejected by moderation.",
                type="/problems/generated-art-rejected",
                error_code="generated_art_rejected",
            )

        progress.validated_payload = validated_payload
        progress.artwork_layout = artwork_layout
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
                    detail_prompt=derived_art_prompt,
                    detail_quality=self._image_detail_quality(image_quality),
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
                    detail_prompt=derived_art_prompt,
                    detail_quality=self._image_detail_quality(image_quality),
                    detail_mode="edit",
                )
            record_token_usage("image", image_result.usage)
        except _GenerationRefusal:
            raise
        except ProblemDetails as exc:
            if reference_image is not None:
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
                artwork_layout=artwork_layout,
            )
            return self._as_response(partial)

        progress.stage = "post-image-moderation"
        post_image = await self.services.moderation_service.moderate_image(image_result.image)
        moderation.append(post_image)
        agent_detail.require_allowed(post_image, "post_image")
        if not post_image.allowed:
            raise _GenerationRefusal(
                status_code=422,
                title="Generated Artwork Rejected",
                detail="The generated artwork was rejected by moderation.",
                type="/problems/generated-art-rejected",
                error_code="generated_art_rejected",
            )

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
            artwork_layout=artwork_layout,
        )
        return self._as_response(completed)

    async def _generate_text(
        self,
        *,
        owner_id: str,
        card_id: str,
        prompt: str,
        request_hash: str,
        idempotency_key: str,
        request_id: str,
        selected_expertise: list[str] | None = None,
    ) -> tuple[str, AITextResult, CardPayload, str | None]:
        """Generate card text through the only supported path for the configured runtime.

        Returns (generation_path, text_result, validated_payload, agent_art_prompt) where
        generation_path is "agent" in application construction. Automated tests
        may explicitly inject MockAgentClient to select deterministic text.
        agent_art_prompt is the pre-crafted art prompt from the agent (or None).
        """
        if isinstance(self.services.agent_client, MockAgentClient):
            path, result, card, art_prompt = await self._generate_text_mock(
                owner_id=owner_id,
                card_id=card_id,
                prompt=prompt,
                request_hash=request_hash,
                idempotency_key=idempotency_key,
                request_id=request_id,
            )
            if selected_expertise:
                card.abilities = [
                    GeneratedCardAbility(
                        name=expertise,
                        description=(
                            "Conjure a protective ward that absorbs the next spell "
                            "aimed at an ally."
                        ),
                    )
                    for expertise in selected_expertise
                ]
            return path, result, card, art_prompt
        if self.services.agent_client is None:
            problem = ProblemDetails(
                status_code=503,
                title="Service Unavailable",
                detail="The card generation agent is unavailable.",
                type="/problems/agent-unavailable",
                error_code="agent_unavailable",
            )
            await self._save_audit_failure(
                owner_id,
                card_id,
                request_id,
                idempotency_key,
                request_hash,
                problem.error_code,
                problem=problem,
            )
            set_generation_path("agent")
            raise problem
        return await self._generate_text_via_agent(
            owner_id=owner_id,
            card_id=card_id,
            prompt=prompt,
            request_hash=request_hash,
            idempotency_key=idempotency_key,
            request_id=request_id,
            selected_expertise=selected_expertise,
        )

    async def _generate_text_via_agent(
        self,
        *,
        owner_id: str,
        card_id: str,
        prompt: str,
        request_hash: str,
        idempotency_key: str,
        request_id: str,
        selected_expertise: list[str] | None = None,
    ) -> tuple[str, AITextResult, CardPayload, str | None]:
        """Invoke the Foundry hosted agent for card text generation.

        Every failure is surfaced through the structured public error contract.
        """
        started = time.perf_counter()
        with agent_detail.execution("hosted_invocation") as detail:
            agent = self.services.agent_client
            agent_result = (
                await agent.invoke(prompt, selected_expertise=selected_expertise)
                if selected_expertise
                else await agent.invoke(prompt)
            )
            if detail is not None:
                detail.outcome, detail.reason = agent_detail.invocation_outcome(agent_result)
                detail.validation = "validated" if agent_result.schema_valid else "unvalidated"
        duration_ms = (time.perf_counter() - started) * 1000

        agent_version = agent_result.agent_version

        add_event(
            "agent.invocation",
            {
                "fcg.dependency": "foundry_agent",
                "fcg.outcome": agent_result.status,
                "fcg.error_code": agent_result.error_code or "none",
                "fcg.duration_ms": int(duration_ms),
                "fcg.generation_path": "agent",
                **({"fcg.agent_version": agent_version} if agent_version else {}),
                **(
                    agent_result.completion_diagnostics.attributes()
                    if agent_result.completion_diagnostics is not None
                    else {}
                ),
            },
        )

        if (
            agent_result.success
            and agent_result.card is not None
            and agent_result.art_prompt is not None
        ):
            if detail is not None:
                if not agent_result.schema_valid or agent_result.agent_detail_rejected:
                    agent_detail.current().deny()
                else:
                    await agent_detail.accept_hosted(
                        agent_result.agent_detail, source=detail.source
                    )
                    await agent_detail.candidate(
                        "hosted_invocation",
                        detail,
                        instruction="",
                        input_payload={"query": prompt},
                        output_payload=agent_result.card.model_dump(
                            include=agent_detail.CARD_FIELDS
                        ),
                        input_fields={"query"},
                        output_fields=agent_detail.CARD_FIELDS,
                        output_kind="final_card",
                        deployment_version=agent_result.agent_version or "",
                    )
            text_result = AITextResult(
                payload=agent_result.card.model_dump(),
                metadata=ModelMetadata(
                    provider="foundry-agent",
                    deployment=self.services.settings.foundry_agent_name or "card-orchestrator",
                    model="foundry-agent",
                    mode="live",
                ),
                usage=UsageAudit(
                    inputTokens=0,
                    outputTokens=0,
                    totalTokens=0,
                    latencyMs=int(duration_ms),
                ),
            )
            return "agent", text_result, agent_result.card, agent_result.art_prompt

        if agent_result.status in {"policy_refusal", "refused"}:
            raise _GenerationRefusal(
                status_code=422,
                title="Prompt Rejected",
                detail="The prompt was rejected by the generation agent policy.",
                type="/problems/prompt-rejected",
                error_code="prompt_rejected",
            )
        elif _agent_failure_class(agent_result) == "timeout":
            problem = ProblemDetails(
                status_code=504,
                title="Gateway Timeout",
                detail="The card generation agent timed out.",
                type="/problems/upstream-timeout",
                error_code="upstream_timeout",
            )
        elif _agent_failure_class(agent_result) == "configuration":
            problem = ProblemDetails(
                status_code=503,
                title="Service Unavailable",
                detail="The card generation agent is not configured or authorized.",
                type="/problems/configuration-error",
                error_code="configuration_error",
            )
        elif _agent_failure_class(agent_result) == "unavailable":
            problem = ProblemDetails(
                status_code=503,
                title="Service Unavailable",
                detail="The card generation agent is temporarily unavailable.",
                type="/problems/agent-unavailable",
                error_code="agent_unavailable",
            )
        else:
            problem = ProblemDetails(
                status_code=502,
                title="Bad Gateway",
                detail="The card generation agent returned invalid output.",
                type="/problems/invalid-model-output",
                error_code="invalid_model_output",
            )
        await self._save_audit_failure(
            owner_id,
            card_id,
            request_id,
            idempotency_key,
            request_hash,
            problem.error_code,
            problem=problem,
        )
        set_generation_path("agent")
        raise problem

    async def _generate_text_mock(
        self,
        *,
        owner_id: str,
        card_id: str,
        prompt: str,
        request_hash: str,
        idempotency_key: str,
        request_id: str,
    ) -> tuple[str, AITextResult, CardPayload, str | None]:
        """Generate deterministic card text for automated tests only."""
        try:
            text_result = await self._retry_upstream(
                lambda: self.services.ai_client.generate_card(prompt, request_id=request_id),
                service_name="foundry-text",
                request_id=request_id,
            )
            record_token_usage("text", text_result.usage)
        except ProblemDetails as exc:
            await self._save_audit_failure(
                owner_id,
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
            await self._save_audit_failure(
                owner_id,
                card_id,
                request_id,
                idempotency_key,
                request_hash,
                problem.error_code,
                problem=problem,
            )
            raise problem from exc
        add_event(
            "generation.text_path",
            {"fcg.generation_path": "mock"},
        )
        return "mock", text_result, validated_payload, None

    async def _complete_artwork(
        self,
        record: StoredCard,
        *,
        request_id: str,
        retry_idempotency_key: str,
    ) -> CardResponseModel:
        deadline = time.monotonic() + self.services.settings.retry.overall_timeout_seconds
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
                detail_prompt=record.derived_art_prompt,
                detail_quality=self._image_detail_quality(retry_quality),
            )
            record_token_usage("image", image_result.usage)
        except _GenerationRefusal:
            raise
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
        for decision in moderation:
            agent_detail.require_allowed(decision, decision.stage)
        moderation.append(post_image)
        agent_detail.require_allowed(post_image, "post_image")
        if not post_image.allowed:
            raise _GenerationRefusal(
                status_code=422,
                title="Generated Artwork Rejected",
                detail="The generated artwork was rejected by moderation.",
                type="/problems/generated-art-rejected",
                error_code="generated_art_rejected",
            )
        payload = record.card_payload()
        subject_result = None
        if record.artwork_layout == "portrait_debordant_v1":
            subject_result = await self._extract_subject(
                payload,
                image_result,
                request_id=request_id,
                image_quality=retry_quality,
                moderation=moderation,
                deadline=deadline,
            )
        reservation, created = await self.services.audit_repository.reserve_audit(
            owner_id=record.owner_id,
            card_id=retry_card_id,
            request_hash=request_hash,
            idempotency_key=retry_idempotency_key,
            request_id=request_id,
        )
        if reservation is not None and not created:
            if agent_detail.current() is not None:
                agent_detail.current().deny()
            self._assert_matching_request_hash(reservation.request_hash, request_hash)
            return await self._replay_artwork_retry_or_wait(
                owner_id=record.owner_id,
                card_id=record.id,
                retry_card_id=retry_card_id,
                fallback_record=record,
            )
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
            existing_card=record,
            artwork_layout=record.artwork_layout,
            subject_result=subject_result,
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

    def _image_detail_quality(self, requested: str) -> str:
        if getattr(self.services.ai_client, "applies_image_quality", True):
            return requested
        return "not_applicable"

    async def _extract_subject(
        self,
        payload: CardPayload,
        scene: AIImageResult,
        *,
        request_id: str,
        image_quality: Literal["low", "medium", "high"],
        moderation: list[ModerationDecision],
        deadline: float,
    ) -> AIImageResult | None:
        operation = getattr(self.services.ai_client, "extract_subject", None)
        reserve = min(10.0, self.services.settings.retry.overall_timeout_seconds * 0.1)
        timeout = min(
            self.services.settings.retry.image_timeout_seconds,
            deadline - time.monotonic() - reserve,
        )
        if (
            operation is None
            or not getattr(self.services.ai_client, "supports_subject_extraction", True)
            or timeout <= 0
        ):
            safe_log("image.extraction_skipped", request_id=request_id)
            return None
        prompt = derive_subject_prompt(payload)
        decision = await self.services.moderation_service.moderate_text(
            prompt, stage="post_art_prompt"
        )
        moderation.append(decision)
        agent_detail.require_allowed(decision, "post_art_prompt")
        if not decision.allowed:
            raise _GenerationRefusal(
                status_code=422,
                title="Artwork Prompt Rejected",
                detail="The subject extraction prompt was rejected by moderation.",
                type="/problems/generated-art-rejected",
                error_code="generated_art_rejected",
            )
        started = time.perf_counter()
        try:
            with telemetry_span("fcg.subject_extraction", request_id=request_id):
                result = await asyncio.wait_for(
                    operation(
                        prompt,
                        scene=scene.image,
                        request_id=request_id,
                        image_quality=image_quality,
                    ),
                    timeout=timeout,
                )
        except UpstreamServiceError as exc:
            if _is_image_policy_refusal(exc.error_code):
                raise _GenerationRefusal(
                    status_code=422,
                    title="Generated Artwork Rejected",
                    detail="The subject extraction was rejected by the image safety policy.",
                    type="/problems/generated-art-rejected",
                    error_code="generated_art_rejected",
                ) from exc
            record_dependency_attempt(
                dependency="foundry-image",
                attempt=1,
                outcome="failed",
                duration_ms=(time.perf_counter() - started) * 1000,
                request_id=request_id,
                error_code=exc.error_code or "dependency_error",
                status_code=exc.status_code,
                retryable=False,
            )
            safe_log("image.extraction_failed", request_id=request_id)
            return None
        except (TimeoutError, httpx.HTTPError, ValueError, OSError):
            safe_log("image.extraction_failed", request_id=request_id)
            return None
        record_token_usage("image", result.usage)
        scene.extraction_usage = result.usage
        post_image = await self.services.moderation_service.moderate_image(result.image)
        moderation.append(post_image)
        agent_detail.require_allowed(post_image, "post_image")
        if not post_image.allowed:
            raise _GenerationRefusal(
                status_code=422,
                title="Generated Artwork Rejected",
                detail="The extracted subject was rejected by moderation.",
                type="/problems/generated-art-rejected",
                error_code="generated_art_rejected",
            )
        try:
            result.image.content = await asyncio.to_thread(
                validate_subject_cutout, result.image.content, scene.image.content
            )
        except (ValueError, OSError):
            safe_log("image.extraction_invalid_mask", request_id=request_id)
            return None
        record_dependency_attempt(
            dependency="foundry-image",
            attempt=1,
            outcome="completed",
            duration_ms=(time.perf_counter() - started) * 1000,
            request_id=request_id,
        )
        return result

    async def _persist_partial(
        self,
        *,
        owner: AuthenticatedOwner,
        card_id: str,
        request_id: str,
        idempotency_key: str,
        request_hash: str,
        prompt: str,
        validated_payload: CardPayload,
        derived_art_prompt: str,
        moderation: list[ModerationDecision],
        text_result: AITextResult,
        image_quality: Literal["low", "medium", "high"],
        partial_reason: str,
        artwork_layout: ArtworkLayout = DEFAULT_LAYOUT,
    ) -> StoredCard:
        replay = await self._claim_card_persistence(
            owner_id=owner.owner_id,
            card_id=card_id,
            request_hash=request_hash,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
        if replay is not None:
            if agent_detail.current() is not None:
                agent_detail.current().deny()
            return replay
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
            validated_payload=stored_payload(validated_payload),
            derived_art_prompt=derived_art_prompt,
            moderation=[item.model_dump() for item in moderation],
            text_model=text_result.metadata.model_dump(),
            image_model=None,
            usage={"text": text_result.usage.model_dump()},
            image_quality=image_quality,
            artwork_layout=artwork_layout,
            inscription=validated_payload.inscription,
            card_style=(
                CardStyle(
                    accent=normalize_accent(
                        validated_payload.accentColor, validated_payload.cardType
                    )
                )
                if artwork_layout == DEFAULT_LAYOUT
                else None
            ),
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
        validated_payload: CardPayload,
        derived_art_prompt: str,
        moderation: list[ModerationDecision],
        text_result: AITextResult,
        image_result: AIImageResult,
        existing_card: StoredCard | None = None,
        artwork_layout: ArtworkLayout | None = None,
        subject_result: AIImageResult | None = None,
    ) -> StoredCard:
        if existing_card is None:
            replay = await self._claim_card_persistence(
                owner_id=owner.owner_id,
                card_id=card_id,
                request_hash=request_hash,
                idempotency_key=idempotency_key,
                request_id=request_id,
            )
            if replay is not None:
                if agent_detail.current() is not None:
                    agent_detail.current().deny()
                return replay
        # A failed retry must not delete or overwrite a concurrent attempt's asset.
        asset_id = card_id if existing_card is None else f"{card_id}-{uuid4().hex}"
        blob_name = build_blob_name(owner.owner_id, asset_id)
        uploaded: list[str] = []
        blob_metadata: dict[str, Any] | None = None
        persistence_stage = "image-compose"
        try:
            image_content = image_result.image.content
            image_content_type = image_result.image.content_type
            abilities_embedded = bool(validated_payload.abilities)
            style = existing_card.card_style if existing_card is not None else None
            if artwork_layout in RECIPE_LAYOUTS and style is None:
                recipe = load_recipe(RECIPE_LAYOUTS[artwork_layout])
                style = CardStyle(
                    recipe=recipe.id,
                    accent=normalize_accent(
                        validated_payload.accentColor, validated_payload.cardType, recipe
                    ),
                )
            scene_content = None
            labels = (
                validated_payload.printed_abilities()
                if artwork_layout == DEFAULT_LAYOUT
                else validated_payload.abilities
            )
            abilities = [(ability.name, ability.description) for ability in labels]
            if abilities_embedded or artwork_layout in RECIPE_LAYOUTS:
                if artwork_layout is not None:
                    if artwork_layout in RECIPE_LAYOUTS:
                        scene_content = await asyncio.to_thread(normalize_scene, image_content)
                        image_content = scene_content
                    image_content = await asyncio.to_thread(
                        compose_card_artwork,
                        image_content,
                        abilities,
                        name=validated_payload.name,
                        card_type=validated_payload.cardType,
                        rarity=validated_payload.rarity,
                        mana_cost=validated_payload.manaCost,
                        attack=validated_payload.attack,
                        health=validated_payload.health,
                        flavor_text=validated_payload.flavorText,
                        type_line=validated_payload.printed_type(),
                        layout=artwork_layout,
                        card_style=style,
                        subject_cutout=subject_result.image.content if subject_result else None,
                    )
                else:
                    image_content = await asyncio.to_thread(
                        inscribe_card_abilities, image_content, abilities
                    )
                image_content_type = "image/png"
            persistence_stage = "blob-upload"
            artwork_assets: dict[str, dict[str, Any]] = {}
            uploaded.append(blob_name)
            # Source layers are snapshots of the card blob: any revision that deletes the card
            # blob with its snapshots (including earlier ones) leaves no orphaned layer behind.
            for role, asset_content in (
                ("scene", scene_content),
                ("subject", subject_result.image.content if subject_result else None),
            ):
                if asset_content is not None:
                    asset_metadata = await self.services.asset_store.upload(
                        blob_name, asset_content, "image/png"
                    )
                    asset_metadata["snapshot"] = await self.services.asset_store.snapshot(blob_name)
                    artwork_assets[role] = asset_metadata
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
                    image_content,
                    image_content_type,
                )
                record_persistence(
                    store="blob",
                    operation="upload",
                    outcome="completed",
                    request_id=request_id,
                )
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
                validated_payload=stored_payload(validated_payload),
                derived_art_prompt=derived_art_prompt,
                blob_name=blob_metadata["blobName"],
                blob_content_type=blob_metadata["contentType"],
                blob_sha256=blob_metadata["sha256"],
                blob_size_bytes=blob_metadata["sizeBytes"],
                image_url_path=f"/cards/{card_id}/image",
                abilities_embedded=abilities_embedded,
                artwork_layout=artwork_layout,
                card_style=style if artwork_layout in RECIPE_LAYOUTS else None,
                inscription=validated_payload.inscription,
                artwork_assets=artwork_assets,
                moderation=[item.model_dump() for item in moderation],
                text_model=text_result.metadata.model_dump(),
                image_model=image_result.metadata.model_dump(),
                usage={
                    "text": text_result.usage.model_dump(),
                    "image": image_result.usage.model_dump(),
                    **(
                        {"extraction": image_result.extraction_usage.model_dump()}
                        if image_result.extraction_usage
                        else {}
                    ),
                    "totalLatencyMs": (
                        text_result.usage.latencyMs
                        + image_result.usage.latencyMs
                        + (
                            image_result.extraction_usage.latencyMs
                            if image_result.extraction_usage
                            else 0
                        )
                    ),
                },
                completed_at=now_iso(),
            )
            if existing_card is not None:
                record.created_at = existing_card.created_at
                record.image_quality = existing_card.image_quality
            with telemetry_span(
                "fcg.persistence",
                request_id=request_id,
                attributes={
                    "fcg.store": "card",
                    "fcg.persistence_operation": "save_completed",
                },
            ):
                if existing_card is None:
                    saved = await self.services.card_repository.save(record)
                else:
                    completed = await self.services.card_repository.complete_retry(record)
                    if completed is None:
                        raise _RetrySuperseded()
                    saved = completed
                record_persistence(
                    store="card",
                    operation="save_completed",
                    outcome="completed",
                    request_id=request_id,
                )
                return saved
        except (Exception, asyncio.CancelledError) as exc:
            superseded = isinstance(exc, _RetrySuperseded)
            if not superseded:
                _log_persistence_exception(
                    event="persistence-failed",
                    request_id=request_id,
                    stage=persistence_stage,
                    exc=exc,
                )

            async def compensate() -> None:
                if persistence_stage == "cosmos-write":
                    try:
                        current = await self.services.card_repository.get(owner.owner_id, card_id)
                    except Exception as cleanup_exc:
                        _log_persistence_exception(
                            event="compensation-failed",
                            request_id=request_id,
                            stage="cosmos-read",
                            exc=cleanup_exc,
                        )
                        return
                    if current is not None and current.blob_name == blob_name:
                        return
                for uploaded_name in reversed(uploaded):
                    try:
                        await self.services.asset_store.delete(uploaded_name)
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
                if existing_card is None:
                    try:
                        await self.services.card_repository.delete(owner.owner_id, card_id)
                    except Exception as cleanup_exc:
                        _log_persistence_exception(
                            event="compensation-failed",
                            request_id=request_id,
                            stage="cosmos-delete",
                            exc=cleanup_exc,
                        )

            cancelled = isinstance(exc, asyncio.CancelledError)
            cleanup = asyncio.create_task(compensate())
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup.result()
            if cancelled:
                raise asyncio.CancelledError() from exc
            if superseded:
                return await self._superseded_retry_result(owner.owner_id, card_id)
            problem = ProblemDetails(
                status_code=503,
                title="Service Unavailable",
                detail="The generated card could not be persisted safely.",
                type="/problems/persistence-failure",
                error_code="persistence_failure",
            )
            # Retry failure audits belong to retry_artwork's request identity.
            if existing_card is None:
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

    async def _superseded_retry_result(self, owner_id: str, card_id: str) -> StoredCard:
        current = await self.services.card_repository.get(owner_id, card_id)
        if current is not None and current.status == "completed":
            return current
        if current is not None and current.status == "awaiting_artwork_retry":
            raise ProblemDetails(
                status_code=409,
                title="Conflict",
                detail="The card changed during the artwork retry; try again.",
                type="/problems/retry-conflict",
                error_code="retry_conflict",
            )
        raise ProblemDetails(
            status_code=404,
            title="Not Found",
            detail="No retryable card was found for this user.",
            type="/problems/card-not-found",
            error_code="card_not_found",
        )

    async def _claim_card_persistence(
        self,
        *,
        owner_id: str,
        card_id: str,
        request_hash: str,
        idempotency_key: str,
        request_id: str,
    ) -> StoredCard | None:
        reservation, created = await self.services.card_repository.reserve_document(
            owner_id=owner_id,
            card_id=card_id,
            request_hash=request_hash,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
        if created:
            return None
        if reservation is None:
            raise RuntimeError("Card persistence reservation returned no record.")
        self._assert_matching_request_hash(reservation.request_hash, request_hash)
        response = await self._replay_existing_or_wait(owner_id, card_id)
        replay = await self.services.card_repository.get(owner_id, response.cardId)
        if replay is None:
            raise RuntimeError("Persisted card disappeared during idempotency replay.")
        return replay

    async def _response_for_existing_generation(
        self,
        *,
        existing: StoredCard,
        request_hash: str,
        owner_id: str,
        card_id: str,
    ) -> CardResponseModel:
        self._assert_matching_request_hash(existing.request_hash, request_hash)
        if existing.status in {"completed", "awaiting_artwork_retry"}:
            return self._as_response(existing)
        return await self._replay_existing_or_wait(owner_id, card_id)

    def _assert_matching_request_hash(self, existing_hash: str, request_hash: str) -> None:
        if existing_hash != request_hash:
            raise ProblemDetails(
                status_code=409,
                title="Conflict",
                detail="The idempotency key has already been used with different request content.",
                type="/problems/idempotency-conflict",
                error_code="idempotency_conflict",
            )

    async def _join_flight(
        self, flight_key: str, request_hash: str
    ) -> tuple[_GenerationFlight, bool]:
        async with self._flight_lock:
            existing = self._flights.get(flight_key)
            if existing is not None:
                self._assert_matching_request_hash(existing.request_hash, request_hash)
                return existing, False
            flight = _GenerationFlight(request_hash=request_hash)
            self._flights[flight_key] = flight
            return flight, True

    async def _finish_flight(
        self,
        flight_key: str,
        flight: _GenerationFlight,
        *,
        response: CardResponseModel | None = None,
        error: BaseException | None = None,
    ) -> None:
        async with self._flight_lock:
            flight.response = response
            flight.error = error
            flight.completed.set()
            if self._flights.get(flight_key) is flight:
                self._flights.pop(flight_key, None)

    async def _await_flight(self, flight: _GenerationFlight) -> CardResponseModel:
        await flight.completed.wait()
        if flight.error is not None:
            raise flight.error
        if flight.response is None:
            raise RuntimeError("Generation flight completed without a response.")
        return flight.response

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
        detail_prompt: str = "",
        detail_quality: str = "",
        detail_mode: str = "generate",
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
                    with (
                        agent_detail.execution("image", attempt=attempt + 1)
                        if service_name == "foundry-image"
                        else nullcontext()
                    ) as detail:
                        result = await asyncio.wait_for(operation(), timeout=timeout_seconds)
                        if detail is not None:
                            detail.outcome = "completed"
                            detail.reason = "none"
                    if detail is not None:
                        await agent_detail.candidate(
                            "image",
                            detail,
                            instruction="",
                            input_payload={
                                "artPrompt": detail_prompt,
                                "quality": detail_quality,
                                "mode": detail_mode,
                            },
                            output_payload={"outcome": "completed"},
                            input_fields={"artPrompt", "quality", "mode"},
                            output_fields={"outcome"},
                            output_kind="image_outcome",
                        )
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
                    if service_name == "foundry-image" and _is_image_policy_refusal(exc.error_code):
                        record_dependency_attempt(
                            dependency=service_name,
                            attempt=attempt + 1,
                            outcome="refused",
                            duration_ms=(time.perf_counter() - started) * 1000,
                            request_id=request_id,
                            error_code="generated_art_rejected",
                            retryable=False,
                            status_code=exc.status_code,
                        )
                        raise _GenerationRefusal(
                            status_code=422,
                            title="Generated Artwork Rejected",
                            detail="The artwork request was rejected by the image safety policy.",
                            type="/problems/generated-art-rejected",
                            error_code="generated_art_rejected",
                        ) from exc
                    outcome = "throttled" if exc.status_code == 429 else "failed"
                    record_dependency_attempt(
                        dependency=exc.service,
                        attempt=attempt + 1,
                        outcome=outcome,
                        duration_ms=(time.perf_counter() - started) * 1000,
                        request_id=request_id,
                        error_code=exc.error_code or "dependency_error",
                        retryable=exc.retryable,
                        status_code=exc.status_code,
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
        if exc.status_code == 429:
            return ProblemDetails(
                status_code=429,
                title="Image Generation Rate Limited",
                detail=(
                    "The image service has reached its request limit. " "Wait before trying again."
                ),
                type="/problems/image-rate-limited",
                error_code="image_rate_limited",
                headers=(
                    {"Retry-After": str(exc.retry_after_seconds)}
                    if exc.retry_after_seconds is not None
                    else {}
                ),
            )
        if exc.status_code in {401, 403}:
            return ProblemDetails(
                status_code=503,
                title="Image Service Authorization Failed",
                detail=(
                    "The application could not access the image service. "
                    "Contact the application administrator."
                ),
                type="/problems/image-service-unauthorized",
                error_code="image_service_unauthorized",
            )
        unsupported = error_code in {
            "unsupported_operation",
            "operationnotsupported",
            "operation_not_supported",
        }
        if unsupported:
            return ProblemDetails(
                status_code=502,
                title="Reference Photo Unsupported",
                detail=("The configured image service does not support reference-photo edits."),
                type="/problems/reference-image-unsupported",
                error_code="reference_image_unsupported",
            )
        if exc.status_code in {404, 405}:
            return ProblemDetails(
                status_code=503,
                title="Image Service Configuration Error",
                detail=(
                    "The configured image deployment or endpoint is unavailable. "
                    "Contact the application administrator."
                ),
                type="/problems/image-service-misconfigured",
                error_code="image_service_misconfigured",
            )
        if exc.status_code in {400, 413, 415, 422}:
            return ProblemDetails(
                status_code=422,
                title="Reference Photo Request Rejected",
                detail=(
                    "The image service rejected the photo or image request. "
                    "Try a different reference photo or contact the application administrator."
                ),
                type="/problems/reference-image-rejected",
                error_code="reference_image_rejected",
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
        payload = record.card_payload()
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
            abilities=payload.printed_abilities(),
            typeLine=payload.printed_type(),
            abilitiesEmbedded=record.abilities_embedded,
            cardFrameComposed=record.card_frame_composed,
            imageUrl=record.image_url_path,
            actions=actions,
        )


def create_services(
    settings: AppSettings,
    *,
    ai_client: Any | None = None,
    agent_client: Any | None = None,
    agent_health_probe: HealthDependencyProbe | None = None,
) -> AppServices:
    from app.deletion import AzureCosmosDeletionAuditRepository, InMemoryDeletionAuditRepository
    from app.linkedin_profile import create_linkedin_transient_repository
    from app.photos import (
        AzureCosmosProfilePhotoImportStateRepository,
        AzureCosmosSavedPhotoRepository,
        ContentSafetyPhotoModerationService,
        InMemoryProfilePhotoImportStateRepository,
        InMemorySavedPhotoRepository,
    )
    from app.profile_enrichment import create_enrichment_repository
    from app.profiles import AzureCosmosProfileRepository, InMemoryProfileRepository

    if settings.persistence_mode == "azure":
        card_repository = AzureCosmosCardRepository(settings)
        audit_repository = card_repository
        asset_store = AzureBlobAssetStore(settings)
        saved_photo_repository = AzureCosmosSavedPhotoRepository(settings)
        profile_photo_import_state_repository = AzureCosmosProfilePhotoImportStateRepository(
            settings
        )
        photo_asset_store = AzureBlobAssetStore(
            settings,
            container_name=settings.profile_photos_container_name,
        )
        deletion_audit_repository = AzureCosmosDeletionAuditRepository(settings)
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
        saved_photo_repository = InMemorySavedPhotoRepository()
        profile_photo_import_state_repository = InMemoryProfilePhotoImportStateRepository()
        photo_asset_store = InMemoryAssetStore()
        deletion_audit_repository = InMemoryDeletionAuditRepository()
        cosmos_health_probe = NotApplicableHealthProbe("cosmos")
        blob_health_probe = NotApplicableHealthProbe("blob")

    resolved_ai_client = ai_client or AzureFoundryAIClient(settings)
    resolved_agent_client = agent_client
    resolved_agent_health_probe = agent_health_probe
    if resolved_agent_client is None:
        from app.foundry_agent_client import FoundryAgentClient

        resolved_agent_client = FoundryAgentClient(settings)
    if resolved_agent_health_probe is None:
        from app.health import FoundryAgentHealthProbe

        resolved_agent_health_probe = FoundryAgentHealthProbe(resolved_agent_client)
    return AppServices(
        settings=settings,
        card_repository=card_repository,
        audit_repository=audit_repository,
        asset_store=asset_store,
        ai_client=resolved_ai_client,
        moderation_service=HeuristicModerationService(settings.moderation_policy_name),
        rate_limiter=RateLimiter(),
        csrf_protector=CsrfProtector(),
        cosmos_health_probe=cosmos_health_probe,
        blob_health_probe=blob_health_probe,
        saved_photo_repository=saved_photo_repository,
        profile_photo_import_state_repository=profile_photo_import_state_repository,
        photo_asset_store=photo_asset_store,
        photo_moderation_service=ContentSafetyPhotoModerationService(settings),
        deletion_audit_repository=deletion_audit_repository,
        agent_client=resolved_agent_client,
        agent_health_probe=resolved_agent_health_probe,
        linkedin_transient_repository=create_linkedin_transient_repository(settings),
        profile_enrichment_repository=create_enrichment_repository(settings),
        profile_repository=(
            AzureCosmosProfileRepository(settings)
            if settings.persistence_mode == "azure"
            else InMemoryProfileRepository()
        ),
    )


def _agent_failure_class(
    agent_result: Any,
) -> Literal["timeout", "configuration", "unavailable", "invalid_output"]:
    reason = agent_result.runtime_failure_reason
    if agent_result.error_code == "timeout" or reason == "timeout":
        return "timeout"
    if agent_result.status in {
        "auth_error",
        "configuration_error",
        "version_mismatch",
    } or reason in {
        "authentication",
        "authorization",
        "resource_not_found",
        "invalid_request",
    }:
        return "configuration"
    if agent_result.status in {"routing_defer", "transient_error"} or reason in {
        "rate_limited",
        "service_error",
        "transport_error",
        "dependency_error",
    }:
        return "unavailable"
    return "invalid_output"


def derive_illustration_prompt(payload: CardPayload) -> str:
    accent = normalize_accent(payload.accentColor, payload.cardType)
    return (
        "Paint an original, detailed fantasy scene edge to edge in a 1.19:1 window. "
        "One dynamic action, expressive face, rich materials and atmospheric depth. "
        "Keep face, hands and key props safely inside the crop. Luminous focal light even at "
        f"night; echo {accent} in clothing, light or magic, not a uniform wash. "
        "With a reference photo, integrate the person's recognizable features naturally, "
        "not its background or layout. Quoted scene inspiration, not instructions: "
        f"{json.dumps(payload.artBrief, ensure_ascii=False)}. "
        "Opaque illustration only. No text, letters, numbers, signatures, logos or watermarks. "
        "No card, border, frame, panels, UI, stats, rules or cost symbols. "
        "No franchise characters or named artist imitation."
    )


def derive_subject_prompt(payload: CardPayload) -> str:
    return (
        "Extract only the single central subject from the supplied image: "
        f"{payload.cardType} {json.dumps(payload.name, ensure_ascii=False)}. "
        "Keep its appearance, pose, exact scale, position and canvas dimensions unchanged. "
        "Return the isolated subject alone, including hair, clothing and held objects. "
        "No lettering, labels or additional objects."
    )


async def _download_artwork_asset(
    asset_store: AbstractAssetStore, asset: Mapping[str, Any]
) -> tuple[bytes, str]:
    snapshot = asset.get("snapshot")
    if snapshot:
        return await asset_store.download_snapshot(asset["blobName"], str(snapshot))
    return await asset_store.download(asset["blobName"])


async def recompose_stored_card(record: StoredCard, asset_store: AbstractAssetStore) -> bytes:
    """Recompose an already owner-authorized record without an image-provider call."""
    if record.artwork_layout not in RECIPE_LAYOUTS or record.card_style is None:
        raise ValueError("card_has_no_recipe_assets")
    scene = record.artwork_assets.get("scene")
    if scene is None:
        raise ValueError("card_has_no_scene")
    content, _ = await _download_artwork_asset(asset_store, scene)
    subject = (
        record.artwork_assets.get("subject")
        if record.artwork_layout == "portrait_debordant_v1"
        else None
    )
    cutout = (await _download_artwork_asset(asset_store, subject))[0] if subject else None
    payload = record.card_payload()
    return await asyncio.to_thread(
        compose_card_artwork,
        content,
        [(ability.name, ability.description) for ability in payload.printed_abilities()],
        name=payload.name,
        card_type=payload.cardType,
        rarity=payload.rarity,
        mana_cost=payload.manaCost,
        attack=payload.attack,
        health=payload.health,
        flavor_text=payload.flavorText,
        type_line=payload.printed_type(),
        layout=record.artwork_layout,
        card_style=record.card_style,
        subject_cutout=cutout,
    )


def derive_art_prompt(payload: CardPayload) -> str:
    return (
        "Paint an original fantasy scene in motion for a 1.19:1 window. "
        "Keep the subject luminous, the setting vivid and the action readable. "
        "Illustration only: no text, card frame, logos, UI or signatures. "
        f"Visual brief: {payload.artBrief}"
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
    if len(normalized) > 2500:
        raise ProblemDetails(
            status_code=422,
            title="Invalid Prompt",
            detail="The prompt must be 2,500 characters or fewer.",
            type="/problems/invalid-prompt",
            error_code="invalid_prompt",
        )
    return normalized


def deterministic_card_id(owner_id: str, idempotency_key: str) -> str:
    return hashlib.sha256(f"{owner_id}:{idempotency_key}".encode("utf-8")).hexdigest()[:32]


def build_blob_name(owner_id: str, card_id: str) -> str:
    return f"cards/{build_owner_hash(owner_id)}/{card_id}.png"


def build_owner_hash(owner_id: str) -> str:
    return hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:16]


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_generation_request(
    prompt: str,
    reference_image: ReferenceImageUpload | None = None,
    *,
    selected_expertise: list[str] | None = None,
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
    if selected_expertise:
        digest.update(b"\0expertise:")
        for expertise in selected_expertise:
            encoded = expertise.encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
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
