"""Private, memory-only session drill. This file is embedded verbatim in Bicep."""

import base64
import hashlib
import json
import logging
import os
import re
import signal
import socket
import sys
import time
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from uuid import UUID

SECRET = "app-session-secret-key"
HOST = "kvfcagdevqhg3qc4rlbt4g.vault.azure.net"
SCHEMA = "session-rotation/v1"
MAX_VERSIONS = 100
OVERLAP = 3600
WATCHDOG = 4200
CLOCK_MARGIN = 3
HASH = re.compile(r"[0-9a-f]{12}")
VERSION = re.compile(r"[0-9a-f]{32}")
STAGES = {
    "preflight",
    "write_intent",
    "rotated",
    "cookie_early",
    "cookie_near",
    "cookie_boundary",
    "awaiting_controller_stop",
    "recovered",
    "blocked",
    "manual_recovery_required",
    "unexpected_failure",
}


class Blocked(Exception):
    """Fixed, content-free failure codes only."""


def require(condition, code):
    if not condition:
        raise Blocked(code)


def digest(version):
    require(isinstance(version, str) and VERSION.fullmatch(version), "version_invalid")
    return hashlib.sha256(version.encode("ascii")).hexdigest()[:12]


def utc():
    return datetime.now(UTC)


def stamp(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def emit(run_id, stage, version_hash=None, created_on=None):
    require(stage in STAGES, "stage_invalid")
    record = {"schema": SCHEMA, "run_id": str(UUID(run_id)), "stage": stage, "at": stamp(utc())}
    if version_hash is not None:
        require(bool(HASH.fullmatch(version_hash)), "hash_invalid")
        record["version_hash"] = version_hash
    if created_on is not None:
        record["created_on"] = stamp(created_on)
    print(json.dumps(record, sort_keys=True), flush=True)


def private_dns():
    addresses = {row[4][0] for row in socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM)}
    require(addresses == {"10.42.2.7"}, "private_dns_mismatch")


def valid_date(value):
    return isinstance(value, datetime) and value.tzinfo is not None


def history(client, now):
    versions = []
    for item in client.list_properties_of_secret_versions(SECRET):
        require(len(versions) < MAX_VERSIONS, "version_limit")
        digest(item.version)
        require(valid_date(item.created_on) and item.created_on <= now, "creation_unknown")
        for name in ("expires_on", "not_before"):
            value = getattr(item, name, None)
            require(value is None or valid_date(value), "validity_unknown")
        require(item.enabled in (True, False), "enabled_unknown")
        require(isinstance(item.tags or {}, dict), "tags_invalid")
        versions.append(item)
    require(bool(versions), "history_missing")
    require(len({v.version for v in versions}) == len(versions), "duplicate_version")
    require(len({digest(v.version) for v in versions}) == len(versions), "hash_collision")
    versions.sort(key=lambda v: v.created_on, reverse=True)
    require(len({v.created_on for v in versions}) == len(versions), "ordering_ambiguous")
    return versions


def usable(item, now, remaining):
    require(item.enabled is True, "version_disabled")
    require(item.not_before is None or item.not_before <= now, "version_not_active")
    require(
        item.expires_on is None or item.expires_on > now + timedelta(seconds=remaining),
        "version_expiring",
    )


def tagged(item, run_id, phase, predecessor):
    tags = item.tags or {}
    return (
        tags.get("session-drill-run") == run_id
        and tags.get("session-drill-phase") == phase
        and tags.get("session-drill-predecessor") == predecessor
    )


class Rotation:
    def __init__(self, client, run_id, expected_hash, now=utc, sleep=time.sleep):
        require(str(UUID(run_id)) == run_id, "run_id_invalid")
        require(bool(HASH.fullmatch(expected_hash)), "hash_invalid")
        self.client, self.run_id, self.expected = client, run_id, expected_hash
        self.now, self.sleep = now, sleep
        self.old = None
        self.target = None
        self.old_value = None
        self.new_value = None
        self.attempted = set()

    def preflight(self):
        rows = history(self.client, self.now())
        require(
            not any((r.tags or {}).get("session-drill-run") == self.run_id for r in rows),
            "preexisting_run",
        )
        self.old = rows[0]
        require(digest(self.old.version) == self.expected, "baseline_changed")
        usable(self.old, self.now(), WATCHDOG + 300)
        # A recent current version is already an overlap experiment, even if untagged.
        require(
            (self.now() - self.old.created_on).total_seconds() > OVERLAP + CLOCK_MARGIN,
            "preexisting_rotation",
        )
        value = self.client.get_secret(SECRET, self.old.version)
        require(
            value.properties.version == self.old.version and bool(value.value),
            "old_value_unavailable",
        )
        self.old_value = value.value

    def reconcile(self, phase, predecessor):
        """Read-only reconciliation; absence is NOT permission to repeat a set."""
        for attempt in range(4):
            rows = history(self.client, self.now())
            own = [r for r in rows if tagged(r, self.run_id, phase, predecessor)]
            if own:
                require(len(own) == 1 and own[0].version == rows[0].version, "write_conflict")
                usable(own[0], self.now(), WATCHDOG + 300 if phase == "rotate" else 0)
                return own[0]
            if attempt != 3:
                self.sleep(2)
        raise Blocked("write_outcome_unknown")

    def write_once(self, phase, predecessor, value):
        require(phase not in self.attempted, "write_already_attempted")
        rows = history(self.client, self.now())
        existing = [r for r in rows if tagged(r, self.run_id, phase, predecessor)]
        if existing:
            require(len(existing) == 1 and rows[0].version == existing[0].version, "write_conflict")
            return existing[0]
        require(digest(rows[0].version) == predecessor, "foreign_latest")
        # Key Vault timestamps have second precision; keep the direct predecessor unambiguous.
        while self.now() <= rows[0].created_on + timedelta(seconds=1):
            self.sleep(0.25)
        rows = history(self.client, self.now())
        require(digest(rows[0].version) == predecessor, "prewrite_race")
        self.attempted.add(phase)
        from azure.core.exceptions import AzureError

        try:
            self.client.set_secret(
                SECRET,
                value,
                enabled=True,
                expires_on=self.now() + timedelta(days=7),
                tags={
                    "session-drill-run": self.run_id,
                    "session-drill-phase": phase,
                    "session-drill-predecessor": predecessor,
                },
                retry_total=0,
                logging_enable=False,
            )
        except (AzureError, TimeoutError):
            # Even a timeout after a committed set is not retried.
            pass
        return self.reconcile(phase, predecessor)

    def rotate(self):
        require(self.old_value is not None, "preflight_required")
        import secrets

        self.new_value = secrets.token_urlsafe(64)
        emit(self.run_id, "write_intent")
        self.target = self.write_once("rotate", self.expected, self.new_value)
        emit(self.run_id, "rotated", digest(self.target.version), self.target.created_on)
        return self.target

    def recover(self):
        rows = history(self.client, self.now())
        rotations = [r for r in rows if tagged(r, self.run_id, "rotate", self.expected)]
        require(len(rotations) == 1, "rotation_unprovable")
        target = rotations[0]
        target_hash = digest(target.version)
        recovered = [r for r in rows if tagged(r, self.run_id, "recover", target_hash)]
        if recovered:
            require(
                len(recovered) == 1 and rows[0].version == recovered[0].version, "recovery_conflict"
            )
            emit(self.run_id, "recovered", digest(recovered[0].version), recovered[0].created_on)
            return recovered[0]
        require(rows[0].version == target.version, "foreign_latest")
        previous = [r for r in rows if digest(r.version) == self.expected]
        require(
            len(previous) == 1 and rows[1].version == previous[0].version, "predecessor_unprovable"
        )
        usable(previous[0], self.now(), 0)
        if self.old_value is None:
            value = self.client.get_secret(SECRET, previous[0].version)
            require(
                value.properties.version == previous[0].version and bool(value.value),
                "recovery_value_unavailable",
            )
            self.old_value = value.value
        recovered = self.write_once("recover", target_hash, self.old_value)
        emit(self.run_id, "recovered", digest(recovered.version), recovered.created_on)
        return recovered


def decode_cookie(cookie, key):
    import itsdangerous

    try:
        raw = itsdangerous.TimestampSigner(key).unsign(cookie, max_age=14 * 86400)
        require(len(raw) <= 16384, "cookie_size")
        data = json.loads(base64.b64decode(raw, validate=True))
    except (itsdangerous.BadSignature, ValueError):
        raise Blocked("cookie_invalid") from None
    require(isinstance(data, dict), "cookie_shape")
    return data


def anonymous(data, csrf=False):
    states = [key for key in data if key.startswith("_state_entra_id_")]
    expected = {"auth_nonce", *states}
    if csrf:
        expected.add("csrf_token")
    require(len(states) == 1 and set(data) == expected, "anonymous_markers_invalid")
    require(
        isinstance(data["auth_nonce"], str) and 16 <= len(data["auth_nonce"]) <= 256,
        "nonce_invalid",
    )
    state = data[states[0]]
    require(isinstance(state, dict) and set(state) == {"data", "exp"}, "state_invalid")
    require(isinstance(state["exp"], (float, int)), "state_expiry_invalid")
    details = state["data"]
    require(
        isinstance(details, dict)
        and set(details) <= {"redirect_uri", "url", "nonce", "code_verifier"}
        and {"redirect_uri", "url", "nonce"} <= set(details)
        and details["nonce"] == data["auth_nonce"],
        "state_data_invalid",
    )
    return {key: data[key] for key in ("auth_nonce", states[0])}


class CookieProbe:
    def __init__(self, client, old_key):
        self.client = client
        self.client.cookies.clear()
        response = self.client.get("/auth/login", follow_redirects=False)
        require(response.status_code in (302, 303, 307), "login_not_redirect")
        self.frozen = self.cookie(response)
        self.markers = anonymous(decode_cookie(self.frozen, old_key))
        self.check(old_key, accepted=True)

    @staticmethod
    def cookie(response):
        cookies = SimpleCookie()
        for header in response.headers.get_list("set-cookie"):
            cookies.load(header)
        require(
            "session" in cookies and len(cookies["session"].value) <= 16384,
            "session_cookie_missing",
        )
        return cookies["session"].value

    def check(self, key, accepted):
        # Explicit frozen header, never the client's refreshed jar.
        self.client.cookies.clear()
        response = self.client.get(
            "/",
            headers={"Cookie": "session=" + self.frozen},
            follow_redirects=False,
        )
        require(response.status_code == 200, "home_unavailable")
        data = decode_cookie(self.cookie(response), key)
        require(
            isinstance(data.get("csrf_token"), str) and bool(data["csrf_token"]), "csrf_missing"
        )
        if accepted:
            require(anonymous(data, csrf=True) == self.markers, "old_cookie_rejected")
        else:
            require(set(data) == {"csrf_token"}, "old_cookie_still_accepted")

    def fresh(self, key):
        probe = CookieProbe(self.client, key)
        probe.check(key, accepted=True)


def experiment(rotation, cookies, monotonic=time.monotonic, sleep=time.sleep):
    start = monotonic()
    target = rotation.rotate()
    created = target.created_on
    # Early probes wait for the refresh budget; provider timing is independently observed.
    milestones = [
        (65, "cookie_early", True),
        (OVERLAP - 15, "cookie_near", True),
        (OVERLAP + CLOCK_MARGIN + 2, "cookie_boundary", False),
    ]
    for seconds, stage, accepted in milestones:
        while rotation.now() < created + timedelta(seconds=seconds):
            require(monotonic() - start < WATCHDOG, "watchdog")
            sleep(
                min(
                    5,
                    max(
                        0.01,
                        (created + timedelta(seconds=seconds) - rotation.now()).total_seconds(),
                    ),
                )
            )
        require(
            abs((rotation.now() - created).total_seconds() - seconds) < 5, "probe_deadline_missed"
        )
        rows = history(rotation.client, rotation.now())
        require(rows[0].version == target.version, "foreign_latest")
        cookies.check(rotation.new_value, accepted)
        if not accepted:
            cookies.fresh(rotation.new_value)
        emit(rotation.run_id, stage, digest(target.version), created)
    emit(rotation.run_id, "awaiting_controller_stop", digest(target.version), created)
    # No success-shaped exit: only the independent controller can acknowledge all workers
    # by stopping this execution. Loss of that controller causes a rollback at 4200s.
    while monotonic() - start < WATCHDOG:
        sleep(5)
    raise Blocked("watchdog")


def deadline(_signum, _frame):
    raise TimeoutError("deadline")


def unexpected(_type, _value, _traceback):
    print('{"schema":"session-rotation/v1","stage":"unexpected_failure"}', flush=True)


def main():
    logging.disable(logging.CRITICAL)
    sys.excepthook = unexpected
    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(4400)
    rotation = None
    run_id = os.environ.get("SESSION_RUN_ID", "")
    try:
        import httpx
        from azure.core.pipeline.transport import RequestsTransport
        from azure.identity import ManagedIdentityCredential
        from azure.keyvault.secrets import SecretClient

        client_id = str(UUID(os.environ["RUNNER_CLIENT_ID"]))
        run_id = str(UUID(run_id))
        expected = os.environ["SESSION_EXPECTED_HASH"]
        app_host = os.environ["SESSION_APP_HOST"]
        require(
            re.fullmatch(r"fcag-dev-app\.[a-z0-9.-]+\.azurecontainerapps\.io", app_host),
            "app_host_invalid",
        )
        private_dns()
        with (
            ManagedIdentityCredential(
                client_id=client_id, retry_total=0, connection_timeout=5, read_timeout=5
            ) as credential,
            SecretClient(
                vault_url=f"https://{HOST}",
                credential=credential,
                transport=RequestsTransport(use_env_settings=False),
                retry_total=0,
                connection_timeout=5,
                read_timeout=5,
                logging_enable=False,
            ) as client,
            httpx.Client(
                base_url=f"https://{app_host}", timeout=10, trust_env=False, follow_redirects=False
            ) as http,
        ):
            rotation = Rotation(client, run_id, expected)
            try:
                rows = history(client, utc())
                if any((r.tags or {}).get("session-drill-run") == run_id for r in rows):
                    # A separately authorized restart is recovery-only, never a second rotation.
                    rotation.recover()
                    return 0
                rotation.preflight()
                cookies = CookieProbe(http, rotation.old_value)
                emit(run_id, "preflight")
                experiment(rotation, cookies)
            except Exception:
                # Terminal safety/recovery boundary; never stringify or chain SDK/HTTP errors.
                # Reconciliation failure forbids guessing which value is current.
                if rotation.attempted or rotation.target is not None:
                    try:
                        rotation.recover()
                    except Exception:
                        emit(run_id, "manual_recovery_required")
                        return 2
                emit(run_id, "blocked")
                return 1
    except Exception:
        unexpected(None, None, None)
        return 2
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    sys.exit(main())
