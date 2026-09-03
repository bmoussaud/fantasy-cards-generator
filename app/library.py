from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from app.generation import (
    AbstractCardRepository,
    AuthenticatedOwner,
    GeneratedCardModel,
    StoredCard,
)


def format_card_timestamp(timestamp: str | None) -> str | None:
    if timestamp is None:
        return None

    try:
        parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return timestamp

    if parsed_timestamp.tzinfo is None:
        return timestamp

    utc_timestamp = parsed_timestamp.astimezone(UTC)
    hour = utc_timestamp.strftime("%I").lstrip("0") or "0"
    return (
        f"{utc_timestamp.strftime('%b')} {utc_timestamp.day}, {utc_timestamp.year}, "
        f"{hour}:{utc_timestamp.strftime('%M')} {utc_timestamp.strftime('%p')} UTC"
    )


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
    created_at_display: str
    completed_at_display: str | None


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
    created_at_display: str
    completed_at_display: str | None


class CardLibraryService:
    def __init__(
        self,
        card_repository: AbstractCardRepository,
    ) -> None:
        self._card_repository = card_repository

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
            created_at_display=format_card_timestamp(record.created_at) or record.created_at,
            completed_at_display=format_card_timestamp(record.completed_at),
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
            created_at_display=format_card_timestamp(record.created_at) or record.created_at,
            completed_at_display=format_card_timestamp(record.completed_at),
        )

    async def _resolve_image_url(self, record: StoredCard) -> str | None:
        if record.status != "completed" or not record.blob_name:
            return None
        return record.image_url_path
