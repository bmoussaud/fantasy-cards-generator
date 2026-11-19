"""Stateless profile proposal contract; credentials and evidence are never domain fields."""

from __future__ import annotations

import ipaddress
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EnrichmentStatus = Literal[
    "completed",
    "no_evidence",
    "ambiguous_identity",
    "unavailable_configuration",
    "refused",
    "unavailable",
    "timeout",
    "invalid_response",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @field_validator("schemaVersion", mode="before", check_fields=False)
    @classmethod
    def integer_schema(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid_schema_version")
        return value


class ProfileEnrichmentRequest(_StrictModel):
    schemaVersion: Literal[1] = 1
    operation: Literal["enrich_profile"]
    publicName: str = Field(min_length=1, max_length=80)
    publicContext: str = Field(default="", max_length=200)
    transactionId: str = Field(pattern=r"^[a-f0-9]{32}$")

    @field_validator("publicName")
    @classmethod
    def require_name(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("invalid_public_name")
        return value


class PublicReference(_StrictModel):
    url: str = Field(min_length=1, max_length=2048)
    title: str = Field(min_length=1, max_length=200)

    @field_validator("url")
    @classmethod
    def public_https(cls, value: str) -> str:
        try:
            parts = urlsplit(value)
            host = (parts.hostname or "").lower().rstrip(".")
            invalid = (
                parts.scheme != "https"
                or parts.username is not None
                or parts.password is not None
                or parts.port not in (None, 443)
                or "." not in host
                or not host.isascii()
                or any(char.isspace() or ord(char) < 32 for char in value)
                or "\\" in value
                or "%" in host
                or host.endswith(
                    (
                        ".local",
                        ".internal",
                        ".localhost",
                        ".test",
                        ".invalid",
                        ".sharepoint.com",
                        ".sharepoint-df.com",
                        ".onedrive.com",
                        ".office.com",
                        ".microsoftonline.com",
                    )
                )
                or host
                in {
                    "localhost",
                    "onedrive.live.com",
                    "1drv.ms",
                    "teams.microsoft.com",
                    "graph.microsoft.com",
                    "sharepoint.com",
                    "onedrive.com",
                    "office.com",
                }
            )
            try:
                ipaddress.ip_address(host)
                invalid = True
            except ValueError:
                pass
        except ValueError:
            invalid = True
        if invalid:
            raise ValueError("invalid_public_reference")
        return value

    @field_validator("title")
    @classmethod
    def plain_title(cls, value: str) -> str:
        if (
            not value.strip()
            or any(ord(char) < 32 for char in value)
            or any(char in value for char in "<>")
        ):
            raise ValueError("invalid_public_title")
        return value


class ProfileEnrichmentResponse(_StrictModel):
    schemaVersion: Literal[2] = 2
    status: EnrichmentStatus
    current_role: Annotated[str, Field(min_length=2, max_length=120)] | None = None
    expertise: list[Annotated[str, Field(min_length=2, max_length=80)]] = Field(
        default_factory=list, max_length=5
    )
    expertise_descriptions: list[Annotated[str, Field(min_length=40, max_length=1000)]] = Field(
        default_factory=list, max_length=5
    )
    publicReferences: list[PublicReference] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def require_consistent_status(self) -> ProfileEnrichmentResponse:
        if self.status == "completed":
            validate_ability_descriptions(
                self.expertise, self.expertise_descriptions, required=True
            )
            if (
                self.current_role is None
                or len(self.current_role.strip()) < 2
                or not self.expertise
                or any(len(skill.strip()) < 2 for skill in self.expertise)
                or len({skill.strip().casefold() for skill in self.expertise})
                != len(self.expertise)
                or any(ord(char) < 32 for char in self.current_role + "".join(self.expertise))
                or not self.publicReferences
            ):
                raise ValueError("incomplete_proposal")
        elif self.current_role is not None or self.expertise or self.expertise_descriptions:
            raise ValueError("unexpected_proposal")
        return self


def validate_ability_descriptions(
    names: list[str], descriptions: list[str], *, required: bool = False
) -> None:
    if not descriptions and not required:
        return
    if len(descriptions) != len(names):
        raise ValueError("ability_description_pair_mismatch")
    for name, description in zip(names, descriptions, strict=True):
        sentences = re.split(r"(?<=[.!?])\s+", description.strip())
        if (
            not name.strip()
            or not 40 <= len(description) <= 1000
            or len(sentences) not in (2, 3)
            or any(len(sentence.strip()) < 10 for sentence in sentences)
            or description[-1:] not in (".", "!", "?")
            or any(ord(char) < 32 or ord(char) == 127 for char in description)
            or any(char in description for char in "<>")
        ):
            raise ValueError("invalid_ability_description")
