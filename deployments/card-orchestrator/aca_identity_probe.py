"""Dev-only, read-only probe in a pinned existing ACA revision and replica."""

import argparse
import base64
import json
import os
import pty
import selectors
import subprocess
import time
import zlib
from pathlib import Path

from aca_identity_payload import MARKER, validate_inputs


def remote_command(endpoint, principal):
    validate_inputs(endpoint, principal)
    source = Path(__file__).with_name("aca_identity_payload.py").read_text()
    source += f"\nemit({endpoint!r}, {principal!r})\n"
    encoded = base64.b64encode(zlib.compress(source.encode())).decode("ascii")
    # The payload is the adjacent inspectable source, not a file written into the app.
    # ACA's exec command splitter retains shell quotes: use one whitespace-free
    # Python argument, rather than a shell-quoted program or an interactive shell.
    return "/app/.venv/bin/python -c " + (
        f"exec(__import__('zlib').decompress(__import__('base64').b64decode('{encoded}')))"
    )


def extract_result(output):
    for line in output.replace("\r", "").splitlines():
        if not line.startswith(MARKER):
            continue
        try:
            result = json.loads(line[len(MARKER) :])
        except ValueError:
            continue
        allowed = {
            "status",
            "tokenAcquired",
            "principalMatched",
            "accessVerified",
            "invocationVerified",
            "endpointPersisted",
            "reason",
            "httpStatus",
            "agentCountOnPage",
        }
        if not isinstance(result, dict) or set(result) - allowed:
            continue
        if result.get("status") not in ("failed", "access_verified"):
            continue
        reasons = {
            "invalid_configuration",
            "aca_identity_unavailable",
            "identity_claim_mismatch",
            "unexpected_http_status",
            "response_too_large",
            "invalid_list_response",
            "http_error",
            "identity_dependency_missing",
            "timeout",
            "probe_failed",
        }
        if "reason" in result and result["reason"] not in reasons:
            continue
        bools = (
            "tokenAcquired",
            "principalMatched",
            "accessVerified",
            "invocationVerified",
            "endpointPersisted",
        )
        if any(type(result.get(key)) is not bool for key in bools):
            continue
        if result["invocationVerified"] or result["endpointPersisted"]:
            continue
        if any(
            key in result and (type(result[key]) is not int or result[key] < 0)
            for key in ("httpStatus", "agentCountOnPage")
        ):
            continue
        if result["status"] == "access_verified" and not (
            result["tokenAcquired"]
            and result["principalMatched"]
            and result["accessVerified"]
            and result.get("httpStatus") == 200
            and "agentCountOnPage" in result
            and "reason" not in result
        ):
            continue
        return result
    return None


def execute(command, timeout=75, input_line=None):
    master, slave = pty.openpty()
    process = None
    output = bytearray()
    sent = False
    try:
        process = subprocess.Popen(
            command, stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        os.close(slave)
        slave = None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                for key, _ in selector.select(timeout=min(1, max(0, deadline - time.monotonic()))):
                    chunk = os.read(key.fd, 4096)
                    if not chunk:
                        return {"status": "failed", "reason": "exec_no_evidence"}
                    output.extend(chunk)
                    if len(output) > 131072:
                        return {"status": "failed", "reason": "exec_output_limit"}
                    if (
                        input_line is not None
                        and not sent
                        and b"Successfully connected to container:" in output
                    ):
                        # Wait until CLI sets up its terminal; early stdin can be flushed.
                        time.sleep(0.2)
                        pending = memoryview((input_line + "\n").encode())
                        while pending:
                            pending = pending[os.write(master, pending) :]
                        sent = True
                    result = extract_result(output.decode("utf-8", errors="replace"))
                    if result is not None:
                        return result
        return {"status": "failed", "reason": "exec_timeout"}
    except OSError:
        return {"status": "failed", "reason": "exec_unavailable"}
    finally:
        if slave is not None:
            os.close(slave)
        os.close(master)
        if process is not None:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=("dev",), required=True)
    for name in (
        "subscription",
        "resource-group",
        "app",
        "revision",
        "replica",
        "container",
        "project-endpoint",
        "expected-principal",
    ):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload = remote_command(args.project_endpoint, args.expected_principal)
    except ValueError:
        parser.error("Supply a canonical verified project endpoint and principal UUID")
    command = [
        "az",
        "containerapp",
        "exec",
        "--subscription",
        args.subscription,
        "--resource-group",
        args.resource_group,
        "--name",
        args.app,
        "--revision",
        args.revision,
        "--replica",
        args.replica,
        "--container",
        args.container,
        "--command",
        "/app/.venv/bin/python -c __import__('signal').alarm(60);exec(input())",
        "--only-show-errors",
    ]
    if not args.execute:
        print("PLAN ONLY: pinned ACA exec; explicit system MI; one GET agents; no model call.")
        return 0
    # Long startup commands receive HTTP 404 from the exec WebSocket gateway.
    # Send only reviewed source (never credentials) over stdin to the bounded process.
    result = execute(command, input_line=payload.split(" ", 2)[2])
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "access_verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
