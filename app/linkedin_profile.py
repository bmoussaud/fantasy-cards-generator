from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import re
import socket
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from authlib.integrations.starlette_client import OAuth, StarletteOAuth2App
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.generation import ReferenceImageUpload, _default_azure_credential, now_iso
from app.settings import AppSettings, SettingsError

LINKEDIN_OIDC_SCOPES = ("openid", "profile")
LINKEDIN_OIDC_METADATA_URL = "https://www.linkedin.com/oauth/.well-known/openid-configuration"
LINKEDIN_OAUTH_STATE_DOCUMENT_PREFIX = "linkedin-oauth-state:"
LINKEDIN_IMPORT_DRAFT_DOCUMENT_PREFIX = "linkedin-import-draft:"
LINKEDIN_CREATION_DRAFT_SESSION_KEY = "linkedin_creation_draft_id"
LINKEDIN_IMPORT_NOTICE_SESSION_KEY = "linkedin_import_notice"
LINKEDIN_TRANSIENT_TTL_SECONDS = 15 * 60
LINKEDIN_TRANSIENT_DOCUMENT_TYPES = {
    "linkedin-oauth-state",
    "linkedin-import-draft",
}
CREATION_DRAFT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$", re.ASCII)
LINKEDIN_PICTURE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
LINKEDIN_PICTURE_MAX_BYTES = 4 * 1024 * 1024
LINKEDIN_PICTURE_TIMEOUT_SECONDS = 10.0


class _PinnedResolver(AbstractResolver):
    def __init__(self, hostname: str, addresses: tuple[str, ...]) -> None:
        self._hostname = hostname
        self._addresses = addresses

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        if host.rstrip(".").lower() != self._hostname:
            raise OSError("Pinned resolver rejected an unexpected hostname.")
        results: list[ResolveResult] = []
        for value in self._addresses:
            address_family = (
                socket.AF_INET6 if ipaddress.ip_address(value).version == 6 else socket.AF_INET
            )
            if family not in {socket.AF_UNSPEC, address_family}:
                continue
            results.append(
                ResolveResult(
                    hostname=host,
                    host=value,
                    port=port,
                    family=address_family,
                    proto=0,
                    flags=0,
                )
            )
        if not results:
            raise OSError("Pinned resolver has no address for the requested family.")
        return results

    async def close(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class LinkedInSettings:
    enabled: bool
    client_id: str | None
    client_secret: str | None
    redirect_uri: str | None
    pilot_owner_allowlist: frozenset[str]
    picture_host_allowlist: frozenset[str]
    scopes: tuple[str, ...] = LINKEDIN_OIDC_SCOPES
    metadata_url: str = LINKEDIN_OIDC_METADATA_URL

    @property
    def is_configured(self) -> bool:
        return not self.missing_required()

    def missing_required(self) -> tuple[str, ...]:
        if not self.enabled:
            return ()
        missing: list[str] = []
        if not self.client_id:
            missing.append("LINKEDIN_CLIENT_ID")
        if not self.client_secret:
            missing.append("LINKEDIN_CLIENT_SECRET")
        if not self.redirect_uri:
            missing.append("LINKEDIN_REDIRECT_URI")
        if not self.pilot_owner_allowlist:
            missing.append("LINKEDIN_PILOT_OWNER_ALLOWLIST")
        return tuple(missing)

    def allows_owner(self, owner_id: str) -> bool:
        return is_canonical_entra_owner(owner_id) and owner_id in self.pilot_owner_allowlist


def load_linkedin_settings(
    environ: Mapping[str, str] | None = None,
) -> LinkedInSettings:
    env = os.environ if environ is None else environ
    enabled = _bool_value(env.get("LINKEDIN_OIDC_ENABLED"), default=False)
    owner_allowlist = frozenset(
        _validate_owner_id(value) for value in _split_csv(env.get("LINKEDIN_PILOT_OWNER_ALLOWLIST"))
    )
    picture_hosts = frozenset(
        _validate_picture_host(value)
        for value in _split_csv(env.get("LINKEDIN_PICTURE_HOST_ALLOWLIST"))
    )
    settings = LinkedInSettings(
        enabled=enabled,
        client_id=_optional_value(env.get("LINKEDIN_CLIENT_ID")),
        client_secret=_optional_value(env.get("LINKEDIN_CLIENT_SECRET")),
        redirect_uri=_validate_redirect_uri(env.get("LINKEDIN_REDIRECT_URI")),
        pilot_owner_allowlist=owner_allowlist,
        picture_host_allowlist=picture_hosts,
    )
    missing = settings.missing_required()
    if missing:
        raise SettingsError(
            "LinkedIn OIDC is enabled but required configuration is missing: "
            + ", ".join(missing)
            + "."
        )
    return settings


def create_linkedin_oauth_client(
    settings: LinkedInSettings,
) -> StarletteOAuth2App:
    if not settings.is_configured:
        raise SettingsError("LinkedIn OIDC configuration is incomplete.")
    oauth = OAuth()
    oauth.register(
        name="linkedin_oidc",
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        server_metadata_url=settings.metadata_url,
        client_kwargs={
            "scope": " ".join(LINKEDIN_OIDC_SCOPES),
            "code_challenge_method": "S256",
        },
    )
    client = oauth.create_client("linkedin_oidc")
    if client is None:
        raise RuntimeError("Failed to create the LinkedIn OIDC client.")
    return client


async def fetch_linkedin_picture(
    picture_uri: str,
    *,
    allowed_hosts: frozenset[str],
    resolver: Callable[[str], Awaitable[tuple[str, ...]]] | None = None,
) -> ReferenceImageUpload:
    parsed = urlsplit(picture_uri)
    hostname = (parsed.hostname or "").rstrip(".").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("LinkedIn picture URI has an invalid port.") from exc
    if (
        parsed.scheme != "https"
        or not hostname
        or hostname not in allowed_hosts
        or parsed.username
        or parsed.password
        or parsed.fragment
        or port not in {None, 443}
    ):
        raise ValueError("LinkedIn picture URI is not allowed.")

    addresses = await (resolver or _resolve_host_addresses)(hostname)
    if not addresses or any(not _is_public_ip(value) for value in addresses):
        raise ValueError("LinkedIn picture host did not resolve to public addresses.")

    timeout = aiohttp.ClientTimeout(total=LINKEDIN_PICTURE_TIMEOUT_SECONDS)
    connector = aiohttp.TCPConnector(
        resolver=_PinnedResolver(hostname, addresses),
        use_dns_cache=True,
    )
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        trust_env=False,
    ) as client:
        async with client.get(picture_uri, allow_redirects=False) as response:
            if 300 <= response.status < 400:
                raise ValueError("LinkedIn picture redirects are not allowed.")
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type not in LINKEDIN_PICTURE_CONTENT_TYPES:
                raise ValueError("LinkedIn picture content type is not supported.")
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    declared_size = int(content_length)
                except ValueError as exc:
                    raise ValueError("LinkedIn picture size is invalid.") from exc
                if declared_size > LINKEDIN_PICTURE_MAX_BYTES:
                    raise ValueError("LinkedIn picture is too large.")
            payload = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                payload.extend(chunk)
                if len(payload) > LINKEDIN_PICTURE_MAX_BYTES:
                    raise ValueError("LinkedIn picture is too large.")
    return ReferenceImageUpload(
        content=bytes(payload),
        content_type=content_type,
        filename="linkedin-profile-photo",
    )


class NormalizedLinkedInProfile(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    sub: str = Field(min_length=1, max_length=255)
    name: str | None = Field(default=None, max_length=255)
    given_name: str | None = Field(default=None, max_length=120)
    family_name: str | None = Field(default=None, max_length=120)
    picture: str | None = Field(default=None, max_length=2048)

    @field_validator("sub", "name", "given_name", "family_name", "picture", mode="before")
    @classmethod
    def normalize_strings(cls, value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        normalized = " ".join(value.split())
        return normalized or None

    @model_validator(mode="after")
    def require_display_name(self) -> "NormalizedLinkedInProfile":
        if self.display_name is None:
            raise ValueError("LinkedIn UserInfo must contain a display name.")
        return self

    @property
    def display_name(self) -> str | None:
        if self.name:
            return self.name
        combined = " ".join(value for value in (self.given_name, self.family_name) if value).strip()
        return combined or None


@dataclass(slots=True)
class LinkedInOAuthState:
    owner_id: str
    state_hash: str
    nonce: str
    code_verifier: str
    creation_draft_id: str
    photo_consent: bool
    return_path: str
    expires_at: str
    created_at: str = field(default_factory=now_iso)
    etag: str | None = None

    @property
    def document_id(self) -> str:
        return f"{LINKEDIN_OAUTH_STATE_DOCUMENT_PREFIX}{self.state_hash}"

    def to_document(self) -> dict[str, Any]:
        return {
            "id": self.document_id,
            "documentType": "linkedin-oauth-state",
            "userId": self.owner_id,
            "owner": {"ownerId": self.owner_id},
            "stateHash": self.state_hash,
            "nonce": self.nonce,
            "codeVerifier": self.code_verifier,
            "creationDraftId": self.creation_draft_id,
            "photoConsent": self.photo_consent,
            "returnPath": validate_return_path(self.return_path),
            "expiresAt": self.expires_at,
            "createdAt": self.created_at,
            "ttl": _remaining_ttl_seconds(self.expires_at),
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "LinkedInOAuthState | None":
        if document.get("documentType") != "linkedin-oauth-state":
            return None
        required = (
            "userId",
            "stateHash",
            "nonce",
            "codeVerifier",
            "creationDraftId",
            "returnPath",
            "expiresAt",
        )
        if any(
            not isinstance(document.get(name), str) or not document.get(name) for name in required
        ):
            return None
        return cls(
            owner_id=str(document["userId"]),
            state_hash=str(document["stateHash"]),
            nonce=str(document["nonce"]),
            code_verifier=str(document["codeVerifier"]),
            creation_draft_id=str(document["creationDraftId"]),
            photo_consent=document.get("photoConsent") is True,
            return_path=validate_return_path(str(document["returnPath"])),
            expires_at=str(document["expiresAt"]),
            created_at=str(document.get("createdAt") or now_iso()),
            etag=_optional_value(document.get("_etag")),
        )


@dataclass(slots=True)
class LinkedInImportDraft:
    owner_id: str
    draft_id: str
    display_name: str
    saved_photo_id: str | None
    outcome: str
    expires_at: str
    created_at: str = field(default_factory=now_iso)

    @property
    def document_id(self) -> str:
        return f"{LINKEDIN_IMPORT_DRAFT_DOCUMENT_PREFIX}{self.draft_id}"

    def to_document(self) -> dict[str, Any]:
        return {
            "id": self.document_id,
            "documentType": "linkedin-import-draft",
            "userId": self.owner_id,
            "owner": {"ownerId": self.owner_id},
            "draftId": self.draft_id,
            "displayName": _bounded_required_text(
                self.display_name,
                name="display name",
                maximum=80,
            ),
            "savedPhotoId": _bounded_optional_text(
                self.saved_photo_id,
                name="saved photo ID",
                maximum=255,
            ),
            "outcome": _bounded_required_text(self.outcome, name="outcome", maximum=64),
            "expiresAt": self.expires_at,
            "createdAt": self.created_at,
            "ttl": _remaining_ttl_seconds(self.expires_at),
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "LinkedInImportDraft | None":
        if document.get("documentType") != "linkedin-import-draft":
            return None
        required = ("userId", "draftId", "displayName", "outcome", "expiresAt")
        if any(
            not isinstance(document.get(name), str) or not document.get(name) for name in required
        ):
            return None
        return cls(
            owner_id=str(document["userId"]),
            draft_id=str(document["draftId"]),
            display_name=str(document["displayName"]),
            saved_photo_id=_optional_value(document.get("savedPhotoId")),
            outcome=str(document["outcome"]),
            expires_at=str(document["expiresAt"]),
            created_at=str(document.get("createdAt") or now_iso()),
        )


class AbstractLinkedInTransientRepository:
    async def create_oauth_state(self, record: LinkedInOAuthState) -> bool:
        raise NotImplementedError

    async def consume_oauth_state(
        self,
        *,
        owner_id: str,
        state_hash: str,
    ) -> LinkedInOAuthState | None:
        raise NotImplementedError

    async def replace_draft(self, record: LinkedInImportDraft) -> LinkedInImportDraft:
        raise NotImplementedError

    async def get_draft(
        self,
        *,
        owner_id: str,
        draft_id: str,
    ) -> LinkedInImportDraft | None:
        raise NotImplementedError

    async def delete_draft(self, *, owner_id: str, draft_id: str) -> None:
        raise NotImplementedError

    async def delete_owner(self, owner_id: str) -> None:
        raise NotImplementedError


class InMemoryLinkedInTransientRepository(AbstractLinkedInTransientRepository):
    def __init__(self) -> None:
        self._states: dict[tuple[str, str], LinkedInOAuthState] = {}
        self._drafts: dict[tuple[str, str], LinkedInImportDraft] = {}
        self._lock = asyncio.Lock()

    async def create_oauth_state(self, record: LinkedInOAuthState) -> bool:
        async with self._lock:
            key = (record.owner_id, record.state_hash)
            if key in self._states:
                return False
            self._states[key] = record
            return True

    async def consume_oauth_state(
        self,
        *,
        owner_id: str,
        state_hash: str,
    ) -> LinkedInOAuthState | None:
        async with self._lock:
            record = self._states.pop((owner_id, state_hash), None)
            if record is None or _is_expired(record.expires_at):
                return None
            return record

    async def replace_draft(self, record: LinkedInImportDraft) -> LinkedInImportDraft:
        async with self._lock:
            self._drafts[(record.owner_id, record.draft_id)] = record
            return record

    async def get_draft(
        self,
        *,
        owner_id: str,
        draft_id: str,
    ) -> LinkedInImportDraft | None:
        async with self._lock:
            record = self._drafts.get((owner_id, draft_id))
            if record is None:
                return None
            if _is_expired(record.expires_at):
                self._drafts.pop((owner_id, draft_id), None)
                return None
            return record

    async def delete_draft(self, *, owner_id: str, draft_id: str) -> None:
        async with self._lock:
            self._drafts.pop((owner_id, draft_id), None)

    async def delete_owner(self, owner_id: str) -> None:
        async with self._lock:
            self._states = {key: value for key, value in self._states.items() if key[0] != owner_id}
            self._drafts = {key: value for key, value in self._drafts.items() if key[0] != owner_id}


class AzureCosmosLinkedInTransientRepository(AbstractLinkedInTransientRepository):
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

    async def create_oauth_state(self, record: LinkedInOAuthState) -> bool:
        from azure.cosmos.exceptions import CosmosResourceExistsError

        container = await self._get_container()
        try:
            await container.create_item(record.to_document())
        except CosmosResourceExistsError:
            return False
        return True

    async def consume_oauth_state(
        self,
        *,
        owner_id: str,
        state_hash: str,
    ) -> LinkedInOAuthState | None:
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import CosmosHttpResponseError, CosmosResourceNotFoundError

        container = await self._get_container()
        document_id = f"{LINKEDIN_OAUTH_STATE_DOCUMENT_PREFIX}{state_hash}"
        try:
            document = await container.read_item(document_id, partition_key=owner_id)
        except CosmosResourceNotFoundError:
            return None
        record = LinkedInOAuthState.from_document(document)
        if record is None or record.owner_id != owner_id or not record.etag:
            return None
        try:
            await container.delete_item(
                document_id,
                partition_key=owner_id,
                etag=record.etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except (CosmosResourceNotFoundError, CosmosHttpResponseError) as exc:
            if getattr(exc, "status_code", None) in {404, 412}:
                return None
            raise
        if _is_expired(record.expires_at):
            return None
        return record

    async def replace_draft(self, record: LinkedInImportDraft) -> LinkedInImportDraft:
        container = await self._get_container()
        await container.upsert_item(record.to_document())
        return record

    async def get_draft(
        self,
        *,
        owner_id: str,
        draft_id: str,
    ) -> LinkedInImportDraft | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        container = await self._get_container()
        document_id = f"{LINKEDIN_IMPORT_DRAFT_DOCUMENT_PREFIX}{draft_id}"
        try:
            document = await container.read_item(document_id, partition_key=owner_id)
        except CosmosResourceNotFoundError:
            return None
        record = LinkedInImportDraft.from_document(document)
        if record is None or record.owner_id != owner_id:
            return None
        if _is_expired(record.expires_at):
            await self.delete_draft(owner_id=owner_id, draft_id=draft_id)
            return None
        return record

    async def delete_draft(self, *, owner_id: str, draft_id: str) -> None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        container = await self._get_container()
        try:
            await container.delete_item(
                f"{LINKEDIN_IMPORT_DRAFT_DOCUMENT_PREFIX}{draft_id}",
                partition_key=owner_id,
            )
        except CosmosResourceNotFoundError:
            return

    async def delete_owner(self, owner_id: str) -> None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        container = await self._get_container()
        iterator = container.query_items(
            query=(
                "SELECT c.id FROM c WHERE c.userId = @ownerId "
                "AND ARRAY_CONTAINS(@documentTypes, c.documentType)"
            ),
            parameters=[
                {"name": "@ownerId", "value": owner_id},
                {
                    "name": "@documentTypes",
                    "value": sorted(LINKEDIN_TRANSIENT_DOCUMENT_TYPES),
                },
            ],
            partition_key=owner_id,
        )
        async for document in iterator:
            document_id = document.get("id")
            if not isinstance(document_id, str):
                continue
            try:
                await container.delete_item(document_id, partition_key=owner_id)
            except CosmosResourceNotFoundError:
                continue


def create_linkedin_transient_repository(
    settings: AppSettings,
) -> AbstractLinkedInTransientRepository:
    if settings.persistence_mode == "azure":
        return AzureCosmosLinkedInTransientRepository(settings)
    return InMemoryLinkedInTransientRepository()


def hash_oauth_state(state: str) -> str:
    normalized = state.strip()
    if not normalized:
        raise ValueError("OAuth state must not be blank.")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def transient_expiry(*, seconds: int = LINKEDIN_TRANSIENT_TTL_SECONDS) -> str:
    return (
        (datetime.now(UTC) + timedelta(seconds=seconds))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def is_canonical_entra_owner(owner_id: str) -> bool:
    parts = owner_id.split(":")
    if len(parts) != 2:
        return False
    try:
        UUID(parts[0])
        UUID(parts[1])
    except ValueError:
        return False
    return True


def validate_return_path(value: str) -> str:
    normalized = value.strip()
    parsed = urlsplit(normalized)
    if (
        not normalized.startswith("/")
        or normalized.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.fragment
    ):
        raise ValueError("LinkedIn return path must be a local absolute path.")
    return normalized


def validate_creation_draft_id(value: str) -> str:
    normalized = value.strip()
    if not CREATION_DRAFT_ID_PATTERN.fullmatch(normalized):
        raise ValueError("Creation draft identifier is invalid.")
    return normalized


async def _resolve_host_addresses(hostname: str) -> tuple[str, ...]:
    records = await asyncio.to_thread(
        socket.getaddrinfo,
        hostname,
        443,
        type=socket.SOCK_STREAM,
    )
    return tuple(sorted({str(record[4][0]) for record in records}))


def _is_public_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _validate_owner_id(value: str) -> str:
    if not is_canonical_entra_owner(value):
        raise SettingsError(
            "LINKEDIN_PILOT_OWNER_ALLOWLIST must contain canonical '<tid>:<oid>' owners."
        )
    return value


def _validate_picture_host(value: str) -> str:
    normalized = value.strip().rstrip(".").lower()
    parsed = urlsplit(f"//{normalized}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise SettingsError(
            "LINKEDIN_PICTURE_HOST_ALLOWLIST must contain hostnames without ports."
        ) from exc
    if (
        not normalized
        or parsed.username
        or parsed.password
        or port is not None
        or parsed.hostname != normalized
        or "/" in normalized
    ):
        raise SettingsError(
            "LINKEDIN_PICTURE_HOST_ALLOWLIST must contain hostnames without schemes or ports."
        )
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        return normalized
    raise SettingsError("LINKEDIN_PICTURE_HOST_ALLOWLIST must not contain IP addresses.")


def _validate_redirect_uri(value: Any) -> str | None:
    normalized = _optional_value(value)
    if normalized is None:
        return None
    parsed = urlsplit(normalized)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise SettingsError("LINKEDIN_REDIRECT_URI must be an HTTPS URL without credentials.")
    return normalized


def _split_csv(value: Any) -> tuple[str, ...]:
    normalized = _optional_value(value)
    if normalized is None:
        return ()
    return tuple(item.strip() for item in normalized.split(",") if item.strip())


def _bool_value(value: Any, *, default: bool) -> bool:
    normalized = _optional_value(value)
    if normalized is None:
        return default
    lowered = normalized.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise SettingsError("LINKEDIN_OIDC_ENABLED must be a boolean value.")


def _optional_value(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _bounded_required_text(value: str, *, name: str, maximum: int) -> str:
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{name} must be 1-{maximum} characters.")
    return normalized


def _bounded_optional_text(value: str | None, *, name: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _bounded_required_text(value, name=name, maximum=maximum)


def _parse_timestamp(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def _is_expired(expires_at: str) -> bool:
    try:
        return _parse_timestamp(expires_at) <= datetime.now(UTC)
    except ValueError:
        return True


def _remaining_ttl_seconds(expires_at: str) -> int:
    try:
        remaining = int((_parse_timestamp(expires_at) - datetime.now(UTC)).total_seconds())
    except ValueError as exc:
        raise ValueError("Transient record expiry must be an ISO-8601 timestamp.") from exc
    return max(1, min(remaining, LINKEDIN_TRANSIENT_TTL_SECONDS))
