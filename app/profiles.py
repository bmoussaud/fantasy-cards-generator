from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.generation import _default_azure_credential, now_iso
from app.problems import ProblemDetails
from app.settings import AppSettings

PROFILE_DOCUMENT_ID = "profile:current"
PROFILE_DOCUMENT_TYPE = "professional-profile"


class ProfessionalProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=2, max_length=120)
    current_role: str = Field(min_length=2, max_length=120)
    expertise: list[str] = Field(min_length=1, max_length=5)
    post_excerpts: list[str] = Field(default_factory=list, max_length=3)

    @field_validator("display_name", "title", "current_role", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        return " ".join(value.split()) if isinstance(value, str) else value

    @field_validator("expertise", "post_excerpts", mode="before")
    @classmethod
    def normalize_values(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        return [
            " ".join(item.split()) if isinstance(item, str) else item
            for item in value
            if not isinstance(item, str) or item.strip()
        ]

    @model_validator(mode="after")
    def validate_context(self) -> ProfessionalProfile:
        for values, minimum, maximum in (
            (self.expertise, 2, 80),
            (self.post_excerpts, 20, 500),
        ):
            if any(not minimum <= len(item) <= maximum for item in values):
                raise ValueError(f"Each entry must contain {minimum} to {maximum} characters.")
        values = [
            self.display_name,
            self.title,
            self.current_role,
            *self.expertise,
            *self.post_excerpts,
        ]
        if any(
            "linkedin.com/in/" in value.casefold() or "linkedin.com/pub/" in value.casefold()
            for value in values
        ):
            raise ValueError("LinkedIn profile URLs are not accepted.")
        if len(" ".join(self.context("x" * 200).split())) > 2500:
            raise ValueError(
                "Shorten your profile to leave room for 200 characters of creative direction "
                "within the 2,500-character generation limit."
            )
        return self

    def context(self, creative_direction: str | None = None) -> str:
        sections = [
            f"Identity (user-confirmed): {self.display_name}",
            f"Professional title (user-provided): {self.title}",
            f"Current role (user-provided): {self.current_role}",
            "Explicit expertise (user-provided, not LinkedIn-verified):\n"
            + "\n".join(f"- {item}" for item in self.expertise),
        ]
        if self.post_excerpts:
            sections.append(
                "Recent post excerpts (user-selected source material):\n"
                + "\n".join(f"- {item}" for item in self.post_excerpts)
            )
        if creative_direction:
            sections.append(f"Creative direction: {creative_direction}")
        sections.append(
            "Interpret interests as themes only; do not present them as certified expertise."
        )
        return "\n\n".join(sections)


class ProfileGenerationOptions(BaseModel):
    creative_direction: str | None = Field(default=None, max_length=200)

    @field_validator("creative_direction", mode="before")
    @classmethod
    def normalize_direction(cls, value: object) -> object:
        return (" ".join(value.split()) or None) if isinstance(value, str) else value


@dataclass(frozen=True)
class SavedProfile:
    profile: ProfessionalProfile
    revision: str
    updated_at: str


class ProfileRepository(Protocol):
    async def get(self, owner_id: str) -> SavedProfile | None: ...

    async def save(
        self, owner_id: str, profile: ProfessionalProfile, revision: str | None
    ) -> None: ...

    async def delete_owner(self, owner_id: str) -> None: ...


def profile_conflict() -> ProblemDetails:
    return ProblemDetails(
        status_code=409,
        title="Profile changed",
        detail="Your profile changed in another tab. Reload the page before saving again.",
        type="/problems/profile-conflict",
        error_code="profile_conflict",
    )


class InMemoryProfileRepository:
    def __init__(self) -> None:
        self._records: dict[str, SavedProfile] = {}
        self._lock = asyncio.Lock()

    async def get(self, owner_id: str) -> SavedProfile | None:
        async with self._lock:
            record = self._records.get(owner_id)
            return (
                SavedProfile(
                    record.profile.model_copy(deep=True), record.revision, record.updated_at
                )
                if record
                else None
            )

    async def save(self, owner_id: str, profile: ProfessionalProfile, revision: str | None) -> None:
        async with self._lock:
            current = self._records.get(owner_id)
            if (current.revision if current else None) != revision:
                raise profile_conflict()
            self._records[owner_id] = SavedProfile(
                profile.model_copy(deep=True), uuid4().hex, now_iso()
            )

    async def delete_owner(self, owner_id: str) -> None:
        async with self._lock:
            self._records.pop(owner_id, None)


class AzureCosmosProfileRepository:
    def __init__(self, settings: AppSettings) -> None:
        from azure.cosmos.aio import CosmosClient

        self._client = CosmosClient(
            settings.cosmos_endpoint, credential=_default_azure_credential()
        )
        self._container = self._client.get_database_client(
            settings.cosmos_database_name or "appdb"
        ).get_container_client(settings.cosmos_container_name or "cards")

    async def get(self, owner_id: str) -> SavedProfile | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            document = await self._container.read_item(PROFILE_DOCUMENT_ID, partition_key=owner_id)
        except CosmosResourceNotFoundError:
            return None
        if document["userId"] != owner_id or document["documentType"] != PROFILE_DOCUMENT_TYPE:
            raise ValueError("Invalid professional profile document.")
        return SavedProfile(
            ProfessionalProfile.model_validate(document["profile"]),
            document["_etag"],
            document["updatedAt"],
        )

    async def save(self, owner_id: str, profile: ProfessionalProfile, revision: str | None) -> None:
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import (
            CosmosAccessConditionFailedError,
            CosmosResourceExistsError,
            CosmosResourceNotFoundError,
        )

        document: dict[str, Any] = {
            "id": PROFILE_DOCUMENT_ID,
            "userId": owner_id,
            "documentType": PROFILE_DOCUMENT_TYPE,
            "schemaVersion": 1,
            "profile": profile.model_dump(),
            "updatedAt": now_iso(),
            "ttl": -1,
        }
        try:
            if revision is None:
                await self._container.create_item(document)
            else:
                await self._container.replace_item(
                    PROFILE_DOCUMENT_ID,
                    document,
                    etag=revision,
                    match_condition=MatchConditions.IfNotModified,
                )
        except (
            CosmosAccessConditionFailedError,
            CosmosResourceExistsError,
            CosmosResourceNotFoundError,
        ):
            raise profile_conflict() from None

    async def delete_owner(self, owner_id: str) -> None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            await self._container.delete_item(PROFILE_DOCUMENT_ID, partition_key=owner_id)
        except CosmosResourceNotFoundError:
            return
