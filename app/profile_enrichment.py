from __future__ import annotations

import asyncio
import hashlib
import os
import time
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.auth import AuthenticatedUser, AuthSettings
from app.generation import AppSettings

SESSION_KEY = "profile_enrichment_session"
MAX_BYTES = 65_536
MAX_SECONDS = 120
EnrichmentPhase = Literal["pending", "working", "dispatched"]


class EnrichmentFailure(Exception):
    def __init__(self, status: str) -> None:
        self.status = status
        super().__init__("Profile enrichment could not complete.")


@dataclass(frozen=True)
class EnrichmentSettings:
    enabled: bool

    @classmethod
    def from_environment(cls) -> EnrichmentSettings:
        return cls(
            os.getenv("PROFILE_ENRICHMENT_ENABLED", "").lower() == "true",
        )

    def available(self, auth: AuthSettings) -> bool:
        return self.enabled and auth.is_configured


ShortText = Annotated[str, Field(max_length=120)]
Skill = Annotated[str, Field(max_length=80)]
Excerpt = Annotated[str, Field(max_length=500)]


class EnrichmentDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(max_length=80)
    title: ShortText = ""
    current_role: ShortText = ""
    expertise: list[Skill] = Field(default_factory=list, max_length=5)
    expertise_descriptions: list[Annotated[str, Field(max_length=1000)]] = Field(
        default_factory=list, max_length=5
    )
    post_excerpts: list[Excerpt] = Field(default_factory=list, max_length=3)


class EnrichmentTransaction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transaction_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    owner_id: str
    session_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    revision: str | None = Field(default=None, max_length=255)
    public_name: str = Field(min_length=1, max_length=80)
    public_context: str = Field(default="", max_length=200)
    draft: EnrichmentDraft
    expires_at: float
    phase: EnrichmentPhase = "pending"

    @property
    def document_id(self) -> str:
        return f"enrichment:{self.state_hash}"

    def document(self) -> dict[str, Any]:
        return {
            "id": self.document_id,
            "documentType": "profile-enrichment",
            "userId": self.owner_id,
            "transaction": self.model_dump(),
            "ttl": max(1, int(self.expires_at - time.time())),
        }


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def canonical_owner(user: AuthenticatedUser) -> str:
    try:
        tenant = str(UUID(user["tenant_id"] or ""))
        object_id = str(UUID(user["object_id"] or ""))
    except ValueError:
        raise EnrichmentFailure("identity_required") from None
    owner = f"{tenant}:{object_id}"
    if user["owner_id"] != owner:
        raise EnrichmentFailure("identity_required")
    return owner


class EnrichmentRepository:
    def __init__(self, container=None) -> None:
        self._container = container
        self._records: dict[tuple[str, str], EnrichmentTransaction] = {}
        self._lock = asyncio.Lock()

    async def create(self, record: EnrichmentTransaction) -> None:
        if self._container is not None:
            from azure.cosmos.exceptions import CosmosResourceExistsError

            try:
                await self._container.create_item(record.document())
            except CosmosResourceExistsError:
                raise EnrichmentFailure("busy") from None
            return
        async with self._lock:
            self._records = {
                key: value for key, value in self._records.items() if value.expires_at > time.time()
            }
            key = (record.owner_id, record.state_hash)
            if key in self._records:
                raise EnrichmentFailure("busy")
            self._records[key] = record.model_copy(deep=True)

    async def consume(
        self, owner_id: str, state: str, session_hash: str
    ) -> EnrichmentTransaction | None:
        return await self._transition(owner_id, digest(state), session_hash, "pending", "working")

    async def admit(self, record: EnrichmentTransaction) -> bool:
        return (
            await self._transition(
                record.owner_id,
                record.state_hash,
                record.session_hash,
                "working",
                "dispatched",
                transaction_id=record.transaction_id,
            )
        ) is not None

    async def _transition(
        self,
        owner_id: str,
        state_hash: str,
        session_hash: str,
        expected_phase: EnrichmentPhase,
        next_phase: EnrichmentPhase,
        transaction_id: str | None = None,
    ) -> EnrichmentTransaction | None:
        key = (owner_id, state_hash)
        if self._container is None:
            async with self._lock:
                record = self._records.get(key)
                if not self._matches(record, session_hash, expected_phase) or (
                    transaction_id is not None and record.transaction_id != transaction_id
                ):
                    return None
                assert record is not None
                self._records[key] = record.model_copy(update={"phase": next_phase})
                return record.model_copy(deep=True)
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import (
            CosmosAccessConditionFailedError,
            CosmosResourceNotFoundError,
        )

        try:
            document = await self._container.read_item(
                f"enrichment:{key[1]}", partition_key=owner_id
            )
            try:
                record = EnrichmentTransaction.model_validate(document["transaction"])
            except (KeyError, ValidationError):
                raise EnrichmentFailure("invalid_response") from None
            if (
                record.owner_id != owner_id
                or not self._matches(record, session_hash, expected_phase)
                or (transaction_id is not None and record.transaction_id != transaction_id)
            ):
                return None
            working = record.model_copy(update={"phase": next_phase})
            await self._container.replace_item(
                record.document_id,
                working.document(),
                etag=document["_etag"],
                match_condition=MatchConditions.IfNotModified,
            )
            return record
        except (CosmosAccessConditionFailedError, CosmosResourceNotFoundError):
            return None

    @staticmethod
    def _matches(
        record: EnrichmentTransaction | None,
        session_hash: str,
        expected_phase: EnrichmentPhase,
    ) -> bool:
        return bool(
            record
            and record.session_hash == session_hash
            and record.phase == expected_phase
            and record.expires_at > time.time()
        )

    async def finish(self, record: EnrichmentTransaction) -> bool:
        if self._container is None:
            async with self._lock:
                key = (record.owner_id, record.state_hash)
                current = self._records.get(key)
                if current is None or current.transaction_id != record.transaction_id:
                    return False
                self._records.pop(key)
                return bool(current and current.expires_at > time.time())
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import (
            CosmosAccessConditionFailedError,
            CosmosResourceNotFoundError,
        )

        try:
            document = await self._container.read_item(
                record.document_id, partition_key=record.owner_id
            )
            if document["transaction"]["transaction_id"] != record.transaction_id:
                return False
            await self._container.delete_item(
                record.document_id,
                partition_key=record.owner_id,
                etag=document["_etag"],
                match_condition=MatchConditions.IfNotModified,
            )
            return record.expires_at > time.time()
        except (CosmosAccessConditionFailedError, CosmosResourceNotFoundError):
            return False

    async def delete_owner(self, owner_id: str, session_hash: str | None = None) -> None:
        if self._container is None:
            async with self._lock:
                self._records = {
                    key: record
                    for key, record in self._records.items()
                    if key[0] != owner_id
                    or (session_hash is not None and record.session_hash != session_hash)
                }
            return
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        items = self._container.query_items(
            query=(
                "SELECT c.id, c.transaction.session_hash FROM c "
                "WHERE c.userId = @owner AND c.documentType = 'profile-enrichment'"
            ),
            parameters=[{"name": "@owner", "value": owner_id}],
            partition_key=owner_id,
        )
        async for item in items:
            if session_hash is not None and item["session_hash"] != session_hash:
                continue
            try:
                await self._container.delete_item(item["id"], partition_key=owner_id)
            except CosmosResourceNotFoundError:
                continue


def create_enrichment_repository(settings: AppSettings) -> EnrichmentRepository:
    if settings.persistence_mode != "azure":
        return EnrichmentRepository()
    from azure.cosmos.aio import CosmosClient

    from app.generation import _default_azure_credential

    client = CosmosClient(settings.cosmos_endpoint, credential=_default_azure_credential())
    return EnrichmentRepository(
        client.get_database_client(settings.cosmos_database_name or "appdb").get_container_client(
            settings.cosmos_container_name or "cards"
        )
    )


def new_transaction(
    *, state: str | None = None, **values: Any
) -> tuple[str, EnrichmentTransaction]:
    state = state or uuid4().hex + uuid4().hex
    return state, EnrichmentTransaction(
        transaction_id=uuid4().hex,
        state_hash=digest(state),
        expires_at=time.time() + 900,
        **values,
    )
