"""Offline probe contracts; no Azure credential or serving container required."""

import base64
import importlib
import io
import json
import logging
import signal
import subprocess
import sys
import time
import urllib.error
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

ENDPOINT = "https://example.services.ai.azure.com/api/projects/example-dev"
PRINCIPAL = "11111111-1111-4111-8111-111111111111"
SESSION = "smoke-109-" + "a" * 32
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
    startup, lines = wrapper.stdin_payload(wrapper.remote_command(ENDPOINT, PRINCIPAL))
    assert len(startup) < 2000 and max(map(len, lines.splitlines())) <= 1024
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
        now=0.0,
        events=[],
        writes=[],
        write_size=1024,
        blocked_until=0,
        write_interval=0,
        next_write=0,
        block_once=False,
        launches=0,
        terminated=False,
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
            state.now = (
                min(state.now + timeout, max(state.now, min(ready.values())))
                if ready
                else state.now + timeout
            )
            return [
                (SimpleNamespace(fd=fd), self.registered[fd])
                for fd, at in ready.items()
                if at <= state.now
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
        part = bytes(pending[: state.write_size])
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
    assert (
        wrapper.execute(
            ["offline"],
            input_line="reviewed-source",
            timeout=80,
            setup_timeout=30,
            total_timeout=110,
        )
        == result
    )
    assert fake_pty.writes == [(13.2, b"reviewed-source\n")]
    assert fake_pty.now == 92
    assert fake_pty.launches == 1


@pytest.mark.parametrize("phase", ["connect", "settle", "transfer"])
def test_setup_deadline_covers_connection_settling_and_blocked_transfer(modules, fake_pty, phase):
    _, wrapper = modules
    if phase != "connect":
        fake_pty.events = [
            (29.9 if phase == "settle" else 1, b"Successfully connected to container:\n")
        ]
    fake_pty.blocked_until = 31
    assert wrapper.execute(
        ["offline"],
        input_line="reviewed-source",
        timeout=80,
        setup_timeout=30,
        total_timeout=110,
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
        ["offline"],
        input_line="reviewed-source",
        timeout=80,
        setup_timeout=30,
        total_timeout=110,
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
    assert (
        wrapper.execute(
            ["offline"],
            input_line="abcdefghijk",
            timeout=80,
            setup_timeout=30,
            total_timeout=110,
        )
        == result
    )
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
        ["offline"],
        input_line="reviewed-source",
        timeout=80,
        setup_timeout=setup,
        total_timeout=total,
    ) == {"status": "failed", "reason": reason}
    assert fake_pty.now == elapsed
    assert len(fake_pty.writes) == (0 if connect is None else 1)
    assert fake_pty.launches == 1 and fake_pty.terminated


def test_access_only_deadline_does_not_reset_after_delivery(modules, fake_pty):
    _, wrapper = modules
    fake_pty.events = [(60, b"Successfully connected to container:\n")]
    assert wrapper.execute(["offline"], input_line="reviewed-source") == {
        "status": "failed",
        "reason": "exec_timeout",
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
            "/app/.venv/bin/python -c __import__('signal').alarm(60);"
            "exec(''.join(iter(input,'END')))"
        )
        assert command[command.index("--replica") + 1] == "synthetic"
        assert command[command.index("--revision") + 1] == "synthetic"
        assert input_line.startswith("__import__('sys').dont_write_bytecode=True;exec(")
        assert input_line.endswith("\nEND")
        assert max(map(len, input_line.splitlines())) <= 1024
        return {"status": "failed", "reason": "exec_no_evidence"}

    monkeypatch.setattr(wrapper, "execute", execute)
    assert wrapper.main(args) == 1


def test_invocation_selects_phased_deadlines_and_consumes_allowance(modules, monkeypatch, capsys):
    _, wrapper = modules
    calls = []

    def execute(command, **kwargs):
        calls.append(kwargs)
        startup = command[command.index("--command") + 1]
        encoded = startup.split("b64decode('")[1].split("'")[0]
        bootstrap = zlib.decompress(base64.b64decode(encoded)).decode()
        assert "signal.signal(signal.SIGALRM,deadline)" in bootstrap
        assert "signal.alarm(30)" in bootstrap and "signal.alarm(95)" in bootstrap
        assert "alarm(5)" not in bootstrap
        return {"status": "failed", "reason": "exec_setup_timeout"}

    monkeypatch.setattr(wrapper, "execute", execute)
    args = [
        "--environment",
        "dev",
        "--project-endpoint",
        ENDPOINT,
        "--expected-principal",
        PRINCIPAL,
        "--execute",
        "--invoke-once",
        "--hosted-version",
        "1",
        "--expected-version",
        "a" * 40,
        "--session-id",
        SESSION,
    ]
    for name in ("subscription", "resource-group", "app", "revision", "replica", "container"):
        args.extend(["--" + name, "synthetic"])
    assert wrapper.main(args) == 1
    assert len(calls) == 1
    assert calls[0]["timeout"] == 100
    assert calls[0]["setup_timeout"] == 10
    assert calls[0]["total_timeout"] == 110
    assert calls[0]["input_line"].endswith("\nEND")
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "reason": "exec_setup_timeout",
        "invocationAllowanceConsumed": True,
        "sessionCreationUnknown": True,
        "sessionCleanupRequired": True,
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
    for name in ("GenerateCardAgentRequest", "_parse_success_envelope", "_extract_output_text"):
        monkeypatch.setattr(payload, name, namespace[name], raising=False)


def session_resource(**overrides):
    return {
        "agent_session_id": SESSION,
        "version_indicator": {"type": "version_ref", "agent_version": "1"},
        "status": "active",
        "created_at": 1788871606,
        "last_accessed_at": 1788871606,
        "expires_at": 1791463606,
        **overrides,
    }


class SequenceOpener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.timeouts = []

    def open(self, request, timeout):
        self.requests.append(request)
        self.timeouts.append(timeout)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        status, document = response
        body = document if isinstance(document, bytes) else json.dumps(document).encode()
        return Opener(body=body, status=status)


def test_session_and_invocation_share_one_checked_token_context(modules, monkeypatch):
    payload, wrapper = modules
    invocation_parser(payload, wrapper, monkeypatch)
    credential = Credential()
    token_calls = []
    original = credential.get_token
    monkeypatch.setattr(
        credential, "get_token", lambda scope: (token_calls.append(scope), original(scope))[1]
    )
    opener = SequenceOpener((201, session_resource()), (200, {"output": []}))
    result = payload.probe(
        ENDPOINT, PRINCIPAL, credential.factory, opener, ("1", "a" * 40, SESSION)
    )
    create, invoke = opener.requests
    assert (
        create.full_url == ENDPOINT + "/agents/card-orchestrator/endpoint/sessions?api-version=v1"
    )
    assert json.loads(create.data) == {
        "agent_session_id": SESSION,
        "version_indicator": {"type": "version_ref", "agent_version": "1"},
    }
    assert invoke.full_url == (
        ENDPOINT + "/agents/card-orchestrator/endpoint/protocols/openai/responses?api-version=v1"
    )
    assert create.method == invoke.method == "POST"
    assert (
        create.headers
        == invoke.headers
        == {
            "Authorization": "Bearer " + credential.value,
            "Accept": "application/json",
            "Content-type": "application/json",
        }
    )
    assert token_calls == [payload.SCOPE] and credential.closed
    assert result["sessionCreateAttempted"] and result["sessionCreated"] and result["sessionReady"]
    assert result["sessionCleanupRequired"] and result["invocationsAttempted"] == 1
    assert result["phase"] == "invoke"
    assert wrapper.extract_result(payload.MARKER + json.dumps(result)) == result


@pytest.mark.parametrize(
    "response,reason,cleanup",
    [
        ((201, b"private-malformed-json"), "invalid_session_response", True),
        ((201, []), "invalid_session_response", True),
        (
            (201, session_resource(agent_session_id="another-session")),
            "invalid_session_response",
            True,
        ),
        (
            (
                201,
                session_resource(version_indicator={"type": "version_ref", "agent_version": "2"}),
            ),
            "session_version_mismatch",
            True,
        ),
        ((201, session_resource(version_indicator=None)), "session_version_mismatch", True),
        ((201, session_resource(status="failed")), "session_not_ready", True),
        ((201, session_resource(status="idle")), "session_not_ready", True),
        ((201, session_resource(status="private-status")), "session_not_ready", True),
        ((201, b"x" * 65537), "response_too_large", True),
        ((200, session_resource()), "unexpected_http_status", True),
        ((202, {}), "unexpected_http_status", True),
        ((409, {"error": {"code": "private-code"}}), "unexpected_http_status", False),
        (TimeoutError("private-timeout"), "timeout", True),
    ],
)
def test_session_failure_never_invokes(modules, monkeypatch, response, reason, cleanup):
    payload, wrapper = modules
    invocation_parser(payload, wrapper, monkeypatch)
    opener = SequenceOpener(response)
    result = payload.probe(
        ENDPOINT, PRINCIPAL, Credential().factory, opener, ("1", "a" * 40, SESSION)
    )
    assert len(opener.requests) == 1 and opener.requests[0].method == "POST"
    assert result["sessionCreateAttempted"] and result["invocationsAttempted"] == 0
    assert result["sessionCleanupRequired"] is cleanup and not result["sessionReady"]
    assert result["reason"] == reason and result["phase"] == "session_create"
    assert wrapper.extract_result(payload.MARKER + json.dumps(result)) == result
    assert "private-" not in json.dumps(result)


@pytest.mark.parametrize("phase", ["session_create", "session_ready", "invoke"])
@pytest.mark.parametrize("code", ["session_not_accessible", "unknown", "private-token-message"])
def test_http_diagnostics_allow_only_literal_service_code(modules, monkeypatch, phase, code):
    payload, wrapper = modules
    invocation_parser(payload, wrapper, monkeypatch)
    monkeypatch.setattr(payload.time, "sleep", lambda _: None)
    error = urllib.error.HTTPError(
        "https://private-url",
        403,
        "private-error-message",
        {"X-Private": "private-header"},
        io.BytesIO(
            json.dumps({"error": {"code": code, "message": "private-token-canary"}}).encode()
        ),
    )
    responses = []
    if phase == "invoke":
        responses.append((201, session_resource()))
    elif phase == "session_ready":
        responses.append((201, session_resource(status="creating")))
    opener = SequenceOpener(*responses, error)
    result = payload.probe(
        ENDPOINT, PRINCIPAL, Credential().factory, opener, ("1", "a" * 40, SESSION)
    )
    assert result["phase"] == phase and result["httpStatus"] == 403
    assert result["serviceCode"] == (
        "session_not_accessible" if code == "session_not_accessible" else "unknown"
    )
    assert result["invocationsAttempted"] == (1 if phase == "invoke" else 0)
    assert result["sessionCleanupRequired"] and error.closed
    assert wrapper.extract_result(payload.MARKER + json.dumps(result)) == result
    assert "private-" not in json.dumps(result)


def test_collision_is_not_cleanup_permission(modules, monkeypatch):
    payload, wrapper = modules
    invocation_parser(payload, wrapper, monkeypatch)
    error = urllib.error.HTTPError(ENDPOINT, 409, "private", {}, io.BytesIO(b"{}"))
    opener = SequenceOpener(error)
    result = payload.probe(
        ENDPOINT, PRINCIPAL, Credential().factory, opener, ("1", "a" * 40, SESSION)
    )
    assert result["sessionCreateAttempted"] and not result["sessionCleanupRequired"]
    assert not result["sessionCreated"] and result["invocationsAttempted"] == 0
    assert len(opener.requests) == 1


@pytest.mark.parametrize("states", [("creating", "active"), ("updating", "creating", "active")])
def test_readiness_polls_same_session_with_bounded_gets(modules, monkeypatch, states):
    payload, wrapper = modules
    invocation_parser(payload, wrapper, monkeypatch)
    sleeps = []
    monkeypatch.setattr(payload.time, "sleep", sleeps.append)
    opener = SequenceOpener(
        *[
            (201 if i == 0 else 200, session_resource(status=state))
            for i, state in enumerate(states)
        ],
        (200, {"output": []}),
    )
    result = payload.probe(
        ENDPOINT, PRINCIPAL, Credential().factory, opener, ("1", "a" * 40, SESSION)
    )
    assert [r.method for r in opener.requests] == ["POST"] + ["GET"] * (len(states) - 1) + ["POST"]
    for request in opener.requests[1:-1]:
        assert (
            request.full_url
            == ENDPOINT
            + "/agents/card-orchestrator/endpoint/sessions/"
            + SESSION
            + "?api-version=v1"
        )
        assert request.data is None and request.headers == opener.requests[0].headers
    assert all(0 < timeout <= 10 for timeout in opener.timeouts[:-1])
    assert opener.timeouts[-1] == 65 and sleeps == [2] * (len(states) - 1)
    assert result["sessionReady"] and result["invocationsAttempted"] == 1


@pytest.mark.parametrize("advance_clock", [True, False])
def test_readiness_deadline_and_poll_cap_prevent_inference(modules, monkeypatch, advance_clock):
    payload, wrapper = modules
    invocation_parser(payload, wrapper, monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(payload.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        payload.time,
        "sleep",
        lambda delay: clock.__setitem__(0, clock[0] + delay if advance_clock else 0),
    )
    opener = SequenceOpener(
        (201, session_resource(status="creating")),
        *[(200, session_resource(status="creating")) for _ in range(16)],
    )
    result = payload.probe(
        ENDPOINT, PRINCIPAL, Credential().factory, opener, ("1", "a" * 40, SESSION)
    )
    assert result["reason"] in ("timeout", "session_readiness_timeout")
    assert result["phase"] == "session_ready" and result["invocationsAttempted"] == 0
    assert result["sessionCleanupRequired"] and not result["sessionReady"]
    assert len(opener.requests) <= 16 and clock[0] <= 30
    assert sum(r.method == "POST" for r in opener.requests) == 1
    assert wrapper.extract_result(payload.MARKER + json.dumps(result)) == result


@pytest.mark.parametrize(
    "session", ["session-1", "smoke-109-short", "smoke-109-" + "a" * 31, "old-existing-session"]
)
def test_session_id_requires_prerecorded_high_entropy_format(modules, session):
    payload, wrapper = modules
    opener, credential = SequenceOpener(), Credential()
    assert (
        payload.probe(ENDPOINT, PRINCIPAL, credential.factory, opener, ("1", "a" * 40, session))[
            "reason"
        ]
        == "invalid_configuration"
    )
    assert opener.requests == [] and credential.options is None
    with pytest.raises(ValueError):
        wrapper.remote_command(ENDPOINT, PRINCIPAL, ("1", "a" * 40, session))


@pytest.mark.parametrize(
    "changes",
    [
        {"serviceCode": "private-code"},
        {"phase": "private-phase"},
        {"sessionCreateAttempted": "true"},
        {"sessionReady": True},
        {"sessionCreated": True, "sessionCreateAttempted": False},
        {"sessionCleanupRequired": True, "sessionCreateAttempted": False},
        {"invocationsAttempted": 1},
    ],
)
def test_forged_session_marker_is_discarded(modules, changes):
    payload, wrapper = modules
    result = payload.initial_result(invocation=True)
    result.update(changes)
    assert wrapper.extract_result(payload.MARKER + json.dumps(result)) is None


@pytest.mark.parametrize("status", ["completed", "refused", "held", "routing_defer"])
def test_single_invocation_real_parser_and_versions(modules, monkeypatch, status):
    payload, wrapper = modules
    invocation_parser(payload, wrapper, monkeypatch)
    build, version, session = "a" * 40, "1", SESSION
    card = {
        "schemaVersion": 1,
        "name": "Lantern Guardian",
        "cardType": "creature",
        "rarity": "common",
        "manaCost": 2,
        "attack": 1,
        "health": 3,
        "rulesText": "Protect one friendly creature.",
        "flavorText": "",
        "artBrief": "An original guardian with a lantern in a peaceful forest.",
    }
    domain = {
        "schemaVersion": 1,
        "status": status,
        "card": card if status == "completed" else None,
        "artPrompt": "Original woodland guardian." if status == "completed" else None,
        "metadata": {"agentVersion": build, "hostedVersion": version},
    }
    body = json.dumps(
        {
            "id": "resp-test",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(domain)}],
                }
            ],
        }
    ).encode()

    class SingleOpener(Opener):
        calls = 0

        def open(self, request, timeout):
            self.calls += 1
            if self.calls == 1:
                assert request.method == "POST" and 0 < timeout <= 10
                return Opener(body=json.dumps(session_resource()).encode(), status=201)
            assert timeout == 65
            assert request.method == "POST"
            wire = json.loads(request.data)
            assert wire["agent_session_id"] == session
            assert "session_id" not in wire
            assert wire["store"] is False and wire["stream"] is False
            assert len(wire["input"]) == 1
            assert json.loads(wire["input"][0]["content"][0]["text"])["schemaVersion"] == 1
            self.request = request
            return self

    opener = SingleOpener(body=body)
    result = payload.probe(
        ENDPOINT, PRINCIPAL, Credential().factory, opener, (version, build, session)
    )
    assert opener.calls == 2
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
            if self.calls == 1:
                return Opener(body=json.dumps(session_resource()).encode(), status=201)
            raise TimeoutError()

    opener = TimeoutOpener()
    result = payload.probe(
        ENDPOINT, PRINCIPAL, Credential().factory, opener, ("1", "a" * 40, SESSION)
    )
    assert result["reason"] == "timeout"
    assert result["invocationsAttempted"] == 1 and opener.calls == 2
    assert not result["invocationVerified"]


def test_http_200_without_card_is_not_success(modules, monkeypatch):
    payload, wrapper = modules
    invocation_parser(payload, wrapper, monkeypatch)
    result = payload._parse_success_envelope(
        {"status": "completed", "output": []}, request_id=None, expected_version="a" * 40
    )
    assert not result.success and not result.schema_valid
    with pytest.raises(ValueError):
        wrapper.remote_command(ENDPOINT, PRINCIPAL, ("1", "not-a-sha", SESSION))


def test_access_only_real_payload_survives_canonical_terminal(modules, monkeypatch):
    payload, wrapper = modules
    monkeypatch.delenv("IDENTITY_ENDPOINT")
    monkeypatch.delenv("IDENTITY_HEADER")
    command = wrapper.remote_command(ENDPOINT, PRINCIPAL)
    assert len(command.split(" ", 2)[2].encode()) > 4096
    startup, input_line = wrapper.stdin_payload(command)
    program = (
        "import sys,termios\n"
        "assert termios.tcgetattr(0)[3] & termios.ICANON\n"
        "def forbid_network(event,args):\n"
        " if event.startswith('socket.'): raise AssertionError('network forbidden')\n"
        "sys.addaudithook(forbid_network)\n"
        "print('INFO: Successfully connected to container:',flush=True)\n"
        + startup.split(" ", 2)[2]
    )
    result = wrapper.execute([sys.executable, "-c", program], input_line=input_line)
    assert result == {**payload.initial_result(), "reason": "aca_identity_unavailable"}
    assert max(map(len, input_line.splitlines())) <= 1024


@pytest.mark.parametrize(
    "mode", [{}, {"invocation": True}, {"prepare": True}], ids=["access", "invocation", "prepare"]
)
def test_large_payload_survives_canonical_terminal(modules, mode):
    payload, wrapper = modules
    result = payload.probe(ENDPOINT, PRINCIPAL, Credential().factory, Opener())
    expression = (
        "padding="
        + repr("x" * 6000)
        + ";"
        + (f"print({payload.MARKER!r}+{json.dumps(result)!r},flush=True)")
    )
    startup, input_line = wrapper.stdin_payload("/app/.venv/bin/python -c " + expression, **mode)
    assert max(map(len, input_line.splitlines())) <= 1024
    program = (
        "print('INFO: Successfully connected to container:',flush=True);" + startup.split(" ", 2)[2]
    )
    # Deliberately keep canonical mode (unlike ACA's local CLI PTY test above).
    assert wrapper.execute([sys.executable, "-c", program], input_line=input_line) == result


@pytest.mark.parametrize(
    "failure,returncode",
    [
        ("signal.raise_signal(signal.SIGALRM)", -signal.SIGALRM),
        ("raise ValueError('synthetic')", 1),
    ],
)
def test_access_only_bootstrap_preserves_alarm_and_uncaught_errors(modules, failure, returncode):
    _, wrapper = modules
    expression = (
        "import signal;"
        "assert 0 < signal.getitimer(signal.ITIMER_REAL)[0] <= 60;"
        "assert signal.getsignal(signal.SIGALRM) == signal.SIG_DFL;" + failure
    )
    startup, input_line = wrapper.stdin_payload("/app/.venv/bin/python -c " + expression)
    result = subprocess.run(
        [sys.executable, "-c", startup.split(" ", 2)[2]],
        input=input_line + "\n",
        text=True,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == returncode
    assert result.stdout == ""


def test_preparation_checks_real_schema_and_local_fixture_but_only_gets(modules, monkeypatch):
    payload, wrapper = modules
    invocation_parser(payload, wrapper, monkeypatch)
    credential, opener = Credential(), Opener()
    result = payload.probe(ENDPOINT, PRINCIPAL, credential.factory, opener, prepare=True)
    assert result == {
        **payload.initial_result(True),
        "status": "invocation_prepared",
        "parserImportReady": True,
        "requestSchemaReady": True,
        "localFixtureParseReady": True,
        "tokenAcquired": True,
        "principalMatched": True,
        "accessVerified": True,
        "httpStatus": 200,
        "agentCountOnPage": 0,
    }
    assert opener.request.method == "GET" and opener.request.data is None
    assert result["schemaValid"] is False and result["invocationVerified"] is False
    assert wrapper.extract_result(payload.MARKER + json.dumps(result)) == result


@pytest.mark.parametrize("invocation", [("1", "a" * 40, "session"), (), ("bad",), "bad"])
def test_preparation_rejects_even_malformed_invocation_without_network(modules, invocation):
    payload, wrapper = modules
    credential, opener = Credential(), Opener()
    result = payload.probe(
        ENDPOINT, PRINCIPAL, credential.factory, opener, invocation, prepare=True
    )
    assert result["reason"] == "invalid_configuration"
    assert result["invocationsAttempted"] == 0
    assert credential.options is None and opener.request is None
    with pytest.raises(ValueError):
        wrapper.remote_command(ENDPOINT, PRINCIPAL, invocation, prepare=True)


@pytest.mark.parametrize(
    "change",
    [
        {"invocationVerified": True},
        {"invocationsAttempted": 1},
        {"schemaValid": True},
        {"localFixtureParseReady": False},
        {"requestSchemaReady": "true"},
        {"parserImportReady": 1},
        {"status": "invocation_verified"},
        {"responseId": "fixture"},
        {"outcome": "completed"},
        {"card": "private"},
    ],
)
def test_preparation_marker_cannot_be_invocation_evidence(modules, monkeypatch, change):
    payload, wrapper = modules
    invocation_parser(payload, wrapper, monkeypatch)
    result = payload.probe(ENDPOINT, PRINCIPAL, Credential().factory, Opener(), prepare=True)
    result.update(change)
    assert wrapper.extract_result(payload.MARKER + json.dumps(result)) is None


def preparation_args():
    args = [
        "--environment",
        "dev",
        "--project-endpoint",
        ENDPOINT,
        "--expected-principal",
        PRINCIPAL,
        "--prepare-invocation",
        "--execute",
    ]
    for name in ("subscription", "resource-group", "app", "revision", "replica", "container"):
        args.extend(["--" + name, "synthetic"])
    return args


@pytest.mark.parametrize(
    "extra",
    [
        ["--invoke-once"],
        ["--hosted-version", "1"],
        ["--expected-version", "bad"],
        ["--session-id", "session"],
        ["--invoke-once", "--session-id", "bad"],
    ],
)
def test_preparation_cli_malformed_args_never_launch_exec(modules, monkeypatch, extra):
    _, wrapper = modules
    monkeypatch.setattr(wrapper, "execute", lambda *a, **kw: pytest.fail("exec forbidden"))
    with pytest.raises(SystemExit):
        wrapper.main(preparation_args() + extra)


def test_preparation_cli_uses_invocation_transport_without_allowance(modules, monkeypatch, capsys):
    _, wrapper = modules
    calls = []

    def execute(command, **kwargs):
        calls.append(kwargs)
        assert len(command[command.index("--command") + 1]) < 2000
        assert max(map(len, kwargs["input_line"].splitlines())) <= 1024
        return {"status": "failed", "reason": "exec_no_evidence"}

    monkeypatch.setattr(wrapper, "execute", execute)
    assert wrapper.main(preparation_args()) == 1
    assert len(calls) == 1
    assert {key: calls[0][key] for key in ("timeout", "setup_timeout", "total_timeout")} == {
        "timeout": 80,
        "setup_timeout": 30,
        "total_timeout": 110,
    }
    assert "invocationAllowanceConsumed" not in json.loads(capsys.readouterr().out)


@pytest.mark.parametrize(
    "bundle,reason",
    [
        ("raise ImportError('private-import-details')", "parser_setup_failed"),
        ("raise TimeoutError('private-timeout-details')", "parser_setup_timeout"),
    ],
)
def test_parser_setup_failure_always_emits_sanitized_marker(
    modules, monkeypatch, capsys, bundle, reason
):
    payload, wrapper = modules
    monkeypatch.setattr(payload, "probe", lambda *a, **kw: pytest.fail("network forbidden"))
    payload.emit(ENDPOINT, PRINCIPAL, prepare=True, parser_bundle=bundle)
    result = wrapper.extract_result(capsys.readouterr().out)
    assert result == {**payload.initial_result(True), "reason": reason}


def test_parser_setup_preserves_remote_remaining_budget(modules, monkeypatch, capsys):
    payload, wrapper = modules
    budgets = []
    monkeypatch.setattr(payload.signal, "getitimer", lambda timer: (12.5, 0))
    monkeypatch.setattr(payload.signal, "setitimer", lambda timer, value: budgets.append(value))
    payload.emit(ENDPOINT, PRINCIPAL, prepare=True, parser_bundle="raise TimeoutError('private')")
    assert budgets == [12.5]
    assert wrapper.extract_result(capsys.readouterr().out)["reason"] == "parser_setup_timeout"


def test_invocation_import_reserves_separate_model_budget(modules, monkeypatch, capsys):
    payload, wrapper = modules
    budgets = []
    monkeypatch.setattr(payload.signal, "getitimer", lambda timer: (90, 0))
    monkeypatch.setattr(payload.signal, "setitimer", lambda timer, value: budgets.append(value))
    payload.emit(
        ENDPOINT,
        PRINCIPAL,
        ("1", "a" * 40, SESSION),
        parser_bundle="raise TimeoutError('private-import')",
    )
    assert budgets == [25]
    result = wrapper.extract_result(capsys.readouterr().out)
    assert result["reason"] == "parser_setup_timeout"
    assert not result["sessionCreateAttempted"] and result["invocationsAttempted"] == 0


def test_emit_installs_separate_setup_and_invoke_guards(modules, monkeypatch, capsys):
    import azure.identity

    payload, wrapper = modules
    budgets = []
    monkeypatch.setattr(payload.signal, "getitimer", lambda timer: (95, 0))
    monkeypatch.setattr(payload.signal, "setitimer", lambda timer, value: budgets.append(value))
    monkeypatch.setattr(azure.identity, "ManagedIdentityCredential", Credential().factory)
    opener = SequenceOpener((201, session_resource()), (200, {"output": []}))
    monkeypatch.setattr(payload.urllib.request, "build_opener", lambda *args: opener)
    payload.emit(
        ENDPOINT, PRINCIPAL, ("1", "a" * 40, SESSION), parser_bundle=wrapper.parser_source()
    )
    assert budgets == [30, 65]
    result = wrapper.extract_result(capsys.readouterr().out)
    assert result["invocationsAttempted"] == 1 and result["sessionReady"]
    assert not result["invocationVerified"]


def test_session_setup_exhaustion_prevents_creation(modules, monkeypatch):
    payload, wrapper = modules
    invocation_parser(payload, wrapper, monkeypatch)
    opener = SequenceOpener()
    result = payload.probe(
        ENDPOINT,
        PRINCIPAL,
        Credential().factory,
        opener,
        ("1", "a" * 40, SESSION),
        setup_deadline=0,
    )
    assert result["reason"] == "timeout" and opener.requests == []
    assert result["invocationsAttempted"] == 0 and not result["sessionCreateAttempted"]


@pytest.mark.parametrize(
    "body", [b"private-non-json", b"x" * 65537, b'{"error":{"code":["private"]}}']
)
def test_error_body_size_and_shape_never_leak(modules, monkeypatch, body):
    payload, wrapper = modules
    invocation_parser(payload, wrapper, monkeypatch)
    error = urllib.error.HTTPError(ENDPOINT, 403, "private", {}, io.BytesIO(body))
    opener = SequenceOpener(error)
    result = payload.probe(
        ENDPOINT, PRINCIPAL, Credential().factory, opener, ("1", "a" * 40, SESSION)
    )
    assert result["serviceCode"] == "unknown" and result["httpStatus"] == 403
    assert result["invocationsAttempted"] == 0 and "private" not in json.dumps(result)
    assert wrapper.extract_result(payload.MARKER + json.dumps(result)) == result


@pytest.mark.parametrize(
    "document,reason",
    [
        (session_resource(agent_session_id="another"), "invalid_session_response"),
        (
            session_resource(version_indicator={"type": "version_ref", "agent_version": "2"}),
            "session_version_mismatch",
        ),
    ],
)
def test_readiness_revalidates_actual_session_and_version(modules, monkeypatch, document, reason):
    payload, wrapper = modules
    invocation_parser(payload, wrapper, monkeypatch)
    monkeypatch.setattr(payload.time, "sleep", lambda _: None)
    opener = SequenceOpener((201, session_resource(status="creating")), (200, document))
    result = payload.probe(
        ENDPOINT, PRINCIPAL, Credential().factory, opener, ("1", "a" * 40, SESSION)
    )
    assert result["reason"] == reason and result["phase"] == "session_ready"
    assert result["invocationsAttempted"] == 0 and not result["sessionReady"]
    assert [r.method for r in opener.requests] == ["POST", "GET"]


def test_invocation_plan_never_executes_or_creates_session(modules, monkeypatch, capsys):
    _, wrapper = modules
    args = [arg for arg in preparation_args() if arg not in ("--prepare-invocation", "--execute")]
    args.extend(
        [
            "--invoke-once",
            "--hosted-version",
            "1",
            "--expected-version",
            "a" * 40,
            "--session-id",
            SESSION,
        ]
    )
    monkeypatch.setattr(wrapper, "execute", lambda *a, **kw: pytest.fail("exec forbidden"))
    assert wrapper.main(args) == 0
    assert "PLAN ONLY" in capsys.readouterr().out


def test_invocation_real_bundle_full_canonical_transport(modules):
    _, wrapper = modules
    startup, input_line = wrapper.stdin_payload(
        wrapper.remote_command(ENDPOINT, PRINCIPAL, ("1", "a" * 40, SESSION)), invocation=True
    )
    domain = {
        "schemaVersion": 1,
        "status": "refused",
        "card": None,
        "artPrompt": None,
        "metadata": {"agentVersion": "a" * 40, "hostedVersion": "1"},
    }
    envelope = {
        "status": "completed",
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(domain)}]}
        ],
    }
    program = (
        "import sys,termios,azure.identity,urllib.request\n"
        "from tests.test_aca_identity_probe import Credential,SequenceOpener,session_resource\n"
        "assert termios.tcgetattr(0)[3] & termios.ICANON\n"
        "def forbid_network(event,args):\n"
        " if event.startswith('socket.'): raise AssertionError('network forbidden')\n"
        "sys.addaudithook(forbid_network)\n"
        "azure.identity.ManagedIdentityCredential=Credential().factory\n"
        f"opener=SequenceOpener((201,session_resource()),(200,{envelope!r}))\n"
        "urllib.request.build_opener=lambda *a:opener\n"
        "print('INFO: Successfully connected to container:',flush=True)\n"
        + startup.split(" ", 2)[2]
    )
    result = wrapper.execute(
        [sys.executable, "-c", program],
        input_line=input_line,
        timeout=100,
        setup_timeout=10,
        total_timeout=110,
    )
    assert result["status"] == "invocation_verified" and result["outcome"] == "refused"
    assert result["sessionCreateAttempted"] and result["sessionReady"]
    assert result["invocationsAttempted"] == 1 and result["sessionCleanupRequired"]
    assert len(startup) < 2000 and max(map(len, input_line.splitlines())) <= 1024


def test_preparation_real_bundle_full_canonical_transport(modules):
    payload, wrapper = modules
    startup, input_line = wrapper.stdin_payload(
        wrapper.remote_command(ENDPOINT, PRINCIPAL, prepare=True), prepare=True
    )
    # Only the identity and HTTP boundary are mocked; parser/import/request/PTY are real.
    program = (
        "import azure.identity,urllib.request;"
        "from tests.test_aca_identity_probe import Credential,Opener;"
        "azure.identity.ManagedIdentityCredential=Credential().factory;"
        "urllib.request.build_opener=lambda *a:Opener();"
        "print('INFO: Successfully connected to container:',flush=True);" + startup.split(" ", 2)[2]
    )
    result = wrapper.execute(
        [sys.executable, "-c", program],
        input_line=input_line,
        timeout=80,
        setup_timeout=30,
        total_timeout=110,
    )
    assert result["status"] == "invocation_prepared"
    assert result["localFixtureParseReady"] is True
    assert result["invocationsAttempted"] == 0 and result["invocationVerified"] is False


def test_cold_import_exceeding_old_five_second_alarm_now_completes(modules):
    _, wrapper = modules
    # Reproduce the latent default-SIGALRM failure locally, not its historical occurrence.
    old = subprocess.run(
        [sys.executable, "-c", "import signal,time;signal.alarm(5);time.sleep(5.2)"],
        capture_output=True,
        timeout=8,
    )
    assert old.returncode == -signal.SIGALRM and old.stdout == b""
    source = (PROJECT / "aca_identity_payload.py").read_text()
    source += (
        f"\nemit({ENDPOINT!r},{PRINCIPAL!r},prepare=True,"
        "parser_bundle='import time;time.sleep(5.2);raise ImportError()')"
    )
    # Encode multiline source exactly as the production remote command does.
    encoded = base64.b64encode(zlib.compress(source.encode())).decode()
    startup, input_line = wrapper.stdin_payload(
        "/app/.venv/bin/python -c "
        + f"exec(__import__('zlib').decompress(__import__('base64').b64decode('{encoded}')))",
        prepare=True,
    )
    program = (
        "print('INFO: Successfully connected to container:',flush=True);" + startup.split(" ", 2)[2]
    )
    result = wrapper.execute([sys.executable, "-c", program], input_line=input_line, timeout=12)
    assert result["reason"] == "parser_setup_failed"
    assert result["invocationsAttempted"] == 0
