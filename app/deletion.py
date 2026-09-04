from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from numbers import Real
from typing import Any, Literal

from app.generation import (
    AbstractAssetStore,
    AbstractAuditRepository,
    AbstractCardRepository,
    AuthenticatedOwner,
    StoredCard,
    _default_azure_credential,
    now_iso,
)
from app.photos import AbstractSavedPhotoRepository, StoredSavedPhoto
from app.problems import ProblemDetails
from app.settings import AppSettings
from app.telemetry import normalize_error_code, record_persistence, telemetry_span

DELETION_AUDIT_DOCUMENT_TYPE = "deletion-audit"
DELETION_AUDIT_DOCUMENT_ID_PREFIX = "deletion-audit:"


@dataclass(slots=True)
class DeletionAuditTimestamps:
    requested_at: str = field(default_factory=now_iso)
    deleted_at: str | None = None
    asset_cleanup_queued_at: str | None = None
    asset_cleanup_completed_at: str | None = None

    def to_document(self) -> dict[str, Any]:
        return {
            "requestedAt": self.requested_at,
            "deletedAt": self.deleted_at,
            "assetCleanupQueuedAt": self.asset_cleanup_queued_at,
            "assetCleanupCompletedAt": self.asset_cleanup_completed_at,
        }

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "DeletionAuditTimestamps":
        return cls(
            requested_at=str(document.get("requestedAt") or now_iso()),
            deleted_at=_optional_string(document.get("deletedAt")),
            asset_cleanup_queued_at=_optional_string(document.get("assetCleanupQueuedAt")),
            asset_cleanup_completed_at=_optional_string(document.get("assetCleanupCompletedAt")),
        )


@dataclass(slots=True)
class DeletionAuditRecord:
    audit_id: str
    owner_id: str
    request_id: str
    timestamps: DeletionAuditTimestamps
    moderation_outcome: str | None = None
    cost_estimate: float | None = None
    ttl_seconds: int = 30 * 24 * 60 * 60

    def to_document(self) -> dict[str, Any]:
        return {
            "id": self.audit_id,
            "documentType": DELETION_AUDIT_DOCUMENT_TYPE,
            "userId": self.owner_id,
            "schemaVersion": 1,
            "requestId": self.request_id,
            "timestamps": self.timestamps.to_document(),
            "moderationOutcome": self.moderation_outcome,
            "costEstimate": self.cost_estimate,
            "ttl": self.ttl_seconds,
        }

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "DeletionAuditRecord | None":
        if str(document.get("documentType")) != DELETION_AUDIT_DOCUMENT_TYPE:
            return None
        audit_id = document.get("id")
        owner_id = document.get("userId")
        request_id = document.get("requestId")
        if not isinstance(audit_id, str) or not audit_id:
            return None
        if not isinstance(owner_id, str) or not owner_id:
            return None
        if not isinstance(request_id, str) or not request_id:
            return None
        raw_cost = document.get("costEstimate")
        cost_estimate = (
            float(raw_cost)
            if isinstance(raw_cost, Real) and not isinstance(raw_cost, bool)
            else None
        )
        return cls(
            audit_id=audit_id,
            owner_id=owner_id,
            request_id=request_id,
            timestamps=DeletionAuditTimestamps.from_document(document.get("timestamps") or {}),
            moderation_outcome=_optional_string(document.get("moderationOutcome")),
            cost_estimate=cost_estimate,
            ttl_seconds=int(document.get("ttl") or 0),
        )


@dataclass(frozen=True, slots=True)
class CardDeletionFailure:
    card_id: str
    error_code: str
    detail: str


@dataclass(frozen=True, slots=True)
class BatchCardDeletionResult:
    deleted_card_ids: list[str]
    failures: list[CardDeletionFailure]

    @property
    def deleted_count(self) -> int:
        return len(self.deleted_card_ids)

    @property
    def failed_count(self) -> int:
        return len(self.failures)


@dataclass(frozen=True, slots=True)
class BlobDeletionTarget:
    store: Literal["card", "photo"]
    blob_name: str


class AbstractDeletionAuditRepository:
    async def save(self, record: DeletionAuditRecord) -> None:
        raise NotImplementedError

    async def list_by_owner(self, owner_id: str) -> list[DeletionAuditRecord]:
        raise NotImplementedError


class InMemoryDeletionAuditRepository(AbstractDeletionAuditRepository):
    def __init__(self) -> None:
        self._records: dict[tuple[str, str], DeletionAuditRecord] = {}
        self._lock = asyncio.Lock()

    async def save(self, record: DeletionAuditRecord) -> None:
        async with self._lock:
            self._records[(record.owner_id, record.audit_id)] = DeletionAuditRecord(
                audit_id=record.audit_id,
                owner_id=record.owner_id,
                request_id=record.request_id,
                timestamps=DeletionAuditTimestamps(
                    requested_at=record.timestamps.requested_at,
                    deleted_at=record.timestamps.deleted_at,
                    asset_cleanup_queued_at=record.timestamps.asset_cleanup_queued_at,
                    asset_cleanup_completed_at=record.timestamps.asset_cleanup_completed_at,
                ),
                moderation_outcome=record.moderation_outcome,
                cost_estimate=record.cost_estimate,
                ttl_seconds=record.ttl_seconds,
            )

    async def list_by_owner(self, owner_id: str) -> list[DeletionAuditRecord]:
        async with self._lock:
            records = [
                record
                for (stored_owner_id, _), record in self._records.items()
                if stored_owner_id == owner_id
            ]
        return sorted(records, key=lambda record: record.timestamps.requested_at, reverse=True)


class AzureCosmosDeletionAuditRepository(AbstractDeletionAuditRepository):
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

    async def save(self, record: DeletionAuditRecord) -> None:
        container = await self._get_container()
        await container.upsert_item(record.to_document())

    async def list_by_owner(self, owner_id: str) -> list[DeletionAuditRecord]:
        container = await self._get_container()
        iterator = container.query_items(
            query=(
                "SELECT * FROM c WHERE c.userId = @ownerId AND c.documentType = @documentType "
                "ORDER BY c.timestamps.requestedAt DESC"
            ),
            parameters=[
                {"name": "@ownerId", "value": owner_id},
                {"name": "@documentType", "value": DELETION_AUDIT_DOCUMENT_TYPE},
            ],
            partition_key=owner_id,
        )
        records: list[DeletionAuditRecord] = []
        async for document in iterator:
            record = DeletionAuditRecord.from_document(document)
            if record is not None:
                records.append(record)
        return records


class DeletionService:
    def __init__(
        self,
        *,
        settings: AppSettings,
        card_repository: AbstractCardRepository,
        audit_repository: AbstractAuditRepository,
        deletion_audit_repository: AbstractDeletionAuditRepository,
        asset_store: AbstractAssetStore,
        saved_photo_repository: AbstractSavedPhotoRepository,
        photo_asset_store: AbstractAssetStore,
    ) -> None:
        self._settings = settings
        self._card_repository = card_repository
        self._audit_repository = audit_repository
        self._deletion_audit_repository = deletion_audit_repository
        self._asset_store = asset_store
        self._saved_photo_repository = saved_photo_repository
        self._photo_asset_store = photo_asset_store
        self._audit_ttl_seconds = settings.audit_retention_days * 24 * 60 * 60

    async def delete_card(
        self,
        *,
        owner: AuthenticatedOwner,
        card_id: str,
        request_id: str,
        schedule_cleanup,
    ) -> None:
        record = await self._card_repository.get(owner.owner_id, card_id)
        if record is None:
            raise ProblemDetails(
                status_code=404,
                title="Not Found",
                detail="No card was found for this user.",
                type="/problems/card-not-found",
                error_code="card_not_found",
            )

        audit = DeletionAuditRecord(
            audit_id=build_card_deletion_audit_id(card_id, request_id),
            owner_id=owner.owner_id,
            request_id=request_id,
            timestamps=DeletionAuditTimestamps(requested_at=now_iso()),
            moderation_outcome=summarize_moderation_outcome([record]),
            cost_estimate=summarize_cost_estimate([record]),
            ttl_seconds=self._audit_ttl_seconds,
        )
        await self._deletion_audit_repository.save(audit)
        await self._card_repository.delete(owner.owner_id, card_id)
        await self._audit_repository.delete_audit(owner.owner_id, card_id)
        audit.timestamps.deleted_at = now_iso()
        blob_targets = self._card_blob_targets([record])
        if blob_targets:
            audit.timestamps.asset_cleanup_queued_at = now_iso()
        await self._deletion_audit_repository.save(audit)
        if blob_targets:
            schedule_cleanup(self._cleanup_assets, audit, blob_targets, request_id=request_id)

    async def delete_cards(
        self,
        *,
        owner: AuthenticatedOwner,
        card_ids: list[str],
        request_id: str,
        schedule_cleanup,
    ) -> BatchCardDeletionResult:
        unique_card_ids = list(dict.fromkeys(card_id for card_id in card_ids if card_id))
        deleted_card_ids: list[str] = []
        failures: list[CardDeletionFailure] = []
        for card_id in unique_card_ids:
            try:
                await self.delete_card(
                    owner=owner,
                    card_id=card_id,
                    request_id=request_id,
                    schedule_cleanup=schedule_cleanup,
                )
            except ProblemDetails as exc:
                failures.append(
                    CardDeletionFailure(
                        card_id=card_id,
                        error_code=exc.error_code,
                        detail=exc.detail,
                    )
                )
            except Exception as exc:
                failures.append(
                    CardDeletionFailure(
                        card_id=card_id,
                        error_code=normalize_error_code(type(exc).__name__),
                        detail="The card could not be deleted.",
                    )
                )
            else:
                deleted_card_ids.append(card_id)
        return BatchCardDeletionResult(deleted_card_ids=deleted_card_ids, failures=failures)

    async def delete_account(
        self,
        *,
        owner: AuthenticatedOwner,
        request_id: str,
        schedule_cleanup,
    ) -> None:
        cards = await self._card_repository.list_by_owner(owner.owner_id)
        saved_photos = await self._saved_photo_repository.list_by_owner(owner.owner_id)
        generation_audits = await self._audit_repository.list_audits_by_owner(owner.owner_id)

        audit = DeletionAuditRecord(
            audit_id=build_account_deletion_audit_id(request_id),
            owner_id=owner.owner_id,
            request_id=request_id,
            timestamps=DeletionAuditTimestamps(requested_at=now_iso()),
            moderation_outcome=summarize_moderation_outcome(cards),
            cost_estimate=summarize_cost_estimate(cards),
            ttl_seconds=self._audit_ttl_seconds,
        )
        await self._deletion_audit_repository.save(audit)

        for card in cards:
            await self._card_repository.delete(owner.owner_id, card.id)
        for generation_audit in generation_audits:
            await self._audit_repository.delete_audit(owner.owner_id, generation_audit.id)
        for photo in saved_photos:
            await self._saved_photo_repository.delete(owner.owner_id, photo.photo_id)

        audit.timestamps.deleted_at = now_iso()
        blob_targets = self._card_blob_targets(cards) + self._photo_blob_targets(saved_photos)
        if blob_targets:
            audit.timestamps.asset_cleanup_queued_at = now_iso()
        await self._deletion_audit_repository.save(audit)
        if blob_targets:
            schedule_cleanup(self._cleanup_assets, audit, blob_targets, request_id=request_id)

    async def _cleanup_assets(
        self,
        audit: DeletionAuditRecord,
        blob_targets: list[BlobDeletionTarget],
        *,
        request_id: str,
    ) -> None:
        had_failure = False
        for target in blob_targets:
            store = self._asset_store if target.store == "card" else self._photo_asset_store
            try:
                with telemetry_span(
                    "fcg.persistence",
                    request_id=request_id,
                    attributes={
                        "fcg.store": "blob",
                        "fcg.persistence_operation": "delete",
                    },
                ):
                    await store.delete(target.blob_name)
            except Exception as exc:
                if _is_not_found_exception(exc):
                    record_persistence(
                        store="blob",
                        operation="delete",
                        outcome="completed",
                        request_id=request_id,
                    )
                    continue
                record_persistence(
                    store="blob",
                    operation="delete",
                    outcome="failed",
                    request_id=request_id,
                    error_code=normalize_error_code(type(exc).__name__),
                )
                had_failure = True
                continue
            record_persistence(
                store="blob",
                operation="delete",
                outcome="completed",
                request_id=request_id,
            )

        if not had_failure:
            audit.timestamps.asset_cleanup_completed_at = now_iso()
        try:
            await self._deletion_audit_repository.save(audit)
        except Exception:
            return

    def _card_blob_targets(self, cards: list[StoredCard]) -> list[BlobDeletionTarget]:
        return [
            BlobDeletionTarget(store="card", blob_name=card.blob_name)
            for card in cards
            if isinstance(card.blob_name, str) and card.blob_name
        ]

    def _photo_blob_targets(self, photos: list[StoredSavedPhoto]) -> list[BlobDeletionTarget]:
        targets: list[BlobDeletionTarget] = []
        for photo in photos:
            if photo.blob_name:
                targets.append(BlobDeletionTarget(store="photo", blob_name=photo.blob_name))
            if photo.thumbnail_blob_name:
                targets.append(
                    BlobDeletionTarget(store="photo", blob_name=photo.thumbnail_blob_name)
                )
        return targets


def build_card_deletion_audit_id(card_id: str, request_id: str) -> str:
    return f"{DELETION_AUDIT_DOCUMENT_ID_PREFIX}card:{card_id}:{request_id}"


def build_account_deletion_audit_id(request_id: str) -> str:
    return f"{DELETION_AUDIT_DOCUMENT_ID_PREFIX}account:{request_id}"


def summarize_moderation_outcome(records: list[StoredCard]) -> str | None:
    outcomes = [_final_moderation_outcome(record) for record in records]
    unique = [value for value in dict.fromkeys(outcomes) if value]
    if not unique:
        return None
    return ", ".join(unique)


def summarize_cost_estimate(records: list[StoredCard]) -> float | None:
    total = 0.0
    found = False
    for record in records:
        value = _extract_cost_estimate(record.usage)
        if value is None:
            continue
        total += value
        found = True
    return round(total, 6) if found else None


def _final_moderation_outcome(record: StoredCard) -> str | None:
    for entry in reversed(record.moderation):
        if not isinstance(entry, dict):
            continue
        reason_code = _optional_string(entry.get("reasonCode"))
        if reason_code:
            return reason_code
    return None


def _extract_cost_estimate(value: Any) -> float | None:
    total = _walk_cost_values(value)
    if total is None:
        return None
    return float(total)


def _walk_cost_values(value: Any) -> float | None:
    if isinstance(value, Real) and not isinstance(value, bool):
        return None
    if not isinstance(value, dict):
        return None

    total = 0.0
    found = False
    for key, item in value.items():
        normalized_key = str(key).strip().lower().replace("_", "")
        if normalized_key in {"costestimate", "costestimateusd", "estimatedcost", "totalcostusd"}:
            if isinstance(item, Real) and not isinstance(item, bool):
                total += float(item)
                found = True
            continue
        nested = _walk_cost_values(item)
        if nested is not None:
            total += nested
            found = True
    return total if found else None


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _is_not_found_exception(exc: Exception) -> bool:
    if isinstance(exc, FileNotFoundError):
        return True
    return type(exc).__name__ == "ResourceNotFoundError"
