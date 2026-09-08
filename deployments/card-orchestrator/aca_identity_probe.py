"""Dev-only ACA probe; optional single invocation, never modifies the serving app."""

import argparse
import ast
import base64
import json
import os
import pty
import re
import selectors
import subprocess
import time
import zlib
from pathlib import Path

from aca_identity_payload import MARKER, validate_inputs, validate_invocation


def parser_source():
    """Bundle the owned parser/model definitions, not a substitute client or response."""
    root = Path(__file__).resolve().parents[2]
    names = {
        "GenerateCardAgentResponse", "FoundryAgentInvocationResult",
        "_parse_success_envelope", "_contains_refusal", "_extract_output_text",
        "_metadata_version", "_incomplete_reason", "_string_or_none",
        "_safe_identifier_or_none",
    }
    source = (root / "app/foundry_agent_client.py").read_text()
    nodes = [node for node in ast.parse(source).body if getattr(node, "name", None) in names]
    assert {node.name for node in nodes} == names
    imports = (
        "from __future__ import annotations\n"
        "import json\nimport re\n"
        "from collections.abc import Mapping\nfrom dataclasses import dataclass\n"
        "from typing import Any, Literal\n"
        "from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator\n"
        "from app.generation import GeneratedCardModel\n"
    )
    lines = source.splitlines()
    return imports + "\n\n".join(
        "\n".join(lines[min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1:node.end_lineno])
        for node in nodes
    ) + "\nGenerateCardAgentResponse.model_rebuild(_types_namespace=globals())\n"


def remote_command(endpoint, principal, invocation=None):
    validate_inputs(endpoint, principal)
    source = Path(__file__).with_name("aca_identity_payload.py").read_text()
    if invocation is None:
        source += f"\nemit({endpoint!r}, {principal!r})\n"
    else:
        validate_invocation(*invocation)
        source = parser_source() + source + f"\nemit({endpoint!r}, {principal!r}, {invocation!r})\n"
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
            "invocationsAttempted", "schemaValid", "outcome", "hostedVersion",
            "applicationVersion", "responseId", "requestId",
            "hostedVersionMatched", "applicationVersionMatched",
        }
        if not isinstance(result, dict) or set(result) - allowed:
            continue
        if result.get("status") not in ("failed", "access_verified", "invocation_verified"):
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
            "invalid_response",
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
        if result["endpointPersisted"]:
            continue
        if "invocationsAttempted" in result:
            if type(result["invocationsAttempted"]) is not int or result["invocationsAttempted"] not in (0, 1):
                continue
            if type(result.get("schemaValid")) is not bool:
                continue
            if any(key in result and type(result[key]) is not bool for key in (
                "hostedVersionMatched", "applicationVersionMatched"
            )):
                continue
            if result.get("outcome") not in (
                None, "completed", "refused", "held", "routing_defer", "policy_refusal",
                "incomplete", "failed", "invalid_response", "version_mismatch",
            ):
                continue
            if any(
                key in result and (
                    not isinstance(result[key], str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}", result[key])
                )
                for key in ("hostedVersion", "applicationVersion", "responseId", "requestId")
            ):
                continue
        elif result["invocationVerified"]:
            continue
        if result["status"] == "invocation_verified" and not (
            result["invocationVerified"] and result.get("schemaValid")
            and result.get("hostedVersionMatched") and result.get("applicationVersionMatched")
            and result["principalMatched"] and result["tokenAcquired"] and result["accessVerified"]
            and result.get("httpStatus") == 200 and result.get("invocationsAttempted") == 1
        ):
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
    parser.add_argument("--invoke-once", action="store_true")
    parser.add_argument("--hosted-version")
    parser.add_argument("--expected-version")
    parser.add_argument("--session-id")
    args = parser.parse_args(argv)
    invocation = (
        (args.hosted_version, args.expected_version, args.session_id) if args.invoke_once else None
    )
    try:
        payload = remote_command(args.project_endpoint, args.expected_principal, invocation)
    except (ValueError, TypeError):
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
        "/app/.venv/bin/python -c __import__('signal').alarm("
        + ("75" if invocation else "60") + ");exec(input())",
        "--only-show-errors",
    ]
    if not args.execute:
        print("PLAN ONLY: pinned ACA exec; explicit system MI; "
              + ("ONE Responses request." if invocation else "one GET agents; no model call."))
        return 0
    # Long startup commands receive HTTP 404 from the exec WebSocket gateway.
    # Send only reviewed source (never credentials) over stdin to the bounded process.
    result = execute(command, input_line=payload.split(" ", 2)[2])
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] in ("access_verified", "invocation_verified") else 1


if __name__ == "__main__":
    raise SystemExit(main())
