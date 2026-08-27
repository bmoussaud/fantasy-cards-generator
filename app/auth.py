from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, TypedDict
from urllib.parse import urlencode, urlsplit, urlunsplit

from authlib.integrations.starlette_client import OAuth, StarletteOAuth2App
from fastapi import HTTPException, Request, status

AUTH_SESSION_KEY = "user"
AUTH_NONCE_SESSION_KEY = "auth_nonce"
DEFAULT_ENTRA_AUTHORITY = "https://login.microsoftonline.com/organizations/v2.0"


class AuthenticatedUser(TypedDict):
    sub: str
    name: str | None
    email: str | None


@dataclass(frozen=True)
class AuthSettings:
    client_id: str | None
    client_secret: str | None
    authority: str | None
    redirect_uri: str | None
    post_logout_redirect_uri: str | None
    session_secret_key: str
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

    def missing_required(self) -> tuple[str, ...]:
        missing: list[str] = []
        if not self.client_id:
            missing.append("ENTRA_CLIENT_ID")
        if not self.client_secret:
            missing.append("ENTRA_CLIENT_SECRET")
        if not self.redirect_uri:
            missing.append("ENTRA_REDIRECT_URI")
        return tuple(missing)


def load_auth_settings() -> AuthSettings:
    session_secret_key = os.getenv("APP_SESSION_SECRET_KEY")
    if not session_secret_key:
        raise RuntimeError("APP_SESSION_SECRET_KEY must be set before starting the application.")

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


def ensure_auth_configured(settings: AuthSettings) -> None:
    missing = settings.missing_required()
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


def get_session_user(request: Request) -> AuthenticatedUser | None:
    raw_user = request.session.get(AUTH_SESSION_KEY)
    if not isinstance(raw_user, dict) or "sub" not in raw_user:
        return None

    return {
        "sub": str(raw_user["sub"]),
        "name": _optional_string(raw_user.get("name")),
        "email": _optional_string(raw_user.get("email")),
    }


def require_authenticated_user(request: Request) -> AuthenticatedUser:
    user = get_session_user(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/auth/login"},
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

    return {
        "sub": str(subject),
        "name": _optional_string(claims.get("name")) or email,
        "email": email,
    }


def build_logout_redirect_target(settings: AuthSettings) -> str:
    if settings.logout_url and settings.post_logout_redirect_uri:
        query = urlencode({"post_logout_redirect_uri": settings.post_logout_redirect_uri})
        return f"{settings.logout_url}?{query}"
    return "/"


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default
