"""In-memory ACA probe: no dotenv, developer credential chain, or model requests."""

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


def probe(endpoint, principal, credential_factory=None, opener=None):
    result = {
        "status": "failed",
        "tokenAcquired": False,
        "principalMatched": False,
        "accessVerified": False,
        "invocationVerified": False,
        "endpointPersisted": False,
    }
    try:
        validate_inputs(endpoint, principal)
    except (ValueError, TypeError):
        return {**result, "reason": "invalid_configuration"}
    if not os.environ.get("IDENTITY_ENDPOINT") or not os.environ.get("IDENTITY_HEADER"):
        return {**result, "reason": "aca_identity_unavailable"}
    logging.disable(logging.CRITICAL)
    credential = None
    try:
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
        # Do not forward the bearer token to redirects or environment-configured proxies.
        opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )
        with opener.open(request, timeout=10) as response:
            result["httpStatus"] = response.status
            if response.status != 200:
                return {**result, "reason": "unexpected_http_status"}
            body = response.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            return {**result, "reason": "response_too_large"}
        document = json.loads(body)
        if not isinstance(document, dict) or not isinstance(document.get("data"), list):
            return {**result, "reason": "invalid_list_response"}
        return {
            **result,
            "status": "access_verified",
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


def emit(endpoint, principal):
    def deadline(signum, frame):
        raise TimeoutError()

    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(45)
    try:
        result = probe(endpoint, principal)
    finally:
        signal.alarm(0)
    print(MARKER + json.dumps(result, sort_keys=True), flush=True)
