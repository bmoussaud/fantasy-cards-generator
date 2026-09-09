"""In-memory ACA MI probe, with a separately opted-in single Responses request."""

import base64
import json
import logging
import os
import re
import signal
import time
import urllib.error
import urllib.request
import uuid

SCOPE = "https://ai.azure.com/.default"
API_VERSION = "2025-11-15-preview"
MARKER = "ACA_IDENTITY_PROBE="
MAX_BODY = 65536
SYNTHETIC_QUERY = "Create an original gentle woodland guardian with a lantern and protective magic."
SESSION_SETUP_TIMEOUT = 30
INVOCATION_TIMEOUT = 65
REMOTE_TIMEOUT = SESSION_SETUP_TIMEOUT + INVOCATION_TIMEOUT


def initial_result(prepare=False, invocation=False):
    result = {
        "status": "failed",
        "tokenAcquired": False,
        "principalMatched": False,
        "accessVerified": False,
        "invocationVerified": False,
        "endpointPersisted": False,
    }
    if prepare:
        result.update(
            preparationOnly=True,
            parserImportReady=False,
            requestSchemaReady=False,
            localFixtureParseReady=False,
            invocationsAttempted=0,
            schemaValid=False,
        )
    if invocation:
        result.update(
            invocationsAttempted=0,
            schemaValid=False,
            sessionCreateAttempted=False,
            sessionCreated=False,
            sessionReady=False,
            sessionCleanupRequired=False,
        )
    return result


def invocation_body(session):
    request = globals()["GenerateCardAgentRequest"](query=SYNTHETIC_QUERY)
    return {
        "store": False,
        "stream": False,
        "agent_session_id": session,
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": request.model_dump_json()}],
            }
        ],
    }


def check_local_fixture():
    """Offline parser exercise only: this document did not come from Foundry."""
    domain = {
        "schemaVersion": 1,
        "status": "completed",
        "card": {
            "schemaVersion": 1,
            "name": "Local fixture guardian",
            "cardType": "creature",
            "rarity": "common",
            "manaCost": 2,
            "attack": 1,
            "health": 3,
            "rulesText": "Protect one friendly creature.",
            "flavorText": "",
            "artBrief": "A local fixture guardian with a lantern.",
        },
        "artPrompt": "Local fixture, not generated service content.",
        "metadata": {"agentVersion": "local-fixture"},
    }
    document = {
        "status": "completed",
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(domain)}]}
        ],
    }
    parsed = globals()["_parse_success_envelope"](
        document, request_id=None, expected_version="local-fixture"
    )
    invalid = globals()["_parse_success_envelope"](
        {"status": "completed", "output": []},
        request_id=None,
        expected_version="local-fixture",
    )
    return parsed.success and parsed.schema_valid and not invalid.schema_valid


def validate_invocation(version, build, session):
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", version):
        raise ValueError("invalid_version")
    if not re.fullmatch(r"[a-f0-9]{40}", build):
        raise ValueError("invalid_build")
    if not re.fullmatch(r"smoke-109-[a-f0-9]{32}", session):
        raise ValueError("invalid_session")


def validate_inputs(endpoint, principal):
    if not re.fullmatch(
        r"https://[a-z0-9][a-z0-9-]*\.services\.ai\.azure\.com/api/projects/"
        r"[A-Za-z0-9][A-Za-z0-9_-]*",
        endpoint,
    ):
        raise ValueError("invalid_project_endpoint")
    if str(uuid.UUID(principal)) != principal:
        raise ValueError("invalid_principal")


def matches_identity(token, expected):
    # Claims are compared in memory, not exported or used instead of server validation.
    claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==="))
    return (
        claims.get("oid") == expected
        and claims.get("aud") in ("https://ai.azure.com", "https://ai.azure.com/")
        and isinstance(claims.get("exp"), (int, float))
        and claims["exp"] > time.time()
    )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ProbeFailure(Exception):
    pass


def service_code(body):
    """Only a literal, recognized service code may cross the diagnostic boundary."""
    try:
        document = json.loads(body)
        if document["error"]["code"] == "session_not_accessible":
            return "session_not_accessible"
    except (ValueError, TypeError, KeyError):
        pass
    return "unknown"


def session_document(opener, request, result, deadline, expected_status):
    result.pop("httpStatus", None)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError()
    with opener.open(request, timeout=min(10, remaining)) as response:
        result["httpStatus"] = response.status
        body = response.read(MAX_BODY + 1)
        if response.status != expected_status:
            result["serviceCode"] = service_code(body) if len(body) <= MAX_BODY else "unknown"
            if result["phase"] == "session_create" and response.status == 409:
                result["sessionCleanupRequired"] = False
            raise ProbeFailure("unexpected_http_status")
    if len(body) > MAX_BODY:
        raise ProbeFailure("response_too_large")
    try:
        return json.loads(body)
    except ValueError:
        raise ProbeFailure("invalid_session_response") from None


def create_owned_session(endpoint, headers, opener, version, session, result, deadline):
    base = endpoint + "/agents/card-orchestrator/endpoint/sessions"
    request = urllib.request.Request(
        base + "?api-version=v1",
        data=json.dumps(
            {
                "agent_session_id": session,
                "version_indicator": {"type": "version_ref", "agent_version": version},
            }
        ).encode(),
        headers=headers,
        method="POST",
    )
    if time.monotonic() >= deadline:
        raise TimeoutError()
    result.update(phase="session_create", sessionCreateAttempted=True, sessionCleanupRequired=True)
    document = session_document(opener, request, result, deadline, 201)
    for poll in range(16):
        if not isinstance(document, dict) or document.get("agent_session_id") != session:
            raise ProbeFailure("invalid_session_response")
        if document.get("version_indicator") != {
            "type": "version_ref",
            "agent_version": version,
        }:
            raise ProbeFailure("session_version_mismatch")
        result["sessionCreated"] = True
        state = document.get("status")
        if state == "active":
            if time.monotonic() >= deadline:
                raise TimeoutError()
            result["sessionReady"] = True
            return
        if state not in ("creating", "updating"):
            raise ProbeFailure("session_not_ready")
        result["phase"] = "session_ready"
        remaining = deadline - time.monotonic()
        if remaining <= 0 or poll == 15:
            raise ProbeFailure("session_readiness_timeout")
        time.sleep(min(2, remaining))
        request = urllib.request.Request(
            base + "/" + session + "?api-version=v1", headers=headers, method="GET"
        )
        document = session_document(opener, request, result, deadline, 200)


def probe(
    endpoint,
    principal,
    credential_factory=None,
    opener=None,
    invocation=None,
    *,
    prepare=False,
    require_persisted_endpoint=False,
    setup_deadline=None,
    remote_deadline=None,
):
    result = initial_result(prepare, invocation is not None and not prepare)
    if setup_deadline is None:
        setup_deadline = time.monotonic() + SESSION_SETUP_TIMEOUT
    try:
        validate_inputs(endpoint, principal)
        if type(require_persisted_endpoint) is not bool:
            raise ValueError("invalid_endpoint_requirement")
        if prepare and invocation is not None:
            raise ValueError("conflicting_modes")
        if invocation is not None:
            validate_invocation(*invocation)
    except (ValueError, TypeError):
        return {**result, "reason": "invalid_configuration"}
    if require_persisted_endpoint:
        persisted = os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
        try:
            validate_inputs(persisted, principal)
        except (ValueError, TypeError):
            return {**result, "reason": "persisted_endpoint_invalid"}
        if persisted != endpoint:
            return {**result, "reason": "persisted_endpoint_mismatch"}
        endpoint = persisted
        result.update(endpointPersisted=True, endpointSource="aca_environment")
    if not os.environ.get("IDENTITY_ENDPOINT") or not os.environ.get("IDENTITY_HEADER"):
        return {**result, "reason": "aca_identity_unavailable"}
    logging.disable(logging.CRITICAL)
    credential = None
    try:
        if prepare:
            result["parserImportReady"] = True
            invocation_body("local-preparation-only")
            result["requestSchemaReady"] = True
            result["localFixtureParseReady"] = check_local_fixture()
            if not result["localFixtureParseReady"]:
                return {**result, "reason": "local_fixture_failed"}
        if credential_factory is None:
            from azure.identity import ManagedIdentityCredential

            credential_factory = ManagedIdentityCredential
        credential = credential_factory(
            client_id=None, connection_timeout=5, read_timeout=10, retry_total=0
        )
        token = credential.get_token(SCOPE).token
        result["tokenAcquired"] = True
        if not matches_identity(token, principal):
            return {**result, "reason": "identity_claim_mismatch"}
        result["principalMatched"] = True
        headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        # Do not forward the bearer token to redirects or environment-configured proxies.
        opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )
        request = urllib.request.Request(
            endpoint + "/agents?api-version=" + API_VERSION,
            headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
            method="GET",
        )
        if invocation is not None:
            version, build, session = invocation
            body = invocation_body(session)
            create_owned_session(
                endpoint, headers, opener, version, session, result, setup_deadline
            )
            request = urllib.request.Request(
                endpoint
                + "/agents/card-orchestrator/endpoint/protocols/openai/responses?api-version=v1",
                data=json.dumps(body).encode(),
                headers=headers,
                method="POST",
            )
        if invocation is not None:
            if remote_deadline is not None:
                remaining = remote_deadline - time.monotonic()
                if remaining < INVOCATION_TIMEOUT:
                    raise TimeoutError()
                signal.setitimer(signal.ITIMER_REAL, INVOCATION_TIMEOUT)
            result["phase"] = "invoke"
            result.pop("httpStatus", None)
            result["invocationsAttempted"] = 1
        with opener.open(request, timeout=INVOCATION_TIMEOUT if invocation else 10) as response:
            result["httpStatus"] = response.status
            if response.status != 200:
                body = response.read(MAX_BODY + 1)
                result["serviceCode"] = service_code(body) if len(body) <= MAX_BODY else "unknown"
                return {**result, "reason": "unexpected_http_status"}
            body = response.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            return {**result, "reason": "response_too_large"}
        document = json.loads(body)
        if invocation is not None:
            if not isinstance(document, dict):
                return {**result, "reason": "invalid_response"}
            # The wrapper prepends these exact owned parser definitions in memory.
            parsed = globals()["_parse_success_envelope"](
                document, request_id=None, expected_version=build
            )
            result.update(
                accessVerified=True,
                schemaValid=parsed.schema_valid,
                outcome=parsed.status,
                hostedVersion=version,
                applicationVersion=build,
            )
            for key, value in (
                ("responseId", parsed.response_id),
                ("requestId", parsed.request_id),
            ):
                if value:
                    result[key] = value
            if parsed.schema_valid:
                domain = json.loads(globals()["_extract_output_text"](document))
                result["hostedVersionMatched"] = (
                    domain.get("metadata", {}).get("hostedVersion") == version
                )
                result["applicationVersionMatched"] = parsed.agent_version == build
            verified = (
                parsed.schema_valid
                and result.get("hostedVersionMatched") is True
                and result.get("applicationVersionMatched") is True
            )
            return {
                **result,
                "status": "invocation_verified" if verified else "failed",
                "invocationVerified": verified,
            }
        if not isinstance(document, dict) or not isinstance(document.get("data"), list):
            return {**result, "reason": "invalid_list_response"}
        return {
            **result,
            "status": "invocation_prepared" if prepare else "access_verified",
            "accessVerified": True,
            "agentCountOnPage": len(document["data"]),
        }
    except urllib.error.HTTPError as error:
        result.update(reason="http_error", httpStatus=error.code, serviceCode="unknown")
        if result.get("phase") == "session_create" and error.code == 409:
            result["sessionCleanupRequired"] = False
        try:
            body = error.read(MAX_BODY + 1)
            if len(body) <= MAX_BODY:
                result["serviceCode"] = service_code(body)
        except Exception:
            pass
        finally:
            error.close()
        return result
    except ProbeFailure as error:
        return {**result, "reason": str(error)}
    except ImportError:
        return {**result, "reason": "identity_dependency_missing"}
    except TimeoutError:
        return {**result, "reason": "timeout"}
    except Exception:
        return {**result, "reason": "probe_failed"}
    finally:
        if credential is not None:
            try:
                credential.close()
            except Exception:
                pass


def emit(
    endpoint,
    principal,
    invocation=None,
    *,
    prepare=False,
    parser_bundle=None,
    require_persisted_endpoint=False,
):
    def deadline(signum, frame):
        raise TimeoutError()

    signal.signal(signal.SIGALRM, deadline)
    budget = REMOTE_TIMEOUT if invocation else (70 if prepare else 45)
    remaining = signal.getitimer(signal.ITIMER_REAL)[0]
    if parser_bundle is not None and remaining:
        budget = min(budget, remaining)
    remote_deadline = time.monotonic() + budget
    setup_budget = min(SESSION_SETUP_TIMEOUT, max(0.001, budget - INVOCATION_TIMEOUT))
    setup_deadline = time.monotonic() + setup_budget
    signal.setitimer(signal.ITIMER_REAL, setup_budget if invocation else budget)
    result = initial_result(prepare, invocation is not None and not prepare)
    logging.disable(logging.CRITICAL)
    try:
        if parser_bundle is not None:
            exec(parser_bundle, globals())
        result = probe(
            endpoint,
            principal,
            invocation=invocation,
            prepare=prepare,
            require_persisted_endpoint=require_persisted_endpoint,
            setup_deadline=setup_deadline,
            remote_deadline=remote_deadline if invocation else None,
        )
    except TimeoutError:
        result["reason"] = "parser_setup_timeout"
    except Exception:
        result["reason"] = "parser_setup_failed"
    finally:
        signal.alarm(0)
    print(MARKER + json.dumps(result, sort_keys=True), flush=True)
