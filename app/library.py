from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlparse

from app.generation import (
    AbstractCardRepository,
    AuthenticatedOwner,
    GeneratedCardModel,
    StoredCard,
)
from app.settings import AppSettings

CARD_LIBRARY_SAS_TTL_SECONDS = 5 * 60


class AbstractAssetUrlSigner(Protocol):
    async def sign_read_url(self, blob_name: str) -> str: ...


@dataclass(frozen=True, slots=True)
class LibraryCardSummary:
    card_id: str
    name: str
    card_type: str
    rarity: str
    status: str
    attack: int
    health: int
    mana_cost: int
    image_url: str | None
    created_at: str
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class LibraryCardDetail:
    card_id: str
    name: str
    card_type: str
    rarity: str
    status: str
    mana_cost: int
    attack: int
    health: int
    rules_text: str
    flavor_text: str
    art_brief: str
    prompt: str | None
    image_url: str | None
    created_at: str
    completed_at: str | None


class CardLibraryService:
    def __init__(
        self,
        card_repository: AbstractCardRepository,
        *,
        asset_url_signer: AbstractAssetUrlSigner | None = None,
    ) -> None:
        self._card_repository = card_repository
        self._asset_url_signer = asset_url_signer

    async def list_cards(self, owner: AuthenticatedOwner) -> list[LibraryCardSummary]:
        records = await self._card_repository.list_by_owner(owner.owner_id)
        visible_records = [record for record in records if record.validated_payload is not None]
        cards = await asyncio.gather(*(self._build_summary(record) for record in visible_records))
        return list(cards)

    async def get_card(
        self,
        owner: AuthenticatedOwner,
        card_id: str,
    ) -> LibraryCardDetail | None:
        record = await self._card_repository.get(owner.owner_id, card_id)
        if record is None or record.validated_payload is None:
            return None
        return await self._build_detail(record)

    async def _build_summary(self, record: StoredCard) -> LibraryCardSummary:
        payload = GeneratedCardModel.model_validate(record.validated_payload or {})
        return LibraryCardSummary(
            card_id=record.id,
            name=payload.name,
            card_type=payload.cardType,
            rarity=payload.rarity,
            status=record.status,
            attack=payload.attack,
            health=payload.health,
            mana_cost=payload.manaCost,
            image_url=await self._resolve_image_url(record),
            created_at=record.created_at,
            completed_at=record.completed_at,
        )

    async def _build_detail(self, record: StoredCard) -> LibraryCardDetail:
        payload = GeneratedCardModel.model_validate(record.validated_payload or {})
        return LibraryCardDetail(
            card_id=record.id,
            name=payload.name,
            card_type=payload.cardType,
            rarity=payload.rarity,
            status=record.status,
            mana_cost=payload.manaCost,
            attack=payload.attack,
            health=payload.health,
            rules_text=payload.rulesText,
            flavor_text=payload.flavorText,
            art_brief=payload.artBrief,
            prompt=record.prompt,
            image_url=await self._resolve_image_url(record),
            created_at=record.created_at,
            completed_at=record.completed_at,
        )

    async def _resolve_image_url(self, record: StoredCard) -> str | None:
        if record.status != "completed" or not record.blob_name:
            return None
        if self._asset_url_signer is None:
            return record.image_url_path
        return await self._asset_url_signer.sign_read_url(record.blob_name)


class AzureBlobSasUrlSigner:
    def __init__(
        self,
        settings: AppSettings,
        *,
        expiry_seconds: int = CARD_LIBRARY_SAS_TTL_SECONDS,
        service: object | None = None,
        permissions_class: object | None = None,
        generate_blob_sas_fn: object | None = None,
        now_fn: object | None = None,
    ) -> None:
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas
        from azure.storage.blob.aio import BlobServiceClient

        if not settings.blob_endpoint:
            raise ValueError("BLOB_ENDPOINT must be set to generate Blob SAS URLs.")

        if expiry_seconds < 1:
            raise ValueError("expiry_seconds must be positive.")

        parsed = urlparse(settings.blob_endpoint)
        account_name = parsed.netloc.split(".", 1)[0]
        if not account_name:
            raise ValueError("BLOB_ENDPOINT must include the storage account host name.")

        self._account_name = account_name
        self._container_name = settings.blob_container_name or "card-assets"
        self._expiry_seconds = expiry_seconds
        self._service = service or BlobServiceClient(
            settings.blob_endpoint,
            credential=_default_azure_credential(),
        )
        self._permissions_class = permissions_class or BlobSasPermissions
        self._generate_blob_sas = generate_blob_sas_fn or generate_blob_sas
        self._now = now_fn or _utcnow

    async def sign_read_url(self, blob_name: str) -> str:
        now = self._now()
        start = now - timedelta(minutes=1)
        expiry = now + timedelta(seconds=self._expiry_seconds)
        delegation_key = await self._service.get_user_delegation_key(
            key_start_time=start,
            key_expiry_time=expiry,
        )
        sas_token = self._generate_blob_sas(
            account_name=self._account_name,
            container_name=self._container_name,
            blob_name=blob_name,
            user_delegation_key=delegation_key,
            permission=self._permissions_class(read=True),
            start=start,
            expiry=expiry,
            protocol="https",
        )
        blob_url = self._service.get_blob_client(
            container=self._container_name,
            blob=blob_name,
        ).url
        return f"{blob_url}?{sas_token}"


def create_asset_url_signer(settings: AppSettings) -> AbstractAssetUrlSigner | None:
    if settings.persistence_mode != "azure":
        return None
    return AzureBlobSasUrlSigner(settings)


def _default_azure_credential():
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential(exclude_interactive_browser_credential=False)


def _utcnow() -> datetime:
    return datetime.now(UTC)
