from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, TypedDict
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import UUID

from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.starlette_client import OAuth, StarletteOAuth2App
from fastapi import HTTPException, Request, status

from app.generation import AuthenticatedOwner
from app.secrets import (
    SecretNotFoundError,
    SecretProvider,
    SecretProviderError,
    SecretValue,
    SecretVersionUnavailableError,
    load_secret_provider_config,
    utc_now,
)

AUTH_SESSION_KEY = "user"
AUTH_NONCE_SESSION_KEY = "auth_nonce"
DEFAULT_ENTRA_AUTHORITY = "https://login.microsoftonline.com/organizations/v2.0"
DEFAULT_ENTRA_CLIENT_SECRET_OVERLAP = timedelta(minutes=15)
ENTRA_TENANT_ID_PLACEHOLDER = "{tenantid}"


class AuthenticatedUser(TypedDict):
    sub: str
    name: str | None
    email: str | None
    tenant_id: str | None
    object_id: str | None
    owner_id: str


@dataclass(frozen=True)
class AuthSettings:
    client_id: str | None
    client_secret: str | None
    authority: str | None
    redirect_uri: str | None
    post_logout_redirect_uri: str | None
    session_secret_key: str | None
    scopes: tuple[str, ...]

    @property
    def authority_url(self) -> str | None:
        if not self.authority:
            return None
        return self.authority.rstrip("/")

    @property
    def metadata_url(self) -> str | None:
        if not self.authority_url:
            return None
        return f"{self.authority_url}/.well-known/openid-configuration"

    @property
    def logout_url(self) -> str | None:
        if not self.authority_url:
            return None

        parsed = urlsplit(self.authority_url)
        path = parsed.path.rstrip("/")
        if path.endswith("/v2.0"):
            path = path[: -len("/v2.0")]
        logout_path = f"{path}/oauth2/v2.0/logout"
        return urlunsplit((parsed.scheme, parsed.netloc, logout_path, "", ""))

    @property
    def scope(self) -> str:
        return " ".join(self.scopes)

    @property
    def is_configured(self) -> bool:
        return not self.missing_required()

    def missing_required(self, *, include_client_secret: bool = True) -> tuple[str, ...]:
        missing: list[str] = []
        if not self.client_id:
            missing.append("ENTRA_CLIENT_ID")
        if include_client_secret and not self.client_secret:
            missing.append("ENTRA_CLIENT_SECRET")
        if not self.redirect_uri:
            missing.append("ENTRA_REDIRECT_URI")
        return tuple(missing)

    def with_client_secret(self, client_secret: str | None) -> AuthSettings:
        return replace(self, client_secret=client_secret)


@dataclass(frozen=True)
class _OAuthClientBinding:
    secret: SecretValue
    client: StarletteOAuth2App


@dataclass(frozen=True)
class _RetainedOAuthClientBinding:
    binding: _OAuthClientBinding
    expires_at: datetime


def load_auth_settings() -> AuthSettings:
    session_secret_key = _load_required_session_secret_key()

    configured_scopes = _first_env(
        "ENTRA_SCOPES", "ENTRA_EXTERNAL_ID_SCOPES", default="openid profile email"
    ).split()
    deduplicated_scopes = tuple(dict.fromkeys(configured_scopes))
    scopes = (
        deduplicated_scopes if "openid" in deduplicated_scopes else ("openid", *deduplicated_scopes)
    )

    return AuthSettings(
        client_id=_first_env("ENTRA_CLIENT_ID", "ENTRA_EXTERNAL_ID_CLIENT_ID"),
        client_secret=_first_env("ENTRA_CLIENT_SECRET", "ENTRA_EXTERNAL_ID_CLIENT_SECRET"),
        authority=_first_env(
            "ENTRA_AUTHORITY",
            "ENTRA_EXTERNAL_ID_AUTHORITY",
            default=DEFAULT_ENTRA_AUTHORITY,
        ),
        redirect_uri=_first_env("ENTRA_REDIRECT_URI", "ENTRA_EXTERNAL_ID_REDIRECT_URI"),
        post_logout_redirect_uri=_first_env(
            "ENTRA_POST_LOGOUT_REDIRECT_URI",
            "ENTRA_EXTERNAL_ID_POST_LOGOUT_REDIRECT_URI",
        ),
        session_secret_key=session_secret_key,
        scopes=scopes,
    )


def _load_required_session_secret_key() -> str | None:
    secret_provider_config = load_secret_provider_config()
    if secret_provider_config.backend == "azure" or (
        secret_provider_config.backend == "auto"
        and secret_provider_config.key_vault_uri is not None
    ):
        return None

    session_secret_key = os.getenv("APP_SESSION_SECRET_KEY")
    if not session_secret_key:
        raise RuntimeError("APP_SESSION_SECRET_KEY must be set before starting the application.")
    return session_secret_key


def ensure_auth_configured(settings: AuthSettings, *, include_client_secret: bool = True) -> None:
    missing = settings.missing_required(include_client_secret=include_client_secret)
    if missing:
        missing_text = ", ".join(missing)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Authentication is not configured. Missing: {missing_text}",
        )


def create_oauth_client(settings: AuthSettings) -> StarletteOAuth2App:
    oauth = OAuth()
    oauth.register(
        name="entra_id",
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        server_metadata_url=settings.metadata_url,
        client_kwargs={
            "scope": settings.scope,
            "code_challenge_method": "S256",
        },
    )

    client = oauth.create_client("entra_id")
    if client is None:
        raise RuntimeError("Failed to create the Entra ID OAuth client.")
    return client


class EntraOAuthClientManager:
    def __init__(
        self,
        *,
        settings: AuthSettings,
        secret_provider: SecretProvider,
        client_factory: Callable[[AuthSettings], StarletteOAuth2App] = create_oauth_client,
        previous_secret_overlap: timedelta = DEFAULT_ENTRA_CLIENT_SECRET_OVERLAP,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if previous_secret_overlap < timedelta(0):
            raise ValueError("previous_secret_overlap must be >= 0.")
        self._settings = settings
        self._secret_provider = secret_provider
        self._client_factory = client_factory
        self._previous_secret_overlap = previous_secret_overlap
        self._clock = clock
        self._lock = asyncio.Lock()
        self._current_binding: _OAuthClientBinding | None = None
        self._previous_binding: _RetainedOAuthClientBinding | None = None

    async def get_client(self) -> StarletteOAuth2App:
        binding = await self._refresh_current_binding()
        return binding.client

    async def authorize_access_token(self, request: Request) -> dict[str, Any]:
        last_error: OAuthError | None = None
        for binding in await self._candidate_bindings():
            server_metadata = await binding.client.load_server_metadata()
            claims_options = build_claims_options(server_metadata.get("issuer"))
            try:
                return await binding.client.authorize_access_token(
                    request,
                    claims_options=claims_options,
                )
            except OAuthError as exc:
                if not _should_retry_with_previous_secret(exc) or last_error is not None:
                    raise
                last_error = exc

        if last_error is not None:
            raise last_error
        raise RuntimeError("No Entra OAuth client is available.")

    async def _candidate_bindings(self) -> tuple[_OAuthClientBinding, ...]:
        current_binding = await self._refresh_current_binding()
        async with self._lock:
            previous = self._previous_binding
            if previous is not None and previous.expires_at <= self._clock():
                self._previous_binding = None
                previous = None

            if previous is None:
                return (current_binding,)
            return (current_binding, previous.binding)

    async def _refresh_current_binding(self) -> _OAuthClientBinding:
        secret = await self._secret_provider.get_secret("ENTRA_CLIENT_SECRET")
        secret_key = _secret_cache_key(secret)

        async with self._lock:
            current = self._current_binding
            if current is not None and _secret_cache_key(current.secret) == secret_key:
                return current

            new_binding = _OAuthClientBinding(
                secret=secret,
                client=self._client_factory(self._settings.with_client_secret(secret.value)),
            )
            if current is not None:
                # Updating Key Vault to a new value does not create or retire the corresponding
                # Entra application credential. Keep the previous OAuth client around briefly so
                # active replicas can overlap old/new credentials during rotation, and only remove
                # the old credential in Entra after the rollout window has elapsed everywhere.
                self._previous_binding = _RetainedOAuthClientBinding(
                    binding=current,
                    expires_at=self._clock() + self._previous_secret_overlap,
                )
            self._current_binding = new_binding
            return new_binding


def build_auth_secret_error_response(exc: SecretProviderError) -> HTTPException:
    if isinstance(exc, (SecretNotFoundError, SecretVersionUnavailableError)):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured. Missing: ENTRA_CLIENT_SECRET",
        )
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Authentication is temporarily unavailable while loading the Entra client secret.",
    )


def build_claims_options(server_metadata_issuer: str | None) -> dict[str, dict[str, Any]] | None:
    issuer = _optional_string(server_metadata_issuer)
    if issuer is None:
        return None

    return {
        "iss": {
            "validate": lambda claims, value: validate_issuer_claim(
                claims,
                value,
                issuer,
            )
        }
    }


def validate_issuer_claim(
    claims: dict[str, Any],
    issuer: Any,
    server_metadata_issuer: str,
) -> bool:
    issuer_value = _optional_string(issuer)
    if issuer_value is None:
        return False

    if ENTRA_TENANT_ID_PLACEHOLDER not in server_metadata_issuer:
        return issuer_value == server_metadata_issuer

    tenant_id = _canonical_tenant_id(claims.get("tid"))
    if tenant_id is None:
        return False

    expected_issuer = server_metadata_issuer.replace(ENTRA_TENANT_ID_PLACEHOLDER, tenant_id)
    parsed_issuer = urlsplit(issuer_value)

    return (
        parsed_issuer.scheme == "https"
        and parsed_issuer.netloc == "login.microsoftonline.com"
        and not parsed_issuer.query
        and not parsed_issuer.fragment
        and parsed_issuer.path.rstrip("/") == f"/{tenant_id}/v2.0"
        and issuer_value == expected_issuer
    )


def get_session_user(request: Request) -> AuthenticatedUser | None:
    raw_user = request.session.get(AUTH_SESSION_KEY)
    if not isinstance(raw_user, dict) or "sub" not in raw_user or "owner_id" not in raw_user:
        return None

    return {
        "sub": str(raw_user["sub"]),
        "name": _optional_string(raw_user.get("name")),
        "email": _optional_string(raw_user.get("email")),
        "tenant_id": _optional_string(raw_user.get("tenant_id")),
        "object_id": _optional_string(raw_user.get("object_id")),
        "owner_id": str(raw_user["owner_id"]),
    }


def get_authenticated_owner(request: Request) -> AuthenticatedOwner | None:
    user = get_session_user(request)
    if user is None:
        return None
    return AuthenticatedOwner(
        owner_id=user["owner_id"],
        tenant_id=user["tenant_id"],
        object_id=user["object_id"],
        subject=user["sub"],
        display_name=user["name"],
        email=user["email"],
    )


def require_authenticated_user(request: Request) -> AuthenticatedUser:
    user = get_session_user(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/auth/login"},
            detail="Authentication required.",
        )
    return user


def require_api_user(request: Request) -> AuthenticatedUser:
    user = get_session_user(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Session"},
            detail="Authentication required.",
        )
    return user


def extract_user_claims(claims: dict[str, Any]) -> AuthenticatedUser:
    subject = claims.get("sub")
    if not subject:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ID token did not include a subject claim.",
        )

    email = _optional_string(claims.get("email")) or _optional_string(
        claims.get("preferred_username")
    )
    if email is None:
        emails = claims.get("emails")
        if isinstance(emails, list) and emails:
            email = _optional_string(emails[0])

    tenant_id = _canonical_tenant_id(claims.get("tid"))
    object_id = _canonical_tenant_id(claims.get("oid"))
    owner_id = _derive_owner_id(subject=str(subject), tenant_id=tenant_id, object_id=object_id)

    return {
        "sub": str(subject),
        "name": _optional_string(claims.get("name")) or email,
        "email": email,
        "tenant_id": tenant_id,
        "object_id": object_id,
        "owner_id": owner_id,
    }


def build_logout_redirect_target(settings: AuthSettings) -> str:
    if settings.logout_url and settings.post_logout_redirect_uri:
        query = urlencode({"post_logout_redirect_uri": settings.post_logout_redirect_uri})
        return f"{settings.logout_url}?{query}"
    return "/"


def _derive_owner_id(*, subject: str, tenant_id: str | None, object_id: str | None) -> str:
    if tenant_id and object_id:
        return f"{tenant_id}:{object_id}"
    if tenant_id:
        return f"{tenant_id}:sub:{subject}"
    return f"sub:{subject}"


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _canonical_tenant_id(value: Any) -> str | None:
    tenant_id = _optional_string(value)
    if tenant_id is None:
        return None

    try:
        return str(UUID(tenant_id))
    except ValueError:
        return None


def _first_env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def _secret_cache_key(secret: SecretValue) -> tuple[str | None, str]:
    return (secret.version, secret.value)


def _should_retry_with_previous_secret(error: OAuthError) -> bool:
    return _optional_string(getattr(error, "error", None)) == "invalid_client"
