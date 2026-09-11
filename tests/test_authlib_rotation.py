from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from dataclasses import replace
from datetime import timedelta
from urllib.parse import parse_qs, urlencode

import httpx
import pytest
from authlib.integrations.base_client.errors import MismatchingStateError, OAuthError
from joserfc import jwt
from joserfc.errors import InvalidClaimError
from joserfc.jwk import RSAKey
from starlette.requests import Request

from app.auth import (
    EntraOAuthClientManager,
    RotatingStarletteOAuth2App,
    create_oauth_client,
    load_auth_settings,
)
from app.secrets import AzureSecretProvider
from tests.test_secrets import FakeClock, FakeSecretBundle, FakeSecretClient, make_version

NAME = "ENTRA_CLIENT_SECRET"
VAULT_NAME = "entra-client-secret"
ISSUER = "https://issuer.example/tenant/v2.0"


def request(session: dict, **params: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "server": ("testserver", 443),
            "path": "/auth/callback",
            "query_string": urlencode(params).encode(),
            "headers": [],
            "session": session,
        }
    )


class CallbackHarness:
    """Real Authlib discovery, state, HTTP exchange and signed ID-token validation."""

    def __init__(self, key: RSAKey):
        self.key = key
        self.clock = FakeClock()
        self.old = make_version(self.clock, "v1", age_seconds=10)
        self.kv = FakeSecretClient(versions={VAULT_NAME: [self.old]})
        self.kv.set_response(VAULT_NAME, None, FakeSecretBundle("old-synthetic", self.old))
        self.provider = AzureSecretProvider(
            key_vault_uri="https://vault.example",
            client=self.kv,
            clock=self.clock.now,
            max_retries=0,
        )
        self.codes: dict[str, dict[str, list[str]]] = {}
        self.exchanges: list[tuple[str, dict[str, list[str]]]] = []
        self.clients: list[RotatingStarletteOAuth2App] = []
        self.current_error: str | None = "invalid_client"
        self.previous_error: str | None = None
        self.token_nonce: str | None = None
        self.token_issuer = ISSUER
        self.expire_during_exchange = False
        self.revoke_during_exchange = False
        self.network_error = False
        self.expire_during_discovery = False
        self.exchange_gate: asyncio.Event | None = None
        self.current_started = asyncio.Event()
        self.transport = httpx.MockTransport(self.handle)
        self.manager = EntraOAuthClientManager(
            settings=load_auth_settings(),
            secret_provider=self.provider,
            client_factory=self.factory,
            clock=self.clock.now,
            previous_secret_overlap=timedelta(seconds=30),
        )

    def factory(self, settings):
        client = create_oauth_client(settings)
        assert isinstance(client, RotatingStarletteOAuth2App)
        client.client_kwargs["transport"] = self.transport
        self.clients.append(client)
        return client

    async def handle(self, req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("openid-configuration"):
            if self.expire_during_discovery:
                self.clock.advance(30)
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": "https://issuer.example/authorize",
                    "token_endpoint": "https://issuer.example/token",
                    "jwks_uri": "https://issuer.example/jwks",
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
            )
        if req.url.path == "/jwks":
            return httpx.Response(200, json={"keys": [self.key.as_dict(private=False)]})
        assert req.url == "https://issuer.example/token"
        body = parse_qs(req.content.decode())
        assert set(body) == {"grant_type", "code", "redirect_uri", "code_verifier"}
        assert body["grant_type"] == ["authorization_code"]
        secret = base64.b64decode(req.headers["authorization"].split()[1]).decode().split(":")[1]
        self.exchanges.append((secret, body))
        if self.network_error:
            raise httpx.ConnectError("synthetic transport failure", request=req)
        if secret == "new-synthetic":
            self.current_started.set()
            if self.exchange_gate is not None:
                await self.exchange_gate.wait()
            if self.expire_during_exchange:
                self.clock.advance(30)
            if self.revoke_during_exchange:
                self.kv._versions[VAULT_NAME][1] = replace(self.old, enabled=False)
                await self.provider.refresh_secret(NAME)
            if self.current_error is not None:
                return httpx.Response(401, json={"error": self.current_error})
        elif self.previous_error is not None:
            return httpx.Response(401, json={"error": self.previous_error})
        code = body["code"][0]
        login = self.codes[code]
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(body["code_verifier"][0].encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        assert login["code_challenge"] == [challenge]
        assert login["code_challenge_method"] == ["S256"]
        assert body["redirect_uri"] == ["https://testserver/auth/callback"]
        now = int(time.time())
        id_token = jwt.encode(
            {"alg": "RS256", "kid": self.key.kid},
            {
                "iss": self.token_issuer,
                "sub": "synthetic-user",
                "aud": "client-id",
                "iat": now,
                "exp": now + 300,
                "nonce": self.token_nonce or login["nonce"][0],
            },
            self.key,
        )
        return httpx.Response(
            200,
            json={
                "access_token": "synthetic-access",
                "token_type": "Bearer",
                "id_token": id_token,
            },
        )

    async def login(self, code: str = "synthetic-code") -> Request:
        session: dict = {}
        client = await self.manager.get_client()
        redirect = await client.authorize_redirect(
            request(session), "https://testserver/auth/callback", nonce=f"nonce-{code}"
        )
        params = parse_qs(httpx.URL(redirect.headers["location"]).query.decode())
        self.codes[code] = params
        return request(session, code=code, state=params["state"][0])

    async def rotate(self):
        self.new = make_version(self.clock, "v2")
        self.kv._versions[VAULT_NAME] = [self.new, self.old]
        self.kv.set_response(VAULT_NAME, None, FakeSecretBundle("new-synthetic", self.new))
        self.kv.set_response(VAULT_NAME, "v1", FakeSecretBundle("old-synthetic", self.old))
        await self.provider.refresh_secret(NAME)
        await self.manager.get_client()


@pytest.fixture(scope="module")
def signing_key():
    return RSAKey.generate_key(parameters={"kid": "synthetic-test-key"})


@pytest.mark.parametrize("rotate", [False, True])
def test_real_callback_initial_success_and_previous_retry(signing_key, rotate):
    async def scenario():
        h = CallbackHarness(signing_key)
        callback = await h.login()
        if rotate:
            await h.rotate()
        token = await h.manager.authorize_access_token(callback)
        assert token["userinfo"]["sub"] == "synthetic-user"
        assert not callback.session
        assert [s for s, _ in h.exchanges] == (
            ["new-synthetic", "old-synthetic"] if rotate else ["old-synthetic"]
        )
        if rotate:
            assert h.exchanges[0][1] == h.exchanges[1][1]
        with pytest.raises(MismatchingStateError):
            await h.manager.authorize_access_token(callback)
        assert len(h.exchanges) == (2 if rotate else 1)
        assert [c.client_secret for c in h.clients] == (
            ["old-synthetic", "new-synthetic"] if rotate else ["old-synthetic"]
        )
        await h.provider.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("missing_timestamp", [False, True])
@pytest.mark.parametrize("current_error", [None, "invalid_client"])
def test_real_callback_three_version_history_requires_proven_predecessor(
    signing_key, missing_timestamp, current_error
):
    async def scenario():
        h = CallbackHarness(signing_key)
        callback = await h.login()
        newest = make_version(h.clock, "v3")
        middle = make_version(h.clock, "v2", age_seconds=5)
        if missing_timestamp:
            middle = replace(middle, created_on=None)
        h.kv._versions[VAULT_NAME] = [newest, middle, h.old]
        h.kv.set_response(VAULT_NAME, None, FakeSecretBundle("new-synthetic", newest))
        h.kv.set_response(VAULT_NAME, "v2", FakeSecretBundle("middle-synthetic", middle))
        h.kv.set_response(VAULT_NAME, "v1", FakeSecretBundle("old-synthetic", h.old))
        await h.provider.refresh_secret(NAME)
        h.current_error = current_error
        snapshot = h.provider.snapshot_for_read(NAME)
        assert snapshot.current.version == "v3"
        assert (snapshot.previous.version if snapshot.previous else None) == (
            None if missing_timestamp else "v2"
        )
        if missing_timestamp and current_error:
            with pytest.raises(OAuthError, match="invalid_client"):
                await h.manager.authorize_access_token(callback)
        else:
            token = await h.manager.authorize_access_token(callback)
            assert token["userinfo"]["sub"] == "synthetic-user"
        assert not callback.session
        assert [secret for secret, _ in h.exchanges] == (
            ["new-synthetic", "middle-synthetic"]
            if current_error and not missing_timestamp
            else ["new-synthetic"]
        )
        assert (VAULT_NAME, "v1") not in h.kv.get_calls
        await h.provider.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["state", "nonce", "issuer"])
def test_real_callback_preserves_state_nonce_and_issuer_validation(signing_key, failure):
    async def scenario():
        h = CallbackHarness(signing_key)
        callback = await h.login()
        await h.rotate()
        if failure == "state":
            callback = request(callback.session, code="synthetic-code", state="wrong")
            error = MismatchingStateError
        else:
            error = InvalidClaimError
            if failure == "nonce":
                h.token_nonce = "incorrect-nonce"
            else:
                h.token_issuer = "https://wrong.example"
        with pytest.raises(error):
            await h.manager.authorize_access_token(callback)
        assert len(h.exchanges) == (0 if failure == "state" else 2)
        if failure != "state":
            assert not callback.session
        await h.provider.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("error", ["invalid_grant", "access_denied", "server_error", "network"])
def test_real_callback_does_not_retry_other_exchange_errors(signing_key, error):
    async def scenario():
        h = CallbackHarness(signing_key)
        callback = await h.login()
        await h.rotate()
        h.current_error = error
        h.network_error = error == "network"
        with pytest.raises(httpx.ConnectError if h.network_error else OAuthError):
            await h.manager.authorize_access_token(callback)
        assert len(h.exchanges) == 1
        assert not callback.session
        await h.provider.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["overlap", "expiry", "revocation", "already_elapsed"])
def test_real_callback_rechecks_fallback_eligibility_before_exchange(signing_key, boundary):
    async def scenario():
        h = CallbackHarness(signing_key)
        callback = await h.login()
        if boundary == "expiry":
            h.old = replace(h.old, expires_on=h.clock.now() + timedelta(seconds=30))
        await h.rotate()
        if boundary in ("overlap", "expiry"):
            h.expire_during_exchange = True
        elif boundary == "revocation":
            h.revoke_during_exchange = True
        else:
            h.clock.advance(30)
        with pytest.raises(OAuthError, match="invalid_client"):
            await h.manager.authorize_access_token(callback)
        assert len(h.exchanges) == 1
        await h.provider.aclose()

    asyncio.run(scenario())


def test_real_concurrent_callbacks_do_not_mutate_shared_clients_or_reuse_state(signing_key):
    async def scenario():
        h = CallbackHarness(signing_key)
        callbacks = [await h.login(f"code-{i}") for i in range(4)]
        await h.rotate()
        h.exchange_gate = asyncio.Event()
        tasks = [asyncio.create_task(h.manager.authorize_access_token(r)) for r in callbacks]
        await h.current_started.wait()
        with pytest.raises(MismatchingStateError):
            await h.manager.authorize_access_token(callbacks[0])
        h.exchange_gate.set()
        tokens = await asyncio.gather(*tasks)
        assert all(t["userinfo"]["sub"] == "synthetic-user" for t in tokens)
        assert len(h.exchanges) == 8
        for code in h.codes:
            attempts = [(s, b) for s, b in h.exchanges if b["code"] == [code]]
            assert [s for s, _ in attempts] == ["new-synthetic", "old-synthetic"]
            assert attempts[0][1] == attempts[1][1]
        assert [c.client_secret for c in h.clients] == ["old-synthetic", "new-synthetic"]
        await h.provider.aclose()

    asyncio.run(scenario())


def test_real_callback_retries_invalid_client_only_once(signing_key):
    async def scenario():
        h = CallbackHarness(signing_key)
        callback = await h.login()
        await h.rotate()
        h.previous_error = "invalid_client"
        with pytest.raises(OAuthError, match="invalid_client"):
            await h.manager.authorize_access_token(callback)
        assert len(h.exchanges) == 2
        assert not callback.session
        await h.provider.aclose()

    asyncio.run(scenario())


def test_real_callback_rechecks_overlap_after_previous_client_discovery(signing_key):
    async def scenario():
        h = CallbackHarness(signing_key)
        callback = await h.login()
        await h.rotate()
        await h.clients[1].load_server_metadata()
        h.clients[0].server_metadata.pop("_loaded_at")
        h.expire_during_discovery = True
        with pytest.raises(OAuthError, match="invalid_client"):
            await h.manager.authorize_access_token(callback)
        assert len(h.exchanges) == 1
        await h.provider.aclose()

    asyncio.run(scenario())


def test_current_success_with_invalid_id_token_never_retries_previous(signing_key):
    async def scenario():
        h = CallbackHarness(signing_key)
        callback = await h.login()
        await h.rotate()
        h.current_error = None
        h.token_nonce = "incorrect-nonce"
        with pytest.raises(InvalidClaimError):
            await h.manager.authorize_access_token(callback)
        assert len(h.exchanges) == 1
        assert not callback.session
        await h.provider.aclose()

    asyncio.run(scenario())
