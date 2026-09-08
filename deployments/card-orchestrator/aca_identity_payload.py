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


def initial_result(prepare=False):
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
    return result


def invocation_body(session):
    request = globals()["GenerateCardAgentRequest"](query=SYNTHETIC_QUERY)
    return {
        "store": False,
        "stream": False,
        "session_id": session,
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
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", session):
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


def probe(
    endpoint, principal, credential_factory=None, opener=None, invocation=None, *, prepare=False
):
    result = initial_result(prepare)
    try:
        validate_inputs(endpoint, principal)
        if prepare and invocation is not None:
            raise ValueError("conflicting_modes")
        if invocation is not None:
            validate_invocation(*invocation)
            result.update(invocationsAttempted=0, schemaValid=False)
    except (ValueError, TypeError):
        return {**result, "reason": "invalid_configuration"}
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
        request = urllib.request.Request(
            endpoint + "/agents?api-version=" + API_VERSION,
            headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
            method="GET",
        )
        if invocation is not None:
            version, build, session = invocation
            # The platform consumes session_id, pinning routing to the version-ref session.
            body = invocation_body(session)
            request = urllib.request.Request(
                endpoint
                + "/agents/card-orchestrator/endpoint/protocols/openai/responses?api-version=v1",
                data=json.dumps(body).encode(),
                headers={
                    "Authorization": "Bearer " + token,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
        # Do not forward the bearer token to redirects or environment-configured proxies.
        opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )
        if invocation is not None:
            result["invocationsAttempted"] = 1
        with opener.open(request, timeout=65 if invocation else 10) as response:
            result["httpStatus"] = response.status
            if response.status != 200:
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
        # No service body, headers, request URL, or exception text leaves this process.
        error.close()
        return {**result, "reason": "http_error", "httpStatus": error.code}
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


def emit(endpoint, principal, invocation=None, *, prepare=False, parser_bundle=None):
    def deadline(signum, frame):
        raise TimeoutError()

    signal.signal(signal.SIGALRM, deadline)
    budget = 70 if invocation or prepare else 45
    remaining = signal.getitimer(signal.ITIMER_REAL)[0]
    if parser_bundle is not None and remaining:
        budget = min(budget, remaining)
    signal.setitimer(signal.ITIMER_REAL, budget)
    result = initial_result(prepare)
    logging.disable(logging.CRITICAL)
    try:
        if parser_bundle is not None:
            exec(parser_bundle, globals())
        result = probe(endpoint, principal, invocation=invocation, prepare=prepare)
    except TimeoutError:
        result["reason"] = "parser_setup_timeout"
    except Exception:
        result["reason"] = "parser_setup_failed"
    finally:
        signal.alarm(0)
    print(MARKER + json.dumps(result, sort_keys=True), flush=True)
