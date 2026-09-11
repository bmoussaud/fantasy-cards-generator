from __future__ import annotations

import asyncio
import json
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from itertools import permutations

import httpx
import pytest
from azure.core.exceptions import ServiceRequestError

from app import main as main_module
from app.auth import EntraOAuthClientManager, load_auth_settings
from app.secrets import (
    DEFAULT_SECRET_REFRESH_INTERVAL_SECONDS,
    DEFAULT_SECRET_REFRESH_TIMEOUT_SECONDS,
    MAX_SECRET_SNAPSHOTS,
    AzureSecretProvider,
    SecretProviderError,
    SecretRefreshTimeout,
    SecretStaleValueExpiredError,
    SecretVersionUnavailableError,
    run_secret_refresh_worker,
)
from app.session_middleware import load_session_signing_keys
from tests.test_secrets import (
    FakeAsyncCredential,
    FakeClock,
    FakeSecretBundle,
    FakeSecretClient,
    make_version,
)
from tests.test_session_middleware import (
    assert_cookie_is_signed_with,
    build_session_app,
    make_signed_session_cookie,
)

NAME = "APP_SESSION_SECRET_KEY"
VAULT_NAME = "app-session-secret-key"


class ControlledSleep:
    def __init__(self):
        self.calls: asyncio.Queue = asyncio.Queue()

    async def __call__(self, delay):
        future = asyncio.get_running_loop().create_future()
        await self.calls.put((delay, future))
        await future

    async def waiting(self):
        return await asyncio.wait_for(self.calls.get(), timeout=1)


def pair_provider(*, clock=None, **kwargs):
    clock = clock or FakeClock()
    current = make_version(clock, "v2")
    previous = make_version(clock, "v1", age_seconds=10)
    client = FakeSecretClient(versions={VAULT_NAME: [current, previous]})
    client.set_response(VAULT_NAME, None, FakeSecretBundle("current-synthetic", current))
    client.set_response(VAULT_NAME, "v1", FakeSecretBundle("previous-synthetic", previous))
    provider = AzureSecretProvider(
        key_vault_uri="https://vault.example",
        client=client,
        clock=clock.now,
        max_retries=0,
        **kwargs,
    )
    return clock, client, provider


def test_mixed_readers_and_session_requests_share_one_bounded_snapshot():
    async def scenario():
        clock, client, provider = pair_provider()
        results = await asyncio.gather(
            *[
                operation()
                for _ in range(10)
                for operation in (
                    lambda: provider.get_secret(NAME),
                    lambda: provider.get_secret_version(NAME, version="v1"),
                    lambda: provider.list_secret_versions(NAME),
                    lambda: load_session_signing_keys(
                        provider, overlap_window=timedelta(seconds=60), clock=clock.now
                    ),
                    lambda: provider.refresh_secret(NAME),
                )
            ]
        )
        assert len(results) == 50
        initial = provider.snapshot_for_read(NAME)
        with pytest.raises(FrozenInstanceError):
            initial.previous = None
        clock.advance(10)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=build_session_app(provider=provider, clock=clock)),
            base_url="https://testserver",
        ) as http:
            cookie = make_signed_session_cookie("previous-synthetic", {"user": "synthetic"})
            reads = await asyncio.gather(
                *[
                    http.get("/session", headers={"cookie": f"fantasy_cards_session={cookie}"})
                    for _ in range(10)
                ]
            )
            writes = await asyncio.gather(*[http.post("/session/synthetic") for _ in range(10)])
        assert all(r.json() == {"user": "synthetic"} for r in reads)
        for response in writes:
            assert_cookie_is_signed_with(
                response.cookies["fantasy_cards_session"], "current-synthetic"
            )
        assert client.get_calls == [(VAULT_NAME, None), (VAULT_NAME, "v1")]
        assert client.list_calls == [VAULT_NAME]
        assert provider.snapshot_for_read(NAME) == initial
        await provider.aclose()

    asyncio.run(scenario())


def test_independently_cached_idle_replicas_adopt_session_and_entra_without_requests():
    async def scenario():
        clock = FakeClock()
        replicas = []
        workers = []
        pending = []
        for _ in range(2):
            old = make_version(clock, "v1", age_seconds=10)
            kv = FakeSecretClient(versions={VAULT_NAME: [old], "entra-client-secret": [old]})
            for name in (VAULT_NAME, "entra-client-secret"):
                kv.set_response(name, None, FakeSecretBundle("old-synthetic", old))
            provider = AzureSecretProvider(
                key_vault_uri="https://vault.example", client=kv, clock=clock.now
            )
            manager = EntraOAuthClientManager(
                settings=load_auth_settings(),
                secret_provider=provider,
                clock=clock.now,
                previous_secret_overlap=timedelta(seconds=30),
            )
            await provider.get_secret(NAME)
            await manager.get_client()
            replicas.append((kv, provider, manager))
            for name, callback in ((NAME, None), ("ENTRA_CLIENT_SECRET", manager.get_client)):
                sleep = ControlledSleep()
                workers.append(
                    asyncio.create_task(
                        run_secret_refresh_worker(
                            provider,
                            name,
                            on_refresh=callback,
                            sleep=sleep,
                            monotonic=lambda: clock.now().timestamp(),
                        )
                    )
                )
                delay, tick = await sleep.waiting()
                assert delay == 20
                pending.append((sleep, tick))
        clock.advance(20)
        for kv, _, _ in replicas:
            new = make_version(clock, "v2")
            for name in (VAULT_NAME, "entra-client-secret"):
                old = kv._versions[name][0]
                kv._versions[name] = [new, old]
                kv.set_response(name, None, FakeSecretBundle("new-synthetic", new))
                kv.set_response(name, "v1", FakeSecretBundle("old-synthetic", old))
        for _, tick in pending:
            tick.set_result(None)
        for sleep, _ in pending:
            delay, _ = await sleep.waiting()
            assert delay == 20
        for kv, provider, manager in replicas:
            assert provider.snapshot_for_read(NAME).current.version == "v2"
            assert manager._current_binding.secret.version == "v2"
            assert manager._previous_binding.binding.secret.version == "v1"
            assert kv.list_calls.count(VAULT_NAME) == 2
            assert kv.list_calls.count("entra-client-secret") == 2
        assert replicas[0][1]._cache is not replicas[1][1]._cache
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(
                    app=build_session_app(provider=replicas[0][1], clock=clock, overlap_seconds=60)
                ),
                base_url="https://replica-a.test",
            ) as replica_a,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(
                    app=build_session_app(provider=replicas[1][1], clock=clock, overlap_seconds=60)
                ),
                base_url="https://replica-b.test",
            ) as replica_b,
        ):
            response = await replica_a.post("/session/synthetic")
            cookie = response.cookies["fantasy_cards_session"]
            assert_cookie_is_signed_with(cookie, "new-synthetic")
            read = await replica_b.get(
                "/session", headers={"cookie": f"fantasy_cards_session={cookie}"}
            )
            assert read.json() == {"user": "synthetic"}
            old_cookie = make_signed_session_cookie("old-synthetic", {"user": "synthetic"})
            read = await replica_b.get(
                "/session", headers={"cookie": f"fantasy_cards_session={old_cookie}"}
            )
            assert read.json() == {"user": "synthetic"}
            clock.advance(60)
            rejected = await replica_b.get(
                "/session", headers={"cookie": f"fantasy_cards_session={old_cookie}"}
            )
            assert rejected.json() == {"user": None}
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        for _, provider, _ in replicas:
            await provider.aclose()

    asyncio.run(scenario())


def test_metadata_success_does_not_extend_value_age_or_stale_deadline():
    async def scenario():
        clock, kv, provider = pair_provider(cache_ttl=10, max_stale=20)
        original = await provider.get_snapshot(NAME)
        clock.advance(11)
        kv.set_response(VAULT_NAME, None, ServiceRequestError("synthetic-outage"))
        values = await asyncio.gather(*[provider.get_secret(NAME) for _ in range(20)])
        assert all(v == original.current for v in values)
        assert provider._cache[NAME].metadata_fetched_at == clock.now()
        assert provider._cache[NAME].current.fetched_at == original.current.fetched_at
        counts = len(kv.get_calls), len(kv.list_calls)
        assert counts == (3, 2)
        for _ in range(20):
            assert await provider.get_secret(NAME) == original.current
            assert await provider.get_secret_version(NAME, version="v1") == original.previous
        assert (len(kv.get_calls), len(kv.list_calls)) == counts
        clock.advance(4.999)
        await provider.get_secret(NAME)
        assert (len(kv.get_calls), len(kv.list_calls)) == counts
        clock.advance(0.001)
        await provider.get_secret(NAME)
        assert len(kv.list_calls) == counts[1] + 1
        clock.advance(13.999)
        assert (await provider.get_secret(NAME)).value == "current-synthetic"
        clock.advance(0.001)
        with pytest.raises(SecretStaleValueExpiredError):
            await provider.get_secret(NAME)
        await provider.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", ["disabled", "expired", "future"])
def test_negative_metadata_survives_partial_pagination_failure(invalid):
    async def scenario():
        clock, kv, provider = pair_provider()
        await provider.get_snapshot(NAME)
        previous = kv._versions[VAULT_NAME][1]
        changes = {
            "disabled": {"enabled": False},
            "expired": {"expires_on": clock.now()},
            "future": {"not_before": clock.now() + timedelta(seconds=10)},
        }

        async def broken_pages(_):
            yield replace(previous, **changes[invalid])
            raise ServiceRequestError("synthetic-page-error")

        kv.list_properties_of_secret_versions = broken_pages
        assert (await provider.refresh_secret(NAME)).version == "v2"
        assert provider.snapshot_for_read(NAME).previous is None
        with pytest.raises(SecretVersionUnavailableError):
            await provider.get_secret_version(NAME, version="v1")
        await provider.aclose()

    asyncio.run(scenario())


def test_observed_invalid_new_latest_fails_closed_even_when_next_page_fails():
    async def scenario():
        clock, kv, provider = pair_provider()
        await provider.get_secret(NAME)
        clock.advance(1)
        latest = make_version(clock, "v3", enabled=False)

        async def broken_pages(_):
            yield latest
            raise ServiceRequestError("synthetic-page-error")

        kv.list_properties_of_secret_versions = broken_pages
        with pytest.raises(SecretProviderError):
            await provider.refresh_secret(NAME)
        with pytest.raises(SecretProviderError):
            await provider.get_secret(NAME)
        assert provider._cache[NAME].current is None
        await provider.aclose()

    asyncio.run(scenario())


def test_previous_fetch_failure_retains_only_original_eligible_predecessor():
    async def scenario():
        clock, kv, provider = pair_provider(cache_ttl=10, max_stale=20)
        initial = await provider.get_snapshot(NAME)
        clock.advance(11)
        kv.set_response(VAULT_NAME, "v1", ServiceRequestError("synthetic-outage"))
        refreshed = await provider.get_snapshot(NAME)
        assert refreshed.current.fetched_at == clock.now()
        assert refreshed.previous == initial.previous
        clock.advance(19)
        refreshed = await provider.get_snapshot(NAME)
        assert refreshed.current.fetched_at == clock.now()
        assert refreshed.previous is None
        assert len(provider._cache[NAME].versions) == 2
        await provider.aclose()

    asyncio.run(scenario())


def test_new_metadata_with_failed_value_fetch_does_not_retry_current_as_its_own_predecessor():
    async def scenario():
        clock, kv, provider = pair_provider()
        initial = await provider.get_snapshot(NAME)
        clock.advance(1)
        newest = make_version(clock, "v3")
        kv._versions[VAULT_NAME] = [newest, kv._versions[VAULT_NAME][0]]
        kv.set_response(VAULT_NAME, None, ServiceRequestError("synthetic-outage"))
        await provider.refresh_secret(NAME)
        snapshot = await provider.get_snapshot(NAME)
        assert snapshot.current == initial.current
        assert snapshot.previous is None
        await provider.aclose()

    asyncio.run(scenario())


def test_metadata_validity_and_overlap_are_intersected_at_exact_read_boundaries():
    async def scenario():
        clock, kv, provider = pair_provider()
        kv._versions[VAULT_NAME][1] = replace(
            kv._versions[VAULT_NAME][1], expires_on=clock.now() + timedelta(seconds=10)
        )
        await provider.get_secret(NAME)
        clock.advance(9.999)
        keys = await load_session_signing_keys(
            provider, overlap_window=timedelta(seconds=10), clock=clock.now
        )
        assert keys.previous is not None
        clock.advance(0.001)
        keys = await load_session_signing_keys(
            provider, overlap_window=timedelta(seconds=10), clock=clock.now
        )
        assert keys.previous is None
        assert len(kv.list_calls) == 1
        assert len(kv.get_calls) == 2
        await provider.aclose()

    asyncio.run(scenario())


def test_unknown_predecessor_is_logged_current_only_not_historical_fallback(caplog):
    async def scenario():
        clock, kv, provider = pair_provider()
        kv._versions[VAULT_NAME] = []
        with caplog.at_level("WARNING", logger="app.secrets"):
            snapshot = await provider.get_snapshot(NAME)
        assert snapshot.current.version == "v2"
        assert snapshot.previous is None
        assert "secret.previous_version_resolution_degraded" in caplog.text
        assert kv.get_calls == [(VAULT_NAME, None)]
        await provider.aclose()

    asyncio.run(scenario())


def test_ambiguous_predecessor_timestamps_disable_overlap():
    async def scenario():
        clock, kv, provider = pair_provider()
        older = replace(kv._versions[VAULT_NAME][1], version="ambiguous-v1")
        kv._versions[VAULT_NAME].append(older)
        snapshot = await provider.get_snapshot(NAME)
        assert snapshot.current.version == "v2"
        assert snapshot.previous is None
        assert kv.get_calls == [(VAULT_NAME, None)]
        await provider.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("missing", ["v3", "v2", "v1"])
@pytest.mark.parametrize("order", list(permutations(range(3))))
def test_missing_history_timestamp_never_enables_older_cookie(missing, order, caplog):
    async def scenario():
        clock, kv, provider = pair_provider()
        history = [
            make_version(clock, "v3"),
            make_version(clock, "v2", age_seconds=10),
            make_version(clock, "v1", age_seconds=20),
        ]
        history = [replace(v, created_on=None) if v.version == missing else v for v in history]
        kv._versions[VAULT_NAME] = [history[i] for i in order]
        kv.set_response(VAULT_NAME, None, FakeSecretBundle("current-synthetic", history[0]))
        kv.set_response(VAULT_NAME, "v2", FakeSecretBundle("middle-synthetic", history[1]))
        with caplog.at_level("WARNING", logger="app.secrets"):
            snapshot = await provider.get_snapshot(NAME)
        assert snapshot.current.version == "v3"
        assert snapshot.previous is None
        assert len(snapshot.versions) == 1
        assert snapshot.versions[0].version == "v3"
        degraded = [
            r for r in caplog.records if r.message == "secret.previous_version_resolution_degraded"
        ]
        assert degraded
        assert all(r.error_category == "version_metadata_missing" for r in degraded)
        assert "synthetic" not in caplog.text
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=build_session_app(provider=provider, clock=clock)),
            base_url="https://testserver",
        ) as http:
            for key in ("previous-synthetic", "middle-synthetic", "current-synthetic"):
                cookie = make_signed_session_cookie(key, {"user": "synthetic"})
                response = await http.get(
                    "/session", headers={"cookie": f"fantasy_cards_session={cookie}"}
                )
                assert response.json() == {
                    "user": "synthetic" if key == "current-synthetic" else None
                }
            response = await http.post("/session/synthetic")
            assert_cookie_is_signed_with(
                response.cookies["fantasy_cards_session"], "current-synthetic"
            )
        for version in ("v1", "v2"):
            with pytest.raises(SecretVersionUnavailableError):
                await provider.get_secret_version(NAME, version=version)
        assert kv.get_calls == [(VAULT_NAME, None)]
        await provider.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", ["disabled", "expired", "future"])
@pytest.mark.parametrize("version", ["v3", "v2", "v1"])
def test_unknown_timestamp_validity_never_promotes_history(invalid, version):
    async def scenario():
        clock, kv, provider = pair_provider()
        changes = {
            "disabled": {"enabled": False},
            "expired": {"expires_on": clock.now()},
            "future": {"not_before": clock.now() + timedelta(seconds=10)},
        }
        history = [
            make_version(clock, "v3"),
            make_version(clock, "v2", age_seconds=10),
            make_version(clock, "v1", age_seconds=20),
        ]
        kv._versions[VAULT_NAME] = [
            replace(v, created_on=None, **changes[invalid]) if v.version == version else v
            for v in history
        ]
        # Even a valid value response must not undo a negative listing observation.
        kv.set_response(VAULT_NAME, None, FakeSecretBundle("current-synthetic", history[0]))
        if version == "v3":
            with pytest.raises(SecretProviderError):
                await provider.get_snapshot(NAME)
        else:
            snapshot = await provider.get_snapshot(NAME)
            assert snapshot.current.version == "v3"
            assert snapshot.previous is None
        assert all(version is None for _, version in kv.get_calls)
        await provider.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "ambiguity",
    ["current-tie", "predecessor-tie", "older-tie", "all-missing", "missing-id", "unplaced-oldest"],
)
@pytest.mark.parametrize("reverse", [False, True])
def test_history_ambiguities_are_resolved_before_snapshot_truncation(ambiguity, reverse):
    async def scenario():
        clock, kv, provider = pair_provider()
        history = [
            make_version(clock, "v3"),
            make_version(clock, "v2", age_seconds=10),
            make_version(clock, "v1", age_seconds=20),
            make_version(clock, "v0", age_seconds=30),
        ]
        if ambiguity == "current-tie":
            history[1] = replace(history[1], created_on=history[0].created_on)
        elif ambiguity == "predecessor-tie":
            history[2] = replace(history[2], created_on=history[1].created_on)
        elif ambiguity == "older-tie":
            history[3] = replace(history[3], created_on=history[2].created_on)
        elif ambiguity == "all-missing":
            history = [replace(v, created_on=None) for v in history]
        elif ambiguity == "missing-id":
            history[3] = replace(history[3], version=None)
        else:
            history[3] = replace(history[3], created_on=None)
        kv._versions[VAULT_NAME] = list(reversed(history)) if reverse else history
        kv.set_response(VAULT_NAME, None, FakeSecretBundle("current-synthetic", history[0]))
        kv.set_response(VAULT_NAME, "v2", FakeSecretBundle("middle-synthetic", history[1]))
        snapshot = await provider.get_snapshot(NAME)
        assert snapshot.current.version == "v3"
        if ambiguity == "older-tie":
            assert snapshot.previous.version == "v2"
            assert len(snapshot.versions) == 2
            assert kv.get_calls == [(VAULT_NAME, None), (VAULT_NAME, "v2")]
        else:
            assert snapshot.previous is None
            assert [v.version for v in snapshot.versions] == ["v3"]
            assert kv.get_calls == [(VAULT_NAME, None)]
        await provider.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("fail_after_observation", ["page", "value", None])
@pytest.mark.parametrize("ambiguity", ["missing-time", "missing-id", "predecessor-tie"])
def test_unknown_history_clears_cached_overlap_before_later_failure(
    fail_after_observation, ambiguity
):
    async def scenario():
        clock, kv, provider = pair_provider(cache_ttl=10, max_stale=20)
        initial = await provider.get_snapshot(NAME)
        clock.advance(11)
        unknown = make_version(clock, "unplaced")
        if ambiguity == "missing-time":
            unknown = replace(unknown, created_on=None)
        elif ambiguity == "missing-id":
            unknown = replace(unknown, version=None)
        else:
            unknown = replace(unknown, created_on=initial.versions[1].created_on)

        async def pages(_):
            yield unknown
            if fail_after_observation == "page":
                raise ServiceRequestError("synthetic-page-error")
            for metadata in kv._versions[VAULT_NAME]:
                yield metadata

        kv.list_properties_of_secret_versions = pages
        if fail_after_observation == "value":
            kv.set_response(VAULT_NAME, None, ServiceRequestError("synthetic-value-error"))
        await provider.refresh_secret(NAME)
        snapshot = provider.snapshot_for_read(NAME)
        assert snapshot.current.version == initial.current.version
        assert snapshot.previous is None
        if fail_after_observation is not None:
            assert snapshot.current.fetched_at == initial.current.fetched_at
            clock.advance(19)
            with pytest.raises(SecretStaleValueExpiredError):
                provider.snapshot_for_read(NAME)
        assert kv.get_calls.count((VAULT_NAME, "v1")) == 1
        await provider.aclose()

    asyncio.run(scenario())


def test_whole_refresh_timeout_includes_metadata_and_value_work():
    async def scenario():
        clock, kv, provider = pair_provider(refresh_timeout_seconds=0.03, request_timeout_seconds=1)
        events = []

        async def pages(_):
            events.append("metadata")
            await asyncio.sleep(0.02)
            yield kv._versions[VAULT_NAME][0]

        async def value():
            events.append("value")
            await asyncio.sleep(0.02)
            return FakeSecretBundle("current", kv._versions[VAULT_NAME][0])

        kv.list_properties_of_secret_versions = pages
        kv.set_response(VAULT_NAME, None, value)
        started = asyncio.get_running_loop().time()
        with pytest.raises(SecretRefreshTimeout):
            await provider.refresh_secret(NAME)
        elapsed = asyncio.get_running_loop().time() - started
        assert events == ["metadata", "value"]
        assert elapsed < 0.5
        assert DEFAULT_SECRET_REFRESH_TIMEOUT_SECONDS == 25
        assert DEFAULT_SECRET_REFRESH_INTERVAL_SECONDS == 20
        await provider.aclose()

    asyncio.run(scenario())


def test_whole_refresh_timeout_also_bounds_retry_backoff():
    async def scenario():
        clock = FakeClock()
        current = make_version(clock, "v1")
        kv = FakeSecretClient(versions={VAULT_NAME: [current]})
        kv.set_response(VAULT_NAME, None, ServiceRequestError("synthetic-outage"))
        delays = []

        async def blocked_backoff(delay):
            delays.append(delay)
            await asyncio.Future()

        provider = AzureSecretProvider(
            key_vault_uri="https://vault.example",
            client=kv,
            clock=clock.now,
            max_retries=2,
            refresh_timeout_seconds=0.01,
            sleep=blocked_backoff,
        )
        with pytest.raises(SecretRefreshTimeout):
            await provider.get_secret(NAME)
        assert delays == [0.25]
        assert kv.get_calls == [(VAULT_NAME, None)]
        await provider.aclose()

    asyncio.run(scenario())


def test_cancelled_waiter_does_not_cancel_shared_refresh_but_close_does():
    async def scenario():
        clock, kv, provider = pair_provider(close_client=True)
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def blocked():
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        kv.set_response(VAULT_NAME, None, blocked)
        first = asyncio.create_task(provider.refresh_secret(NAME))
        second = asyncio.create_task(provider.get_secret(NAME))
        await entered.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not cancelled.is_set()
        original_close = kv.close

        async def close():
            assert cancelled.is_set()
            await original_close()

        kv.close = close
        await provider.aclose()
        with pytest.raises(asyncio.CancelledError):
            await second
        assert not provider._inflight
        assert not provider._cache
        assert kv.close_calls == 1
        await provider.aclose()
        assert kv.close_calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("auth_enabled", [False, True])
def test_lifespan_preloads_and_owns_independent_workers_and_shutdown(
    auth_enabled, monkeypatch, capsys
):
    async def scenario():
        clock, kv, provider = pair_provider(close_client=True)
        old = make_version(clock, "v1")
        kv._versions["entra-client-secret"] = [old]
        kv.set_response("entra-client-secret", None, FakeSecretBundle("old-synthetic", old))
        sleeps = {}

        def worker(provider, name, **kwargs):
            sleep = ControlledSleep()
            sleeps[name] = sleep
            return run_secret_refresh_worker(
                provider,
                name,
                **kwargs,
                sleep=sleep,
                monotonic=lambda: clock.now().timestamp(),
            )

        monkeypatch.setattr(main_module, "run_secret_refresh_worker", worker)
        if not auth_enabled:
            monkeypatch.delenv("ENTRA_CLIENT_ID")
            monkeypatch.delenv("ENTRA_REDIRECT_URI")
        app = main_module.create_app(secret_provider=provider)
        # Keep binding deadlines on the same deterministic clock as the vault.
        app.state.entra_oauth_client_manager = EntraOAuthClientManager(
            settings=load_auth_settings(),
            secret_provider=provider,
            clock=clock.now,
            previous_secret_overlap=timedelta(seconds=20),
        )
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def blocked():
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        async with app.router.lifespan_context(app):
            await asyncio.sleep(0)
            initial_events = [
                json.loads(line)
                for line in capsys.readouterr().err.splitlines()
                if line.startswith("{")
            ]
            assert [(event["logical_secret"], event["stage"]) for event in initial_events] == (
                [("session", "provider"), ("entra", "provider"), ("entra", "entra_binding")]
                if auth_enabled
                else [("session", "provider")]
            )
            assert all(event["result"] == "adopted" for event in initial_events)
            assert set(sleeps) == ({NAME, "ENTRA_CLIENT_SECRET"} if auth_enabled else {NAME})
            assert provider.snapshot_for_read(NAME).current.version == "v2"
            kv.set_response(VAULT_NAME, None, blocked)
            _, session_tick = await sleeps[NAME].waiting()
            session_tick.set_result(None)
            await entered.wait()
            if auth_enabled:
                _, entra_tick = await sleeps["ENTRA_CLIENT_SECRET"].waiting()
                clock.advance(20)
                new = make_version(clock, "v2")
                kv._versions["entra-client-secret"] = [new, old]
                kv.set_response("entra-client-secret", None, FakeSecretBundle("new", new))
                kv.set_response("entra-client-secret", "v1", FakeSecretBundle("old-synthetic", old))
                entra_tick.set_result(None)
                _, next_tick = await sleeps["ENTRA_CLIENT_SECRET"].waiting()
                manager = app.state.entra_oauth_client_manager
                assert manager._current_binding.secret.version == "v2"
                assert manager._previous_binding is not None
                clock.advance(20)
                next_tick.set_result(None)
                await sleeps["ENTRA_CLIENT_SECRET"].waiting()
                assert manager._previous_binding is None
        assert cancelled.is_set()
        assert kv.close_calls == 1
        assert not provider._inflight
        if auth_enabled:
            events = [
                json.loads(line)
                for line in capsys.readouterr().err.splitlines()
                if line.startswith("{")
            ]
            assert [event["result"] for event in events if event["stage"] == "entra_binding"] == [
                "adopted",
                "unchanged",
            ]

    asyncio.run(scenario())


def test_worker_retries_expected_failures_and_accounts_for_cycle_duration(caplog):
    async def scenario():
        clock, kv, provider = pair_provider()
        kv.set_list_response(VAULT_NAME, ServiceRequestError("synthetic-outage"))
        sleep = ControlledSleep()
        worker = asyncio.create_task(
            run_secret_refresh_worker(
                provider, NAME, sleep=sleep, monotonic=lambda: clock.now().timestamp()
            )
        )
        _, tick = await sleep.waiting()
        with caplog.at_level("WARNING", logger="app.secrets"):
            tick.set_result(None)
            delay, next_tick = await sleep.waiting()
        assert "secret.background_refresh_degraded" in caplog.text
        assert delay == 20
        clock.advance(20)
        kv.set_list_response(VAULT_NAME, None)

        def slow_value():
            clock.advance(7)
            return FakeSecretBundle("current", kv._versions[VAULT_NAME][0])

        kv.set_response(VAULT_NAME, None, slow_value)
        next_tick.set_result(None)
        delay, _ = await sleep.waiting()
        assert delay == 13
        assert provider.snapshot_for_read(NAME).current.value == "current"
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await provider.aclose()

    asyncio.run(scenario())


def test_snapshot_cache_and_failure_entries_are_bounded():
    async def scenario():
        clock, kv, provider = pair_provider()
        for i in range(MAX_SECRET_SNAPSHOTS + 5):
            name = f"SYNTHETIC_{i}"
            vault_name = name.lower().replace("_", "-")
            kv.set_list_response(vault_name, ServiceRequestError("synthetic-outage"))
            with pytest.raises(SecretProviderError):
                await provider.get_secret(name)
        assert len(provider._cache) == MAX_SECRET_SNAPSHOTS
        assert not provider._inflight
        await provider.aclose()

    asyncio.run(scenario())


def test_sdk_close_failure_still_closes_owned_credential():
    async def scenario():
        credential = FakeAsyncCredential()
        _, kv, provider = pair_provider(
            credential=credential, close_client=True, close_credential=True
        )

        async def broken_close():
            raise RuntimeError("synthetic-close-failure")

        kv.close = broken_close
        with pytest.raises(RuntimeError, match="synthetic-close-failure"):
            await provider.aclose()
        assert credential.close_calls == 1

    asyncio.run(scenario())


def test_unexpected_worker_failure_surfaces_and_lifespan_closes_resources(monkeypatch):
    async def scenario():
        _, kv, provider = pair_provider(close_client=True)
        monkeypatch.delenv("ENTRA_CLIENT_ID")
        monkeypatch.delenv("ENTRA_REDIRECT_URI")

        async def broken_worker(*args, **kwargs):
            raise RuntimeError("synthetic-programming-error")

        monkeypatch.setattr(main_module, "run_secret_refresh_worker", broken_worker)
        app = main_module.create_app(secret_provider=provider)
        with pytest.raises(ExceptionGroup) as caught:
            async with app.router.lifespan_context(app):
                await asyncio.Future()
        assert len(caught.value.exceptions) == 1
        assert str(caught.value.exceptions[0]) == "synthetic-programming-error"
        assert kv.close_calls == 1

    asyncio.run(scenario())
