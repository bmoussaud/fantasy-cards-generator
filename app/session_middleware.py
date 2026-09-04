from __future__ import annotations

import json
import logging
import os
from base64 import b64decode, b64encode
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

import itsdangerous
from itsdangerous.exc import BadSignature
from starlette.datastructures import MutableHeaders
from starlette.middleware.sessions import Session
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.secrets import (
    Clock,
    SecretProvider,
    SecretProviderError,
    SecretValue,
    SecretVersion,
    SecretVersionUnavailableError,
    classify_secret_error,
    get_secret_version,
    list_secret_versions,
    utc_now,
)

SESSION_SECRET_NAME = "APP_SESSION_SECRET_KEY"
DEFAULT_SESSION_SIGNING_KEY_OVERLAP_SECONDS = 3600.0
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SessionCookieSettings:
    signing_key_overlap: timedelta = timedelta(seconds=DEFAULT_SESSION_SIGNING_KEY_OVERLAP_SECONDS)


@dataclass(frozen=True, slots=True)
class SessionSigningKeys:
    current: SecretValue
    previous: SecretValue | None = None


def load_session_cookie_settings(environ: dict[str, str] | None = None) -> SessionCookieSettings:
    values = os.environ if environ is None else environ
    raw_overlap = values.get("SESSION_SIGNING_KEY_OVERLAP_SECONDS")
    overlap_seconds = (
        DEFAULT_SESSION_SIGNING_KEY_OVERLAP_SECONDS
        if raw_overlap in (None, "")
        else float(raw_overlap)
    )
    if overlap_seconds < 0:
        raise ValueError("SESSION_SIGNING_KEY_OVERLAP_SECONDS must be >= 0.")
    return SessionCookieSettings(signing_key_overlap=timedelta(seconds=overlap_seconds))


async def load_session_signing_keys(
    provider: SecretProvider,
    *,
    overlap_window: timedelta,
    clock: Clock = utc_now,
) -> SessionSigningKeys:
    current_secret = await provider.get_secret(SESSION_SECRET_NAME)
    if overlap_window <= timedelta(0) or current_secret.version is None:
        return SessionSigningKeys(current=current_secret)

    try:
        versions = await list_secret_versions(provider, SESSION_SECRET_NAME)
    except SecretProviderError as exc:
        LOGGER.warning(
            "session.previous_key_resolution_degraded",
            extra={"error_category": classify_secret_error(exc)},
        )
        return SessionSigningKeys(current=current_secret)

    current_metadata: SecretVersion | None = None
    current_index: int | None = None
    for index, version in enumerate(versions):
        if version.enabled and version.version == current_secret.version:
            current_metadata = version
            current_index = index
            break

    if current_metadata is None or current_index is None:
        LOGGER.warning(
            "session.previous_key_resolution_degraded",
            extra={"error_category": "version_metadata_missing"},
        )
        return SessionSigningKeys(current=current_secret)

    previous_secret: SecretValue | None = None
    previous_metadata = versions[current_index + 1] if current_index + 1 < len(versions) else None
    if _allows_previous_version(
        current=current_metadata,
        previous=previous_metadata,
        overlap_window=overlap_window,
        clock=clock,
    ):
        try:
            previous_secret = await get_secret_version(
                provider,
                SESSION_SECRET_NAME,
                version=previous_metadata.version if previous_metadata else None,
            )
        except SecretVersionUnavailableError:
            previous_secret = None

    return SessionSigningKeys(current=current_secret, previous=previous_secret)


def _allows_previous_version(
    *,
    current: SecretVersion,
    previous: SecretVersion | None,
    overlap_window: timedelta,
    clock: Clock,
) -> bool:
    if previous is None or previous.enabled is False or overlap_window <= timedelta(0):
        return False

    rotation_started_at = current.activated_on
    if rotation_started_at is None:
        return False

    # Once the overlap window closes — or the direct predecessor version is disabled —
    # cookies signed with that predecessor stop authenticating and the user must sign in again.
    return clock() <= rotation_started_at + overlap_window


class RotatingSessionMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        session_cookie: str = "session",
        max_age: int | None = 14 * 24 * 60 * 60,
        path: str = "/",
        same_site: Literal["lax", "strict", "none"] = "lax",
        https_only: bool = False,
        domain: str | None = None,
        secret_provider_state_key: str = "secret_provider",
        signing_key_overlap: timedelta = timedelta(
            seconds=DEFAULT_SESSION_SIGNING_KEY_OVERLAP_SECONDS
        ),
        clock: Clock = utc_now,
    ) -> None:
        self.app = app
        self.session_cookie = session_cookie
        self.max_age = max_age
        self.path = path
        self.secret_provider_state_key = secret_provider_state_key
        self.signing_key_overlap = signing_key_overlap
        self.clock = clock
        self.security_flags = "httponly; samesite=" + same_site
        if https_only:
            self.security_flags += "; secure"
        if domain is not None:
            self.security_flags += f"; domain={domain}"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):  # pragma: no cover
            await self.app(scope, receive, send)
            return

        provider = self._secret_provider(scope)
        signing_keys = await load_session_signing_keys(
            provider,
            overlap_window=self.signing_key_overlap,
            clock=self.clock,
        )

        connection = HTTPConnection(scope)
        initial_session_was_empty = True

        if self.session_cookie in connection.cookies:
            data = connection.cookies[self.session_cookie].encode("utf-8")
            session_data = self._load_session(signing_keys, data)
            if session_data is None:
                scope["session"] = Session()
            else:
                scope["session"] = Session(session_data)
                initial_session_was_empty = False
        else:
            scope["session"] = Session()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                session: Session = scope["session"]
                headers = MutableHeaders(scope=message)
                if session.accessed:
                    headers.add_vary_header("Cookie")
                if session.modified and session:
                    refreshed_keys = await load_session_signing_keys(
                        provider,
                        overlap_window=self.signing_key_overlap,
                        clock=self.clock,
                    )
                    data = b64encode(json.dumps(session).encode("utf-8"))
                    signed = self._signer(refreshed_keys.current.value).sign(data)
                    header_value = (
                        "{session_cookie}={data}; path={path}; {max_age}{security_flags}"
                    ).format(
                        session_cookie=self.session_cookie,
                        data=signed.decode("utf-8"),
                        path=self.path,
                        max_age=f"Max-Age={self.max_age}; " if self.max_age else "",
                        security_flags=self.security_flags,
                    )
                    headers.append("Set-Cookie", header_value)
                elif session.modified and not initial_session_was_empty:
                    header_value = (
                        "{session_cookie}={data}; path={path}; {expires}{security_flags}"
                    ).format(
                        session_cookie=self.session_cookie,
                        data="null",
                        path=self.path,
                        expires="expires=Thu, 01 Jan 1970 00:00:00 GMT; ",
                        security_flags=self.security_flags,
                    )
                    headers.append("Set-Cookie", header_value)
            await send(message)

        await self.app(scope, receive, send_wrapper)

    def _secret_provider(self, scope: Scope) -> SecretProvider:
        app = scope.get("app")
        if app is None or not hasattr(app.state, self.secret_provider_state_key):
            raise RuntimeError("Application secret provider is not available.")
        return getattr(app.state, self.secret_provider_state_key)

    def _load_session(
        self,
        signing_keys: SessionSigningKeys,
        data: bytes,
    ) -> dict[str, object] | None:
        for secret in (signing_keys.current, signing_keys.previous):
            if secret is None:
                continue
            try:
                unsigned = self._signer(secret.value).unsign(data, max_age=self.max_age)
                return json.loads(b64decode(unsigned))
            except BadSignature:
                continue
        return None

    @staticmethod
    def _signer(secret_key: str) -> itsdangerous.TimestampSigner:
        return itsdangerous.TimestampSigner(secret_key)
