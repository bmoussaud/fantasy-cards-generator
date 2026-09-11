"""Bounded, metadata-only Key Vault probe for the dedicated private job."""

import hashlib
import ipaddress
import json
import logging
import os
import re
import signal
import socket
import sys

SECRET_NAMES = ("app-session-secret-key", "entra-client-secret")
SCHEMA = "private-metadata-preflight/v1"
MAX_VERSIONS = 100


def emit(status, version_hash=None):
    record = {"schema": SCHEMA, "status": status}
    if version_hash is not None:
        record["version_hash"] = version_hash
    print(json.dumps(record, sort_keys=True), flush=True)


def private_dns(hostname, expected_ip):
    expected = ipaddress.ip_address(expected_ip)
    if not any(
        expected in ipaddress.ip_network(network)
        for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    ):
        raise ValueError("invalid_private_address")
    addresses = {
        ipaddress.ip_address(result[4][0])
        for result in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    }
    if addresses != {expected}:
        raise ValueError("private_dns_mismatch")


def metadata(client):
    versions = []
    for name in SECRET_NAMES:
        count = 0
        # This endpoint is /secrets/{name}/versions, never vault-wide list or get.
        for properties in client.list_properties_of_secret_versions(name):
            count += 1
            if count > MAX_VERSIONS:
                raise ValueError("version_limit")
            version = properties.version
            if not isinstance(version, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", version):
                raise ValueError("invalid_version")
            versions.append(f"{name}:{version}")
        if count == 0:
            raise ValueError("missing_metadata")
    return hashlib.sha256("\n".join(sorted(versions)).encode("ascii")).hexdigest()


def deadline(_signum, _frame):
    raise TimeoutError("deadline")


def unexpected(_type, _value, _traceback):
    # Terminal safety boundary: SDK/import/programming errors must not expose URIs.
    emit("unexpected_error")


def main():
    logging.disable(logging.CRITICAL)
    sys.excepthook = unexpected
    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(60)
    from azure.core.exceptions import (
        ClientAuthenticationError,
        HttpResponseError,
        ServiceRequestError,
        ServiceResponseError,
    )
    from azure.core.pipeline.transport import RequestsTransport
    from azure.identity import CredentialUnavailableError, ManagedIdentityCredential
    from azure.keyvault.secrets import SecretClient

    try:
        client_id = os.environ["RUNNER_CLIENT_ID"]
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", client_id):
            raise ValueError("invalid_identity")
        hostname = "kvfcagdevqhg3qc4rlbt4g.vault.azure.net"
        private_dns(hostname, os.environ["RUNNER_PRIVATE_IP"])
        with (
            ManagedIdentityCredential(
                client_id=client_id,
                connection_timeout=5,
                read_timeout=5,
                retry_total=0,
            ) as credential,
            SecretClient(
                vault_url=f"https://{hostname}",
                credential=credential,
                transport=RequestsTransport(use_env_settings=False),
                connection_timeout=5,
                read_timeout=5,
                retry_total=0,
                logging_enable=False,
            ) as client,
        ):
            version_hash = metadata(client)
        emit("ok", version_hash)
        return 0
    except (KeyError, ValueError):
        emit("configuration_or_metadata_invalid")
    except socket.gaierror:
        emit("private_dns_failed")
    except TimeoutError:
        emit("deadline_exceeded")
    except (CredentialUnavailableError, ClientAuthenticationError):
        emit("identity_unavailable")
    except HttpResponseError as error:
        emit(
            {
                403: "metadata_forbidden",
                404: "metadata_missing",
                429: "metadata_throttled",
            }.get(error.status_code, "metadata_http_failed")
        )
    except (ServiceRequestError, ServiceResponseError):
        emit("metadata_transport_failed")
    finally:
        signal.alarm(0)
    return 1


if __name__ == "__main__":
    sys.exit(main())
