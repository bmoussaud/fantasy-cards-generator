"""Unit + integration tests for scripts/session_rotation/harness.py.

The harness is a private, memory-only drill payload that is embedded verbatim into
the session-rotation Container App Job (see control.py's `validate_job`). It never
touches live Azure or a live app in these tests: Key Vault is replaced with an
in-memory fake client, and the cookie-overlap behaviour is exercised against the
*real* `RotatingSessionMiddleware` running in-process behind Starlette's ASGI
`TestClient` (no network calls), so the overlap/rejection assertions reflect
production code, not a hand-written proxy.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import secrets as secrets_module
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import itsdangerous
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.testclient import TestClient

from app.session_middleware import RotatingSessionMiddleware
from tests.test_session_middleware import (
    FakeClock,
    FakeManagedSecretVersion,
    FakeSessionSecretProvider,
)

ROOT = Path(__file__).resolve().parents[1]
HARNESS_PATH = ROOT / "scripts/session_rotation/harness.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location(
        "session_rotation_harness_under_test", HARNESS_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def harness():
    return _load_harness()


def version_id() -> str:
    return secrets_module.token_hex(16)


# A brute-forced sha256(...)[:12] collision pair, used only to exercise the
# hash-collision guard in history(); the values carry no other significance.
COLLIDING_VERSION_A = "b83ca525f262072834ec2c52282dc231"[:32]
COLLIDING_VERSION_B = "de617cf7b4fe2bcbf03e91dad61be809"


@dataclass
class FakeKVItem:
    version: str
    created_on: datetime
    enabled: bool = True
    expires_on: datetime | None = None
    not_before: datetime | None = None
    tags: dict | None = field(default_factory=dict)


class FakeKVClient:
    """A minimal stand-in for azure.keyvault.secrets.SecretClient."""

    def __init__(self, items, values, *, clock, secret_name):
        self.items = list(items)
        self.values = dict(values)
        self.clock = clock
        self.secret_name = secret_name
        self.set_secret_calls = []

    def list_properties_of_secret_versions(self, name):
        assert name == self.secret_name
        return list(self.items)

    def get_secret(self, name, version):
        assert name == self.secret_name
        value = self.values[version]
        return _Bundle(value, _Properties(version))

    def set_secret(
        self, name, value, *, enabled, expires_on, tags, retry_total=0, logging_enable=False
    ):
        assert name == self.secret_name
        version = version_id()
        now = self.clock()
        item = FakeKVItem(
            version=version, created_on=now, enabled=enabled, expires_on=expires_on, tags=dict(tags)
        )
        self.items.append(item)
        self.values[version] = value
        self.set_secret_calls.append(item)
        return _Bundle(value, _Properties(version))


@dataclass
class _Properties:
    version: str


@dataclass
class _Bundle:
    value: str
    properties: _Properties


def make_item(version=None, *, age_seconds=0, now=None, **kwargs):
    now = now or datetime(2026, 1, 1, tzinfo=UTC)
    return FakeKVItem(
        version=version or version_id(),
        created_on=now - timedelta(seconds=age_seconds),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# digest() / emit()
# ---------------------------------------------------------------------------


def test_digest_is_stable_and_matches_hash_pattern(harness):
    version = version_id()
    d1 = harness.digest(version)
    d2 = harness.digest(version)
    assert d1 == d2
    assert harness.HASH.fullmatch(d1)


def test_digest_rejects_non_version_string(harness):
    with pytest.raises(harness.Blocked) as excinfo:
        harness.digest("not-a-32-hex-version")
    assert excinfo.value.args[0] == "version_invalid"


def test_emit_rejects_unknown_stage(harness, capsys):
    with pytest.raises(harness.Blocked) as excinfo:
        harness.emit(str(uuid4()), "not_a_real_stage")
    assert excinfo.value.args[0] == "stage_invalid"
    assert capsys.readouterr().out == ""


def test_emit_rejects_malformed_hash(harness):
    with pytest.raises(harness.Blocked) as excinfo:
        harness.emit(str(uuid4()), "preflight", version_hash="not-a-hash")
    assert excinfo.value.args[0] == "hash_invalid"


def test_emit_prints_content_free_json_record(harness, capsys):
    run_id = str(uuid4())
    created = datetime(2026, 1, 1, tzinfo=UTC)
    harness.emit(run_id, "rotated", version_hash="0" * 12, created_on=created)
    record = json.loads(capsys.readouterr().out)
    assert record == {
        "schema": harness.SCHEMA,
        "run_id": run_id,
        "stage": "rotated",
        "at": record["at"],
        "version_hash": "0" * 12,
        "created_on": "2026-01-01T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# private_dns()
# ---------------------------------------------------------------------------


def test_private_dns_accepts_single_expected_address(harness, monkeypatch):
    monkeypatch.setattr(
        harness.socket,
        "getaddrinfo",
        lambda *a, **k: [(None, None, None, None, ("10.42.2.7", 443))],
    )
    harness.private_dns()  # does not raise


def test_private_dns_rejects_unexpected_address(harness, monkeypatch):
    monkeypatch.setattr(
        harness.socket,
        "getaddrinfo",
        lambda *a, **k: [(None, None, None, None, ("203.0.113.9", 443))],
    )
    with pytest.raises(harness.Blocked) as excinfo:
        harness.private_dns()
    assert excinfo.value.args[0] == "private_dns_mismatch"


# ---------------------------------------------------------------------------
# history(): ordering + integrity guards
# ---------------------------------------------------------------------------


def test_history_returns_versions_sorted_newest_first(harness):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    old = make_item(age_seconds=100, now=now)
    new = make_item(age_seconds=10, now=now)
    client = FakeKVClient([old, new], {}, clock=lambda: now, secret_name=harness.SECRET)
    rows = harness.history(client, now)
    assert [r.version for r in rows] == [new.version, old.version]


def test_history_rejects_empty_history(harness):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    client = FakeKVClient([], {}, clock=lambda: now, secret_name=harness.SECRET)
    with pytest.raises(harness.Blocked) as excinfo:
        harness.history(client, now)
    assert excinfo.value.args[0] == "history_missing"


def test_history_rejects_duplicate_version(harness):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    version = version_id()
    items = [make_item(version, now=now), make_item(version, age_seconds=5, now=now)]
    client = FakeKVClient(items, {}, clock=lambda: now, secret_name=harness.SECRET)
    with pytest.raises(harness.Blocked) as excinfo:
        harness.history(client, now)
    assert excinfo.value.args[0] == "duplicate_version"


def test_history_rejects_hash_collision(harness):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    items = [
        make_item(COLLIDING_VERSION_A, now=now),
        make_item(COLLIDING_VERSION_B, age_seconds=5, now=now),
    ]
    client = FakeKVClient(items, {}, clock=lambda: now, secret_name=harness.SECRET)
    with pytest.raises(harness.Blocked) as excinfo:
        harness.history(client, now)
    assert excinfo.value.args[0] == "hash_collision"


def test_history_rejects_ambiguous_ordering(harness):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    items = [make_item(now=now), make_item(now=now)]  # identical created_on
    client = FakeKVClient(items, {}, clock=lambda: now, secret_name=harness.SECRET)
    with pytest.raises(harness.Blocked) as excinfo:
        harness.history(client, now)
    assert excinfo.value.args[0] == "ordering_ambiguous"


def test_history_rejects_future_created_on(harness):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    items = [make_item(now=now, age_seconds=-10)]  # created "in the future"
    client = FakeKVClient(items, {}, clock=lambda: now, secret_name=harness.SECRET)
    with pytest.raises(harness.Blocked) as excinfo:
        harness.history(client, now)
    assert excinfo.value.args[0] == "creation_unknown"


def test_history_rejects_naive_created_on(harness):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    items = [FakeKVItem(version=version_id(), created_on=datetime(2026, 1, 1))]
    client = FakeKVClient(items, {}, clock=lambda: now, secret_name=harness.SECRET)
    with pytest.raises(harness.Blocked) as excinfo:
        harness.history(client, now)
    assert excinfo.value.args[0] == "creation_unknown"


def test_history_rejects_non_bool_enabled(harness):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    items = [make_item(now=now, enabled=None)]
    client = FakeKVClient(items, {}, clock=lambda: now, secret_name=harness.SECRET)
    with pytest.raises(harness.Blocked) as excinfo:
        harness.history(client, now)
    assert excinfo.value.args[0] == "enabled_unknown"


def test_history_rejects_non_mapping_tags(harness):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    items = [make_item(now=now, tags="not-a-dict")]
    client = FakeKVClient(items, {}, clock=lambda: now, secret_name=harness.SECRET)
    with pytest.raises(harness.Blocked) as excinfo:
        harness.history(client, now)
    assert excinfo.value.args[0] == "tags_invalid"


def test_history_enforces_version_limit(harness, monkeypatch):
    monkeypatch.setattr(harness, "MAX_VERSIONS", 2)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    items = [make_item(now=now, age_seconds=s) for s in (0, 10, 20)]
    client = FakeKVClient(items, {}, clock=lambda: now, secret_name=harness.SECRET)
    with pytest.raises(harness.Blocked) as excinfo:
        harness.history(client, now)
    assert excinfo.value.args[0] == "version_limit"


# ---------------------------------------------------------------------------
# usable()
# ---------------------------------------------------------------------------


def test_usable_accepts_enabled_active_far_from_expiry(harness):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    item = make_item(now=now, expires_on=now + timedelta(days=7))
    harness.usable(item, now, 60)  # does not raise


def test_usable_rejects_disabled_version(harness):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    item = make_item(now=now, enabled=False)
    with pytest.raises(harness.Blocked) as excinfo:
        harness.usable(item, now, 0)
    assert excinfo.value.args[0] == "version_disabled"


def test_usable_rejects_not_yet_active_version(harness):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    item = make_item(now=now, not_before=now + timedelta(seconds=5))
    with pytest.raises(harness.Blocked) as excinfo:
        harness.usable(item, now, 0)
    assert excinfo.value.args[0] == "version_not_active"


def test_usable_rejects_version_expiring_before_remaining_budget(harness):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    item = make_item(now=now, expires_on=now + timedelta(seconds=30))
    with pytest.raises(harness.Blocked) as excinfo:
        harness.usable(item, now, 60)
    assert excinfo.value.args[0] == "version_expiring"


# ---------------------------------------------------------------------------
# Rotation: preflight()
# ---------------------------------------------------------------------------


def _rotation(harness, client, run_id, expected, *, now):
    return harness.Rotation(client, run_id, expected, now=lambda: now[0], sleep=lambda s: None)


def test_preflight_populates_old_value(harness):
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    baseline = make_item(age_seconds=8000, now=now[0])
    client = FakeKVClient(
        [baseline],
        {baseline.version: "old-secret-value"},
        clock=lambda: now[0],
        secret_name=harness.SECRET,
    )
    rotation = _rotation(harness, client, str(uuid4()), harness.digest(baseline.version), now=now)

    rotation.preflight()

    assert rotation.old.version == baseline.version
    assert rotation.old_value == "old-secret-value"


def test_preflight_rejects_preexisting_run_tag(harness):
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    run_id = str(uuid4())
    baseline = make_item(age_seconds=8000, now=now[0], tags={"session-drill-run": run_id})
    client = FakeKVClient(
        [baseline], {baseline.version: "v"}, clock=lambda: now[0], secret_name=harness.SECRET
    )
    rotation = _rotation(harness, client, run_id, harness.digest(baseline.version), now=now)
    with pytest.raises(harness.Blocked) as excinfo:
        rotation.preflight()
    assert excinfo.value.args[0] == "preexisting_run"


def test_preflight_rejects_baseline_mismatch(harness):
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    baseline = make_item(age_seconds=8000, now=now[0])
    client = FakeKVClient(
        [baseline], {baseline.version: "v"}, clock=lambda: now[0], secret_name=harness.SECRET
    )
    rotation = _rotation(harness, client, str(uuid4()), "0" * 12, now=now)
    with pytest.raises(harness.Blocked) as excinfo:
        rotation.preflight()
    assert excinfo.value.args[0] == "baseline_changed"


def test_preflight_rejects_recent_baseline_as_preexisting_rotation(harness):
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    baseline = make_item(age_seconds=10, now=now[0])  # well within OVERLAP
    client = FakeKVClient(
        [baseline], {baseline.version: "v"}, clock=lambda: now[0], secret_name=harness.SECRET
    )
    rotation = _rotation(harness, client, str(uuid4()), harness.digest(baseline.version), now=now)
    with pytest.raises(harness.Blocked) as excinfo:
        rotation.preflight()
    assert excinfo.value.args[0] == "preexisting_rotation"


def test_preflight_rejects_expiring_baseline(harness):
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    baseline = make_item(age_seconds=8000, now=now[0], expires_on=now[0] + timedelta(seconds=60))
    client = FakeKVClient(
        [baseline], {baseline.version: "v"}, clock=lambda: now[0], secret_name=harness.SECRET
    )
    rotation = _rotation(harness, client, str(uuid4()), harness.digest(baseline.version), now=now)
    with pytest.raises(harness.Blocked) as excinfo:
        rotation.preflight()
    assert excinfo.value.args[0] == "version_expiring"


# ---------------------------------------------------------------------------
# Rotation: rotate() / write_once() idempotency and conflict handling
# ---------------------------------------------------------------------------


def _prepared_rotation(harness, now):
    run_id = str(uuid4())
    baseline = make_item(age_seconds=8000, now=now[0])
    client = FakeKVClient(
        [baseline],
        {baseline.version: "old-secret-value"},
        clock=lambda: now[0],
        secret_name=harness.SECRET,
    )
    rotation = _rotation(harness, client, run_id, harness.digest(baseline.version), now=now)
    rotation.preflight()
    return rotation, client, baseline


def test_rotate_writes_new_tagged_version_and_emits_events(harness, capsys):
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    rotation, client, baseline = _prepared_rotation(harness, now)

    target = rotation.rotate()

    assert len(client.set_secret_calls) == 1
    written = client.set_secret_calls[0]
    assert written.tags["session-drill-run"] == rotation.run_id
    assert written.tags["session-drill-phase"] == "rotate"
    assert written.tags["session-drill-predecessor"] == harness.digest(baseline.version)
    assert target.version == written.version
    stages = [json.loads(line)["stage"] for line in capsys.readouterr().out.splitlines()]
    assert stages == ["write_intent", "rotated"]


def test_rotate_resumes_idempotently_when_tagged_write_already_exists(harness, capsys):
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    rotation, client, baseline = _prepared_rotation(harness, now)
    # Simulate a prior, already-committed write for this exact run/phase/predecessor.
    existing = FakeKVItem(
        version=version_id(),
        created_on=now[0],
        tags={
            "session-drill-run": rotation.run_id,
            "session-drill-phase": "rotate",
            "session-drill-predecessor": harness.digest(baseline.version),
        },
    )
    client.items.append(existing)
    client.values[existing.version] = "resumed-value"

    target = rotation.rotate()

    assert len(client.set_secret_calls) == 0  # no second write attempted
    assert target.version == existing.version


def test_rotate_rejects_write_conflict_when_two_tagged_rows_exist(harness):
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    rotation, client, baseline = _prepared_rotation(harness, now)
    tags = {
        "session-drill-run": rotation.run_id,
        "session-drill-phase": "rotate",
        "session-drill-predecessor": harness.digest(baseline.version),
    }
    for offset in (0, 1):
        item = FakeKVItem(
            version=version_id(), created_on=now[0] - timedelta(seconds=offset), tags=dict(tags)
        )
        client.items.append(item)
        client.values[item.version] = "v"

    with pytest.raises(harness.Blocked) as excinfo:
        rotation.rotate()
    assert excinfo.value.args[0] == "write_conflict"


def test_rotate_rejects_when_latest_is_no_longer_the_expected_predecessor(harness):
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    rotation, client, baseline = _prepared_rotation(harness, now)
    # Someone else wrote a new, untagged latest version between preflight and rotate.
    foreign = make_item(age_seconds=0, now=now[0])
    client.items.append(foreign)
    client.values[foreign.version] = "foreign-value"

    with pytest.raises(harness.Blocked) as excinfo:
        rotation.rotate()
    assert excinfo.value.args[0] == "foreign_latest"


def test_write_once_rejects_repeated_phase_attempt_in_same_process(harness):
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    rotation, client, baseline = _prepared_rotation(harness, now)
    rotation.rotate()
    with pytest.raises(harness.Blocked) as excinfo:
        rotation.write_once("rotate", harness.digest(baseline.version), "again")
    assert excinfo.value.args[0] == "write_already_attempted"


# ---------------------------------------------------------------------------
# Rotation: recover()
# ---------------------------------------------------------------------------


def test_recover_requires_a_provable_prior_rotation(harness):
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    rotation, client, baseline = _prepared_rotation(harness, now)
    with pytest.raises(harness.Blocked) as excinfo:
        rotation.recover()
    assert excinfo.value.args[0] == "rotation_unprovable"


def test_recover_writes_predecessor_value_as_new_version_never_disabling_latest(harness):
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    rotation, client, baseline = _prepared_rotation(harness, now)
    target = rotation.rotate()
    now[0] = now[0] + timedelta(seconds=5)

    recovered = rotation.recover()

    assert recovered.version != target.version
    assert recovered.version != baseline.version  # a *new* version, not toggling the old one
    # The rotated ("bad") version is still present and still enabled -- never disabled.
    rotated_row = next(i for i in client.items if i.version == target.version)
    assert rotated_row.enabled is True
    # The recovered value matches what was in place pre-rotation.
    assert client.values[recovered.version] == "old-secret-value"


def test_recover_is_idempotent_when_already_recovered(harness):
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    rotation, client, baseline = _prepared_rotation(harness, now)
    rotation.rotate()
    now[0] = now[0] + timedelta(seconds=5)
    first = rotation.recover()
    calls_before = len(client.set_secret_calls)

    second = rotation.recover()

    assert second.version == first.version
    assert len(client.set_secret_calls) == calls_before  # no second corrective write


def test_recover_rejects_when_predecessor_is_not_directly_prior(harness):
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    rotation, client, baseline = _prepared_rotation(harness, now)
    rotation.rotate()
    now[0] = now[0] + timedelta(seconds=5)
    # Insert an unrelated, untagged version between the rotation and the baseline.
    wedge = FakeKVItem(version=version_id(), created_on=now[0] - timedelta(seconds=1))
    client.items.append(wedge)
    client.values[wedge.version] = "wedge-value"

    with pytest.raises(harness.Blocked) as excinfo:
        rotation.recover()
    assert excinfo.value.args[0] == "foreign_latest"


# ---------------------------------------------------------------------------
# decode_cookie() / anonymous()
# ---------------------------------------------------------------------------


def _sign(key, payload):
    raw = base64.b64encode(json.dumps(payload).encode("utf-8"))
    return itsdangerous.TimestampSigner(key).sign(raw).decode("utf-8")


def _anonymous_payload(nonce="n" * 20):
    return {
        "auth_nonce": nonce,
        "_state_entra_id_abc123": {
            "data": {"redirect_uri": "https://app/cb", "url": "https://entra/auth", "nonce": nonce},
            "exp": 123456.0,
        },
    }


def test_decode_cookie_round_trips_valid_payload(harness):
    key = "secret-key-value"
    cookie = _sign(key, _anonymous_payload())
    data = harness.decode_cookie(cookie, key)
    assert data["auth_nonce"] == "n" * 20


def test_decode_cookie_rejects_bad_signature(harness):
    cookie = _sign("key-a", _anonymous_payload())
    with pytest.raises(harness.Blocked) as excinfo:
        harness.decode_cookie(cookie, "key-b")
    assert excinfo.value.args[0] == "cookie_invalid"


def test_decode_cookie_rejects_oversized_payload(harness):
    key = "secret-key-value"
    payload = {"auth_nonce": "n" * 20, "junk": "x" * 20000}
    cookie = _sign(key, payload)
    with pytest.raises(harness.Blocked) as excinfo:
        harness.decode_cookie(cookie, key)
    assert excinfo.value.args[0] == "cookie_size"


def test_decode_cookie_rejects_non_dict_payload(harness):
    key = "secret-key-value"
    raw = base64.b64encode(json.dumps([1, 2, 3]).encode("utf-8"))
    cookie = itsdangerous.TimestampSigner(key).sign(raw).decode("utf-8")
    with pytest.raises(harness.Blocked) as excinfo:
        harness.decode_cookie(cookie, key)
    assert excinfo.value.args[0] == "cookie_shape"


def test_anonymous_accepts_well_formed_markers(harness):
    payload = _anonymous_payload()
    result = harness.anonymous(payload)
    assert result == payload


def test_anonymous_rejects_extra_unexpected_key(harness):
    payload = _anonymous_payload()
    payload["unexpected"] = "value"
    with pytest.raises(harness.Blocked) as excinfo:
        harness.anonymous(payload)
    assert excinfo.value.args[0] == "anonymous_markers_invalid"


def test_anonymous_rejects_mismatched_state_nonce(harness):
    payload = _anonymous_payload()
    payload["_state_entra_id_abc123"]["data"]["nonce"] = "different-nonce-value"
    with pytest.raises(harness.Blocked) as excinfo:
        harness.anonymous(payload)
    assert excinfo.value.args[0] == "state_data_invalid"


def test_anonymous_requires_csrf_token_when_requested(harness):
    payload = _anonymous_payload()
    payload["csrf_token"] = "abc"
    result = harness.anonymous(payload, csrf=True)
    assert "csrf_token" not in result  # only the anonymous markers are returned


# ---------------------------------------------------------------------------
# CookieProbe: exercised against the *real* RotatingSessionMiddleware
# ---------------------------------------------------------------------------


def _build_probe_app(*, provider, clock, overlap_seconds=3600):
    app = FastAPI()
    app.state.secret_provider = provider
    app.add_middleware(
        RotatingSessionMiddleware,
        session_cookie="session",
        same_site="lax",
        signing_key_overlap=timedelta(seconds=overlap_seconds),
        clock=clock.now,
    )

    @app.get("/auth/login")
    async def login(request: Request):
        # Mirrors the Authlib-managed anonymous pre-login session shape the harness
        # expects: an auth_nonce plus a single `_state_entra_id_<state>` blob.
        nonce = secrets_module.token_urlsafe(16)
        request.session["auth_nonce"] = nonce
        request.session["_state_entra_id_teststate"] = {
            "data": {
                "redirect_uri": "https://app.example/auth/callback",
                "url": "https://entra.example/authorize",
                "nonce": nonce,
            },
            "exp": clock.now().timestamp() + 600,
        }
        return RedirectResponse("https://entra.example/authorize", status_code=302)

    @app.get("/")
    async def home(request: Request):
        if "csrf_token" not in request.session:
            request.session["csrf_token"] = secrets_module.token_urlsafe(16)
        return JSONResponse({"ok": True})

    return app


@contextmanager
def _http_client(app):
    client = TestClient(app, base_url="https://testserver", follow_redirects=False)
    yield client


def test_cookie_probe_accepts_old_cookie_within_overlap_after_real_rotation(harness):
    clock = FakeClock()
    provider = FakeSessionSecretProvider(
        clock=clock, versions=[FakeManagedSecretVersion("v1", "old-key-value", clock.now())]
    )
    app = _build_probe_app(provider=provider, clock=clock, overlap_seconds=3600)
    with _http_client(app) as client:
        probe = harness.CookieProbe(client, "old-key-value")

        # Rotate: the app now serves a new current key, with the old one as "previous".
        provider.replace_versions(
            [
                FakeManagedSecretVersion("v2", "new-key-value", clock.now()),
                FakeManagedSecretVersion("v1", "old-key-value", clock.now() - timedelta(seconds=1)),
            ]
        )

        # Within the overlap window the frozen (pre-rotation) cookie must still work.
        probe.check("new-key-value", accepted=True)


def test_cookie_probe_rejects_old_cookie_after_overlap_elapses(harness):
    clock = FakeClock()
    provider = FakeSessionSecretProvider(
        clock=clock, versions=[FakeManagedSecretVersion("v1", "old-key-value", clock.now())]
    )
    app = _build_probe_app(provider=provider, clock=clock, overlap_seconds=3600)
    with _http_client(app) as client:
        probe = harness.CookieProbe(client, "old-key-value")

        rotated_at = clock.now()
        provider.replace_versions(
            [
                FakeManagedSecretVersion("v2", "new-key-value", rotated_at),
                FakeManagedSecretVersion("v1", "old-key-value", rotated_at - timedelta(seconds=1)),
            ]
        )
        clock.advance(3601)  # past the overlap window anchored on the new version

        probe.check("new-key-value", accepted=False)
        probe.fresh("new-key-value")  # a brand-new probe must still authenticate cleanly


def test_cookie_probe_rejects_non_redirect_login_response(harness):
    clock = FakeClock()
    provider = FakeSessionSecretProvider(
        clock=clock, versions=[FakeManagedSecretVersion("v1", "old-key-value", clock.now())]
    )
    app = FastAPI()
    app.state.secret_provider = provider
    app.add_middleware(
        RotatingSessionMiddleware,
        session_cookie="session",
        clock=clock.now,
    )

    @app.get("/auth/login")
    async def login():
        return JSONResponse({"unexpected": True})

    with _http_client(app) as client:
        with pytest.raises(harness.Blocked) as excinfo:
            harness.CookieProbe(client, "old-key-value")
    assert excinfo.value.args[0] == "login_not_redirect"


# ---------------------------------------------------------------------------
# experiment(): timing milestones with a fully controllable clock
# ---------------------------------------------------------------------------


class _FakeCookies:
    def __init__(self):
        self.calls = []

    def check(self, key, accepted):
        self.calls.append(("check", key, accepted))

    def fresh(self, key):
        self.calls.append(("fresh", key))


def test_experiment_runs_milestones_in_order_and_then_awaits_watchdog(harness):
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    rotation, client, baseline = _prepared_rotation(harness, now)
    cookies = _FakeCookies()
    monotonic = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        monotonic[0] += seconds
        now[0] = now[0] + timedelta(seconds=seconds)

    with pytest.raises(harness.Blocked) as excinfo:
        harness.experiment(rotation, cookies, monotonic=lambda: monotonic[0], sleep=sleep)

    assert excinfo.value.args[0] == "watchdog"
    kinds = [c[0] for c in cookies.calls]
    assert kinds.count("check") == 3
    assert kinds.count("fresh") == 1  # only emitted for the not-accepted boundary probe
    assert sleeps  # time was advanced via the injected sleep, never real time.sleep
