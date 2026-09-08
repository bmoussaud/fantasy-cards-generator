"""Offline probe contracts; no Azure credential or serving container required."""

import base64
import importlib
import json
import logging
import sys
import time
import urllib.error
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

ENDPOINT = "https://example.services.ai.azure.com/api/projects/example-dev"
PRINCIPAL = "11111111-1111-4111-8111-111111111111"
PROJECT = Path(__file__).resolve().parents[1] / "deployments/card-orchestrator"


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(PROJECT))
    previous = logging.root.manager.disable
    payload = importlib.import_module("aca_identity_payload")
    wrapper = importlib.import_module("aca_identity_probe")
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://localhost/identity")
    monkeypatch.setenv("IDENTITY_HEADER", "synthetic-test-header")
    yield payload, wrapper
    logging.disable(previous)


def token(**overrides):
    claims = {"oid": PRINCIPAL, "aud": "https://ai.azure.com", "exp": time.time() + 3600}
    claims.update(overrides)
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()
    return "synthetic." + encoded + ".synthetic"


class Credential:
    def __init__(self, value=None):
        self.value = value or token()
        self.closed = False
        self.options = None

    def factory(self, **kwargs):
        self.options = kwargs
        return self

    def get_token(self, scope):
        assert scope == "https://ai.azure.com/.default"
        return SimpleNamespace(token=self.value)

    def close(self):
        self.closed = True


class Opener:
    def __init__(self, body=b'{"data":[]}', status=200, error=None):
        self.body, self.status, self.error = body, status, error
        self.request = None

    def open(self, request, timeout):
        assert timeout == 10
        self.request = request
        if self.error:
            raise self.error
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, limit):
        assert limit == 65537
        return self.body[:limit]


def test_empty_list_proves_access_only_with_explicit_identity(modules):
    payload, _ = modules
    credential, opener = Credential(), Opener()
    result = payload.probe(ENDPOINT, PRINCIPAL, credential.factory, opener)
    assert result["status"] == "access_verified"
    assert result["httpStatus"] == 200
    assert result["agentCountOnPage"] == 0
    assert result["principalMatched"] is True
    assert result["invocationVerified"] is False
    assert result["endpointPersisted"] is False
    assert credential.options == {
        "client_id": None,
        "connection_timeout": 5,
        "read_timeout": 10,
        "retry_total": 0,
    }
    assert credential.closed
    assert opener.request.method == "GET"
    assert opener.request.full_url == ENDPOINT + "/agents?api-version=2025-11-15-preview"
    assert opener.request.data is None
    assert credential.value not in json.dumps(result)


@pytest.mark.parametrize(
    "claims",
    [
        {"oid": "wrong"},
        {"aud": "https://management.azure.com/"},
        {"exp": 1},
    ],
)
def test_claim_mismatch_blocks_data_plane(modules, claims):
    payload, _ = modules
    credential, opener = Credential(token(**claims)), Opener()
    result = payload.probe(ENDPOINT, PRINCIPAL, credential.factory, opener)
    assert result["reason"] == "identity_claim_mismatch"
    assert opener.request is None
    assert credential.closed


def test_missing_aca_identity_never_falls_back(modules, monkeypatch):
    payload, _ = modules
    monkeypatch.delenv("IDENTITY_HEADER")
    credential = Credential()
    assert (
        payload.probe(ENDPOINT, PRINCIPAL, credential.factory)["reason"]
        == "aca_identity_unavailable"
    )
    assert credential.options is None


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.services.ai.azure.com/api/projects/example-dev",
        ENDPOINT + "?redirect=evil",
        ENDPOINT + "/agents",
        "https://example.services.ai.azure.com.evil/api/projects/example",
        "https://user:password@example.services.ai.azure.com/api/projects/example",
    ],
)
def test_endpoint_rejects_untrusted_destinations(modules, endpoint):
    payload, wrapper = modules
    with pytest.raises(ValueError):
        wrapper.remote_command(endpoint, PRINCIPAL)
    assert payload.probe(endpoint, PRINCIPAL)["reason"] == "invalid_configuration"


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 429, 500])
def test_http_errors_never_count_as_invocation_or_access(modules, status):
    payload, _ = modules
    error = urllib.error.HTTPError(ENDPOINT, status, "private-service-body", {}, None)
    credential = Credential()
    result = payload.probe(ENDPOINT, PRINCIPAL, credential.factory, Opener(error=error))
    assert result["httpStatus"] == status
    assert not result["accessVerified"] and not result["invocationVerified"]
    assert "private-service-body" not in json.dumps(result)
    assert credential.closed


@pytest.mark.parametrize(
    "body,reason",
    [
        (b'{"value":[]}', "invalid_list_response"),
        (b"[]", "invalid_list_response"),
        (b'{"data":{}}', "invalid_list_response"),
        (b"x" * 65537, "response_too_large"),
        (b"private-invalid-json", "probe_failed"),
    ],
)
def test_response_shape_and_size_are_bounded(modules, body, reason):
    payload, _ = modules
    result = payload.probe(ENDPOINT, PRINCIPAL, Credential().factory, Opener(body=body))
    assert result["reason"] == reason
    assert not result["accessVerified"]


def test_redirects_cannot_forward_bearer(modules):
    payload, _ = modules
    assert payload.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil") is None


def test_payload_is_exact_reviewable_source_without_container_writes(modules):
    _, wrapper = modules
    command = wrapper.remote_command(ENDPOINT, PRINCIPAL).split()
    assert command[:2] == ["/app/.venv/bin/python", "-c"]
    encoded = command[2].split("b64decode('")[1].split("'")[0]
    source = zlib.decompress(base64.b64decode(encoded)).decode()
    assert len(wrapper.remote_command(ENDPOINT, PRINCIPAL)) < 8000
    assert (
        source
        == (PROJECT / "aca_identity_payload.py").read_text()
        + f"\nemit({ENDPOINT!r}, {PRINCIPAL!r})\n"
    )
    assert "DefaultAzureCredential" not in source


def test_wrapper_discards_raw_output_and_forged_result(modules):
    payload, wrapper = modules
    assert wrapper.extract_result("private-cli-traceback") is None
    assert wrapper.extract_result(payload.MARKER + '{"status":"access_verified"}') is None
    result = payload.probe(ENDPOINT, PRINCIPAL, Credential().factory, Opener())
    assert wrapper.extract_result("cli chatter\r\n" + payload.MARKER + json.dumps(result)) == result
    result["token"] = "do-not-export"
    assert wrapper.extract_result(payload.MARKER + json.dumps(result)) is None


def test_exec_uses_real_pty_but_never_exports_process_output(modules):
    _, wrapper = modules
    result = wrapper.execute(
        [sys.executable, "-c", "import os;assert os.isatty(0);print('private')"]
    )
    assert result == {"status": "failed", "reason": "exec_no_evidence"}


def test_exec_timeout_is_bounded(modules):
    _, wrapper = modules
    result = wrapper.execute([sys.executable, "-c", "import time;time.sleep(10)"], timeout=0.05)
    assert result == {"status": "failed", "reason": "exec_timeout"}


def test_exec_transmits_source_only_after_connection_over_pty(modules):
    payload, wrapper = modules
    result = payload.probe(ENDPOINT, PRINCIPAL, Credential().factory, Opener())
    program = (
        "import tty;tty.setcbreak(0);"
        "print('INFO: Successfully connected to container:',flush=True);"
        "assert input()=='reviewed-source';"
        f"print({payload.MARKER!r}+{json.dumps(result)!r},flush=True)"
    )
    assert wrapper.execute([sys.executable, "-c", program], input_line="reviewed-source") == result


@pytest.fixture
def fake_pty(modules, monkeypatch):
    _, wrapper = modules
    state = SimpleNamespace(
        now=0.0, events=[], writes=[], write_size=1024, blocked_until=0,
        write_interval=0, next_write=0, block_once=False, launches=0, terminated=False,
    )
    process = SimpleNamespace(
        stdout=SimpleNamespace(close=lambda: None),
        poll=lambda: None,
        wait=lambda timeout: None,
    )

    def terminate():
        state.terminated = True

    process.terminate = terminate

    def launch(*args, **kwargs):
        state.launches += 1
        return process

    class Selector:
        def __init__(self):
            self.registered = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def register(self, file, event):
            fd = 11 if file is process.stdout else file
            self.registered[fd] = event

        def unregister(self, fd):
            del self.registered[fd]

        def select(self, timeout):
            ready = {}
            if state.events:
                ready[11] = state.events[0][0]
            if 12 in self.registered:
                ready[12] = max(state.blocked_until, state.next_write, state.now)
            state.now = min(state.now + timeout, max(state.now, min(ready.values()))) \
                if ready else state.now + timeout
            return [
                (SimpleNamespace(fd=fd), self.registered[fd])
                for fd, at in ready.items() if at <= state.now
            ]

    def read(fd, size):
        assert fd == 11
        return state.events.pop(0)[1]

    def write(fd, pending):
        assert fd == 12
        state.next_write = state.now + state.write_interval
        if state.block_once:
            state.block_once = False
            raise BlockingIOError()
        part = bytes(pending[:state.write_size])
        state.writes.append((state.now, part))
        return len(part)

    monkeypatch.setattr(wrapper.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(wrapper.pty, "openpty", lambda: (12, 13))
    monkeypatch.setattr(wrapper.os, "set_blocking", lambda fd, blocking: None)
    monkeypatch.setattr(wrapper.os, "close", lambda fd: None)
    monkeypatch.setattr(wrapper.os, "read", read)
    monkeypatch.setattr(wrapper.os, "write", write)
    monkeypatch.setattr(wrapper.subprocess, "Popen", launch)
    monkeypatch.setattr(wrapper.selectors, "DefaultSelector", Selector)
    return state


def test_delayed_split_connection_gets_full_result_budget(modules, fake_pty):
    payload, wrapper = modules
    result = payload.probe(ENDPOINT, PRINCIPAL, Credential().factory, Opener())
    fake_pty.events = [
        (12, b"INFO: Successfully connec"),
        (13, b"ted to container:\n"),
        (14, b"INFO: Successfully connected to container:\n"),
        (92, (payload.MARKER + json.dumps(result) + "\n").encode()),
    ]
    assert wrapper.execute(
        ["offline"], input_line="reviewed-source", timeout=80,
        setup_timeout=30, total_timeout=110,
    ) == result
    assert fake_pty.writes == [(13.2, b"reviewed-source\n")]
    assert fake_pty.now == 92
    assert fake_pty.launches == 1


@pytest.mark.parametrize("phase", ["connect", "settle", "transfer"])
def test_setup_deadline_covers_connection_settling_and_blocked_transfer(
    modules, fake_pty, phase
):
    _, wrapper = modules
    if phase != "connect":
        fake_pty.events = [
            (29.9 if phase == "settle" else 1, b"Successfully connected to container:\n")
        ]
    fake_pty.blocked_until = 31
    assert wrapper.execute(
        ["offline"], input_line="reviewed-source", timeout=80,
        setup_timeout=30, total_timeout=110,
    ) == {"status": "failed", "reason": "exec_setup_timeout"}
    assert fake_pty.now == 30
    assert fake_pty.writes == []
    assert fake_pty.launches == 1 and fake_pty.terminated


def test_partial_transfer_is_bounded_without_retransmission(modules, fake_pty):
    _, wrapper = modules
    fake_pty.events = [
        (1, b"Successfully connected to container:\n"),
        (4, b"Successfully connected to container:\n"),
    ]
    fake_pty.write_size = 2
    fake_pty.write_interval = 10
    fake_pty.block_once = True
    assert wrapper.execute(
        ["offline"], input_line="reviewed-source", timeout=80,
        setup_timeout=30, total_timeout=110,
    ) == {"status": "failed", "reason": "exec_setup_timeout"}
    assert b"".join(part for _, part in fake_pty.writes) == b"revi"
    assert fake_pty.now == 30 and fake_pty.launches == 1


def test_result_budget_starts_after_last_partial_write(modules, fake_pty):
    payload, wrapper = modules
    result = payload.probe(ENDPOINT, PRINCIPAL, Credential().factory, Opener())
    fake_pty.events = [
        (1, b"Successfully connected to container:\n"),
        (5, b"Successfully connected to container:\n"),
        (100, (payload.MARKER + json.dumps(result) + "\n").encode()),
    ]
    fake_pty.write_size = 4
    fake_pty.write_interval = 10
    assert wrapper.execute(
        ["offline"], input_line="abcdefghijk", timeout=80,
        setup_timeout=30, total_timeout=110,
    ) == result
    assert b"".join(part for _, part in fake_pty.writes) == b"abcdefghijk\n"
    assert fake_pty.writes[-1][0] == 21.2
    assert fake_pty.launches == 1


@pytest.mark.parametrize(
    "setup,total,connect,reason,elapsed",
    [
        (30, 110, 10, "exec_timeout", 90.2),
        (50, 110, 40, "exec_total_timeout", 110),
        (50, 20, None, "exec_total_timeout", 20),
    ],
)
def test_result_and_overall_deadlines_do_not_retry(
    modules, fake_pty, setup, total, connect, reason, elapsed
):
    _, wrapper = modules
    if connect is not None:
        fake_pty.events = [(connect, b"Successfully connected to container:\n")]
    assert wrapper.execute(
        ["offline"], input_line="reviewed-source", timeout=80,
        setup_timeout=setup, total_timeout=total,
    ) == {"status": "failed", "reason": reason}
    assert fake_pty.now == elapsed
    assert len(fake_pty.writes) == (0 if connect is None else 1)
    assert fake_pty.launches == 1 and fake_pty.terminated


def test_access_only_deadline_does_not_reset_after_delivery(modules, fake_pty):
    _, wrapper = modules
    fake_pty.events = [(60, b"Successfully connected to container:\n")]
    assert wrapper.execute(["offline"], input_line="reviewed-source") == {
        "status": "failed", "reason": "exec_timeout",
    }
    assert fake_pty.now == 75 and len(fake_pty.writes) == 1


def test_execution_pins_target_and_bounds_remote_input_wait(modules, monkeypatch):
    _, wrapper = modules
    args = [
        "--environment",
        "dev",
        "--project-endpoint",
        ENDPOINT,
        "--expected-principal",
        PRINCIPAL,
        "--execute",
    ]
    for name in ("subscription", "resource-group", "app", "revision", "replica", "container"):
        args.extend(["--" + name, "synthetic"])

    def execute(command, input_line):
        assert command[command.index("--command") + 1] == (
            "/app/.venv/bin/python -c __import__('signal').alarm(60);exec(input())"
        )
        assert command[command.index("--replica") + 1] == "synthetic"
        assert command[command.index("--revision") + 1] == "synthetic"
        assert input_line.startswith("exec(__import__('zlib').decompress(")
        return {"status": "failed", "reason": "exec_no_evidence"}

    monkeypatch.setattr(wrapper, "execute", execute)
    assert wrapper.main(args) == 1


def test_invocation_selects_phased_deadlines_and_consumes_allowance(modules, monkeypatch, capsys):
    _, wrapper = modules
    calls = []

    def execute(command, **kwargs):
        calls.append(kwargs)
        assert command[command.index("--command") + 1].endswith(
            "source=''.join(iter(input,'END'));__import__('signal').alarm(5);exec(source)"
        )
        return {"status": "failed", "reason": "exec_setup_timeout"}

    monkeypatch.setattr(wrapper, "execute", execute)
    args = [
        "--environment", "dev", "--project-endpoint", ENDPOINT,
        "--expected-principal", PRINCIPAL, "--execute", "--invoke-once",
        "--hosted-version", "1", "--expected-version", "a" * 40, "--session-id", "session-1",
    ]
    for name in ("subscription", "resource-group", "app", "revision", "replica", "container"):
        args.extend(["--" + name, "synthetic"])
    assert wrapper.main(args) == 1
    assert len(calls) == 1
    assert calls[0]["timeout"] == 80
    assert calls[0]["setup_timeout"] == 30
    assert calls[0]["total_timeout"] == 110
    assert calls[0]["input_line"].endswith("\nEND")
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed", "reason": "exec_setup_timeout", "invocationAllowanceConsumed": True,
    }


def test_plan_only_and_prod_rejection(modules, capsys):
    _, wrapper = modules
    args = [
        "--environment",
        "dev",
        "--project-endpoint",
        ENDPOINT,
        "--expected-principal",
        PRINCIPAL,
    ]
    for name in ("subscription", "resource-group", "app", "revision", "replica", "container"):
        args.extend(["--" + name, "synthetic"])
    assert wrapper.main(args) == 0
    assert "PLAN ONLY" in capsys.readouterr().out
    args[1] = "prod"
    with pytest.raises(SystemExit):
        wrapper.main(args)


def invocation_parser(payload, wrapper, monkeypatch):
    namespace = {"__name__": "aca_identity_payload"}
    exec(wrapper.parser_source(), namespace)
    for name in ("_parse_success_envelope", "_extract_output_text"):
        monkeypatch.setattr(payload, name, namespace[name], raising=False)


@pytest.mark.parametrize("status", ["completed", "refused", "held", "routing_defer"])
def test_single_invocation_real_parser_and_versions(modules, monkeypatch, status):
    payload, wrapper = modules
    invocation_parser(payload, wrapper, monkeypatch)
    build, version, session = "a" * 40, "1", "session-1"
    card = {
        "schemaVersion": 1, "name": "Lantern Guardian", "cardType": "creature",
        "rarity": "common", "manaCost": 2, "attack": 1, "health": 3,
        "rulesText": "Protect one friendly creature.", "flavorText": "",
        "artBrief": "An original guardian with a lantern in a peaceful forest.",
    }
    domain = {
        "schemaVersion": 1, "status": status,
        "card": card if status == "completed" else None,
        "artPrompt": "Original woodland guardian." if status == "completed" else None,
        "metadata": {"agentVersion": build, "hostedVersion": version},
    }
    body = json.dumps({
        "id": "resp-test", "status": "completed",
        "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(domain)}
        ]}],
    }).encode()

    class SingleOpener(Opener):
        calls = 0

        def open(self, request, timeout):
            self.calls += 1
            assert timeout == 65
            assert request.method == "POST"
            wire = json.loads(request.data)
            assert wire["session_id"] == session
            assert wire["store"] is False and wire["stream"] is False
            assert len(wire["input"]) == 1
            assert json.loads(wire["input"][0]["content"][0]["text"])["schemaVersion"] == 1
            self.request = request
            return self

    opener = SingleOpener(body=body)
    result = payload.probe(
        ENDPOINT, PRINCIPAL, Credential().factory, opener, (version, build, session)
    )
    assert opener.calls == 1
    assert result["invocationsAttempted"] == 1
    assert result["invocationVerified"] and result["schemaValid"]
    assert result["outcome"] == status
    assert wrapper.extract_result(payload.MARKER + json.dumps(result)) == result
    assert "Lantern Guardian" not in json.dumps(result)
    assert "artPrompt" not in json.dumps(result)


def test_invocation_timeout_consumes_attempt_without_retry(modules, monkeypatch):
    payload, wrapper = modules
    invocation_parser(payload, wrapper, monkeypatch)

    class TimeoutOpener:
        calls = 0

        def open(self, request, timeout):
            self.calls += 1
            raise TimeoutError()

    opener = TimeoutOpener()
    result = payload.probe(
        ENDPOINT, PRINCIPAL, Credential().factory, opener, ("1", "a" * 40, "session-1")
    )
    assert result["reason"] == "timeout"
    assert result["invocationsAttempted"] == opener.calls == 1
    assert not result["invocationVerified"]


def test_http_200_without_card_is_not_success(modules, monkeypatch):
    payload, wrapper = modules
    invocation_parser(payload, wrapper, monkeypatch)
    result = payload._parse_success_envelope(
        {"status": "completed", "output": []}, request_id=None, expected_version="a" * 40
    )
    assert not result.success and not result.schema_valid
    with pytest.raises(ValueError):
        wrapper.remote_command(ENDPOINT, PRINCIPAL, ("1", "not-a-sha", "session-1"))


def test_large_invocation_payload_survives_canonical_terminal(modules):
    payload, wrapper = modules
    result = payload.probe(ENDPOINT, PRINCIPAL, Credential().factory, Opener())
    expression = "padding=" + repr("x" * 6000) + ";" + (
        f"print({payload.MARKER!r}+{json.dumps(result)!r},flush=True)"
    )
    startup, input_line = wrapper.stdin_payload(
        "/app/.venv/bin/python -c " + expression, invocation=True
    )
    assert max(map(len, input_line.splitlines())) <= 1024
    program = (
        "print('INFO: Successfully connected to container:',flush=True);"
        + startup.split(" ", 2)[2]
    )
    # Deliberately keep canonical mode (unlike ACA's local CLI PTY test above).
    assert wrapper.execute([sys.executable, "-c", program], input_line=input_line) == result
