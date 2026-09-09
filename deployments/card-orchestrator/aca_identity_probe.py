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

from aca_identity_payload import (
    MARKER,
    REMOTE_TIMEOUT,
    initial_result,
    validate_inputs,
    validate_invocation,
)

INVOCATION_SETUP_TIMEOUT = 30
INVOCATION_RESULT_TIMEOUT = 100
INVOCATION_TOTAL_TIMEOUT = INVOCATION_SETUP_TIMEOUT + INVOCATION_RESULT_TIMEOUT


def parser_source():
    """Bundle the owned parser/model definitions, not a substitute client or response."""
    root = Path(__file__).resolve().parents[2]
    names = {
        "GenerateCardAgentRequest",
        "GenerateCardAgentResponse",
        "FoundryAgentInvocationResult",
        "_parse_success_envelope",
        "_contains_refusal",
        "_extract_output_text",
        "_metadata_version",
        "_incomplete_reason",
        "_string_or_none",
        "_safe_identifier_or_none",
        "_parse_runtime_failure_code",
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
    return (
        imports
        + "\n\n".join(
            "\n".join(
                lines[
                    min([node.lineno] + [d.lineno for d in node.decorator_list])
                    - 1 : node.end_lineno
                ]
            )
            for node in nodes
        )
        + "\nGenerateCardAgentRequest.model_rebuild(_types_namespace=globals())\n"
        + "GenerateCardAgentResponse.model_rebuild(_types_namespace=globals())\n"
    )


def remote_command(
    endpoint, principal, invocation=None, *, prepare=False, require_persisted_endpoint=False
):
    validate_inputs(endpoint, principal)
    if type(require_persisted_endpoint) is not bool:
        raise ValueError("invalid_endpoint_requirement")
    if prepare and invocation is not None:
        raise ValueError("conflicting_modes")
    source = Path(__file__).with_name("aca_identity_payload.py").read_text()
    requirement = ", require_persisted_endpoint=True" if require_persisted_endpoint else ""
    if invocation is None and not prepare:
        source += f"\nemit({endpoint!r}, {principal!r}{requirement})\n"
    else:
        if invocation is not None:
            validate_invocation(*invocation)
        source += (
            f"\nemit({endpoint!r}, {principal!r}, {invocation!r}, "
            f"prepare={prepare!r}, parser_bundle={parser_source()!r}{requirement})\n"
        )
    encoded = base64.b64encode(zlib.compress(source.encode())).decode("ascii")
    # The payload is the adjacent inspectable source, not a file written into the app.
    # ACA's exec command splitter retains shell quotes: use one whitespace-free
    # Python argument, rather than a shell-quoted program or an interactive shell.
    return "/app/.venv/bin/python -c " + (
        "__import__('sys').dont_write_bytecode=True;"
        f"exec(__import__('zlib').decompress(__import__('base64').b64decode('{encoded}')))"
    )


def stdin_payload(payload, *, invocation=False, prepare=False):
    expression = payload.split(" ", 2)[2]
    # ACA's terminal can be canonical: never send a source line beyond PC_MAX_CANON.
    input_lines = (
        "\n".join(expression[offset : offset + 1024] for offset in range(0, len(expression), 1024))
        + "\nEND"
    )
    source_reader = "''.join(iter(input,'END'))"
    if not invocation and not prepare:
        return (
            "/app/.venv/bin/python -c __import__('signal').alarm(60);" + f"exec({source_reader})",
            input_lines,
        )
    # Install a diagnostic handler before input/decompression or any application import.
    failure = initial_result(prepare, invocation)
    bootstrap = (
        "import signal,json\n"
        "def deadline(signum,frame): raise TimeoutError()\n"
        "signal.signal(signal.SIGALRM,deadline)\n"
        "signal.alarm(30)\n"
        f"result={failure!r}\n"
        "try:\n"
        f" source={source_reader}\n"
        f" signal.alarm({REMOTE_TIMEOUT if invocation else 70})\n"
        " exec(source)\n"
        "except TimeoutError:\n"
        " result['reason']='bootstrap_timeout'\n"
        f" print({MARKER!r}+json.dumps(result),flush=True)\n"
        "except Exception:\n"
        " result['reason']='bootstrap_failed'\n"
        f" print({MARKER!r}+json.dumps(result),flush=True)\n"
        "finally: signal.alarm(0)\n"
    )
    encoded = base64.b64encode(zlib.compress(bootstrap.encode())).decode("ascii")
    command = "/app/.venv/bin/python -c " + (
        "__import__('sys').dont_write_bytecode=True;"
        f"exec(__import__('zlib').decompress(__import__('base64').b64decode('{encoded}')))"
    )
    return command, input_lines


def extract_result(output, *, require_persisted_endpoint=False):
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
            "endpointSource",
            "reason",
            "httpStatus",
            "agentCountOnPage",
            "invocationsAttempted",
            "schemaValid",
            "outcome",
            "hostedVersion",
            "applicationVersion",
            "responseId",
            "requestId",
            "hostedVersionMatched",
            "applicationVersionMatched",
            "preparationOnly",
            "parserImportReady",
            "requestSchemaReady",
            "localFixtureParseReady",
            "sessionCreateAttempted",
            "sessionCreated",
            "sessionReady",
            "sessionCleanupRequired",
            "phase",
            "serviceCode",
            "serviceReason",
            "serviceParam",
            "runtimeStage",
            "runtimeReason",
            "runtimeHttpType",
            "runtimeHttpStatus",
        }
        if not isinstance(result, dict) or set(result) - allowed:
            continue
        if result.get("status") not in (
            "failed",
            "access_verified",
            "invocation_verified",
            "invocation_prepared",
        ):
            continue
        reasons = {
            "invalid_configuration",
            "persisted_endpoint_invalid",
            "persisted_endpoint_mismatch",
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
            "local_fixture_failed",
            "parser_setup_timeout",
            "parser_setup_failed",
            "bootstrap_timeout",
            "bootstrap_failed",
            "invalid_session_response",
            "session_version_mismatch",
            "session_not_ready",
            "session_readiness_timeout",
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
        if result["endpointPersisted"] != (result.get("endpointSource") == "aca_environment"):
            continue
        if "endpointSource" in result and result["endpointSource"] != "aca_environment":
            continue
        if (
            require_persisted_endpoint
            and result["status"] != "failed"
            and not result["endpointPersisted"]
        ):
            continue
        if result["invocationVerified"] != (result["status"] == "invocation_verified"):
            continue
        if "phase" in result and result["phase"] not in (
            "session_create",
            "session_ready",
            "invoke",
        ):
            continue
        if "serviceCode" in result and result["serviceCode"] not in (
            "unknown",
            "session_not_accessible",
            "invalid_request",
            "card_boundary_invalid_request",
        ):
            continue
        if result.get("serviceCode") == "card_boundary_invalid_request":
            if result.get("serviceReason") not in (
                "invalid_json",
                "invalid_object",
                "unsupported_field",
                "not_false",
                "invalid_value",
                "not_single_item_list",
                "invalid_user_message",
                "invalid_input_text",
                "invalid_schema",
            ) or result.get("serviceParam") not in (
                "body",
                "top_level",
                "agent",
                "agent_reference",
                "background",
                "conversation",
                "instructions",
                "max_output_tokens",
                "metadata",
                "model",
                "previous_response_id",
                "prompt",
                "response_id",
                "store",
                "stream",
                "text",
                "tool_choice",
                "tools",
                "agent_session_id",
                "input",
                "content",
                "domain",
            ):
                continue
        elif "serviceReason" in result or "serviceParam" in result:
            continue
        runtime_keys = {
            "runtimeStage",
            "runtimeReason",
            "runtimeHttpType",
            "runtimeHttpStatus",
        }
        if runtime_keys & result.keys():
            if (
                not runtime_keys <= result.keys()
                or result.get("outcome") != "failed"
                or result.get("schemaValid") is not False
                or result.get("status") != "failed"
                or result.get("runtimeStage")
                not in (
                    "specialist_setup",
                    "concept",
                    "lore",
                    "art_direction",
                    "orchestration",
                )
                or result.get("runtimeReason")
                not in (
                    "timeout",
                    "authentication",
                    "authorization",
                    "resource_not_found",
                    "invalid_request",
                    "rate_limited",
                    "service_error",
                    "transport_error",
                    "invalid_response",
                    "dependency_error",
                )
                or result.get("runtimeHttpType")
                not in (
                    "none",
                    "bad_request",
                    "authentication",
                    "permission_denied",
                    "not_found",
                    "conflict",
                    "unprocessable",
                    "rate_limit",
                    "server",
                    "api_status",
                )
                or result.get("runtimeHttpStatus")
                not in (
                    "none",
                    "http_400",
                    "http_401",
                    "http_403",
                    "http_404",
                    "http_408",
                    "http_409",
                    "http_422",
                    "http_429",
                    "http_500",
                    "http_502",
                    "http_503",
                    "http_504",
                    "http_other",
                )
            ):
                continue
        session_keys = {
            "sessionCreateAttempted",
            "sessionCreated",
            "sessionReady",
            "sessionCleanupRequired",
        }
        if session_keys & result.keys():
            if (
                any(type(result.get(key)) is not bool for key in session_keys)
                or "preparationOnly" in result
                or result["status"] not in ("failed", "invocation_verified")
                or "invocationsAttempted" not in result
                or (result["sessionCreated"] and not result["sessionCreateAttempted"])
                or (result["sessionReady"] and not result["sessionCreated"])
                or (result["sessionCleanupRequired"] and not result["sessionCreateAttempted"])
                or (result["sessionCreated"] and not result["sessionCleanupRequired"])
                or (result.get("phase") is not None and not result["sessionCreateAttempted"])
                or (
                    result.get("invocationsAttempted") == 1
                    and (not result["sessionReady"] or result.get("phase") != "invoke")
                )
                or (result.get("phase") == "invoke" and result.get("invocationsAttempted") != 1)
            ):
                continue
        elif "phase" in result or result.get("invocationsAttempted") == 1:
            continue
        preparation_keys = {
            "preparationOnly",
            "parserImportReady",
            "requestSchemaReady",
            "localFixtureParseReady",
        }
        if preparation_keys & result.keys() or result["status"] == "invocation_prepared":
            if (
                result.get("preparationOnly") is not True
                or any(type(result.get(key)) is not bool for key in preparation_keys)
                or result["invocationVerified"]
                or result.get("invocationsAttempted") != 0
                or result.get("schemaValid") is not False
                or result["status"] not in ("failed", "invocation_prepared")
                or set(result)
                & {
                    "outcome",
                    "hostedVersion",
                    "applicationVersion",
                    "responseId",
                    "requestId",
                    "hostedVersionMatched",
                    "applicationVersionMatched",
                }
            ):
                continue
            if result["status"] == "invocation_prepared" and not all(
                result[key] for key in preparation_keys
            ):
                continue
        if "invocationsAttempted" in result:
            if type(result["invocationsAttempted"]) is not int or result[
                "invocationsAttempted"
            ] not in (0, 1):
                continue
            if type(result.get("schemaValid")) is not bool:
                continue
            if any(
                key in result and type(result[key]) is not bool
                for key in ("hostedVersionMatched", "applicationVersionMatched")
            ):
                continue
            if result.get("outcome") not in (
                None,
                "completed",
                "refused",
                "held",
                "routing_defer",
                "policy_refusal",
                "incomplete",
                "failed",
                "invalid_response",
                "version_mismatch",
            ):
                continue
            if any(
                key in result
                and (
                    not isinstance(result[key], str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}", result[key])
                )
                for key in ("hostedVersion", "applicationVersion", "responseId", "requestId")
            ):
                continue
        elif result["invocationVerified"]:
            continue
        if result["status"] == "invocation_verified" and not (
            result["invocationVerified"]
            and result.get("schemaValid")
            and result.get("hostedVersionMatched")
            and result.get("applicationVersionMatched")
            and result["principalMatched"]
            and result["tokenAcquired"]
            and result["accessVerified"]
            and result.get("httpStatus") == 200
            and result.get("invocationsAttempted") == 1
            and result.get("sessionReady") is True
        ):
            continue
        if any(
            key in result and (type(result[key]) is not int or result[key] < 0)
            for key in ("httpStatus", "agentCountOnPage")
        ):
            continue
        if result["status"] in ("access_verified", "invocation_prepared") and not (
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


def execute(
    command,
    timeout=75,
    input_line=None,
    *,
    setup_timeout=None,
    total_timeout=None,
    require_persisted_endpoint=False,
):
    started = time.monotonic()
    deadline = started + (setup_timeout if setup_timeout is not None else timeout)
    overall_deadline = started + (
        total_timeout if total_timeout is not None else (setup_timeout or 0) + timeout
    )
    master, slave = pty.openpty()
    process = None
    output = bytearray()
    sent = False
    pending = None
    ready_at = None
    writing = False
    try:
        os.set_blocking(master, False)
        process = subprocess.Popen(
            command, stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        os.close(slave)
        slave = None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                now = time.monotonic()
                if total_timeout is not None and now >= overall_deadline:
                    return {"status": "failed", "reason": "exec_total_timeout"}
                if now >= deadline:
                    reason = (
                        "exec_setup_timeout"
                        if setup_timeout is not None and not sent
                        else "exec_timeout"
                    )
                    return {"status": "failed", "reason": reason}
                wait = min(1, deadline - now, overall_deadline - now)
                if pending is not None and not writing:
                    if now >= ready_at:
                        selector.register(master, selectors.EVENT_WRITE)
                        writing = True
                    else:
                        wait = min(wait, ready_at - now)
                for key, _ in selector.select(timeout=wait):
                    if time.monotonic() >= min(deadline, overall_deadline):
                        break
                    if key.fd == master:
                        try:
                            written = os.write(master, pending)
                        except BlockingIOError:
                            continue
                        if written <= 0:
                            return {"status": "failed", "reason": "exec_unavailable"}
                        pending = pending[written:]
                        if not pending:
                            selector.unregister(master)
                            pending = None
                            sent = True
                            if setup_timeout is not None:
                                deadline = time.monotonic() + timeout
                        continue
                    chunk = os.read(key.fd, 4096)
                    if not chunk:
                        return {"status": "failed", "reason": "exec_no_evidence"}
                    output.extend(chunk)
                    if len(output) > 131072:
                        return {"status": "failed", "reason": "exec_output_limit"}
                    if (
                        input_line is not None
                        and not sent
                        and pending is None
                        and b"Successfully connected to container:" in output
                    ):
                        # Wait until CLI sets up its terminal; early stdin can be flushed.
                        ready_at = time.monotonic() + 0.2
                        pending = memoryview((input_line + "\n").encode())
                    result = extract_result(
                        output.decode("utf-8", errors="replace"),
                        require_persisted_endpoint=require_persisted_endpoint,
                    )
                    if result is not None:
                        return result
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
    parser.add_argument(
        "--require-persisted-endpoint",
        action="store_true",
        help="Require ACA's environment endpoint to exactly match the verified project endpoint",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--invoke-once", action="store_true")
    modes.add_argument("--prepare-invocation", action="store_true")
    parser.add_argument("--hosted-version")
    parser.add_argument("--expected-version")
    parser.add_argument("--session-id")
    args = parser.parse_args(argv)
    if not args.invoke_once and any(
        value is not None for value in (args.hosted_version, args.expected_version, args.session_id)
    ):
        parser.error("Version/session options require --invoke-once")
    invocation = (
        (args.hosted_version, args.expected_version, args.session_id) if args.invoke_once else None
    )
    try:
        payload = remote_command(
            args.project_endpoint,
            args.expected_principal,
            invocation,
            prepare=args.prepare_invocation,
            require_persisted_endpoint=args.require_persisted_endpoint,
        )
    except (ValueError, TypeError):
        parser.error("Supply a canonical verified project endpoint and principal UUID")
    startup, input_line = stdin_payload(
        payload, invocation=invocation is not None, prepare=args.prepare_invocation
    )
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
        startup,
        "--only-show-errors",
    ]
    if not args.execute:
        print(
            "PLAN ONLY: pinned ACA exec; explicit system MI; "
            + (
                "ONE owned version-pinned session create, bounded readiness GETs, "
                "then at most ONE Responses request; operator finally-cleanup required."
                if invocation
                else (
                    "parser/request/local fixture preparation; one GET agents; NO POST."
                    if args.prepare_invocation
                    else "one GET agents; no model call."
                )
            )
        )
        return 0
    # Long startup commands receive HTTP 404 from the exec WebSocket gateway.
    # Send only reviewed source (never credentials) over stdin to the bounded process.
    if invocation or args.prepare_invocation:
        result = execute(
            command,
            input_line=input_line,
            timeout=INVOCATION_RESULT_TIMEOUT if invocation else 80,
            setup_timeout=INVOCATION_SETUP_TIMEOUT if invocation else 30,
            total_timeout=INVOCATION_TOTAL_TIMEOUT if invocation else 110,
            require_persisted_endpoint=args.require_persisted_endpoint,
        )
    else:
        result = execute(
            command,
            input_line=input_line,
            require_persisted_endpoint=args.require_persisted_endpoint,
        )
    if invocation and "invocationsAttempted" not in result:
        result["invocationAllowanceConsumed"] = True
        result["sessionCreationUnknown"] = True
        result["sessionCleanupRequired"] = True
    print(json.dumps(result, sort_keys=True))
    return (
        0
        if result["status"] in ("access_verified", "invocation_verified", "invocation_prepared")
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
