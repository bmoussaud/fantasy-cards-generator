"""Fail-closed dev-only rollout of one reviewed web image by immutable digest."""

from __future__ import annotations

import argparse
import copy
import datetime
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SUBSCRIPTION = "b8ff3e15-7e2d-4fac-a773-992fb59ccedd"
GROUP = "rg-fcag-dev"
APP = "fcag-dev-app"
REGISTRY = "fcagdevqhg3qc4rlbt4gacr"
LOGIN_SERVER = f"{REGISTRY}.azurecr.io"
REPOSITORY = "fantasy-cards-generator/web-nat-dev"
TARGET_DIGEST = "sha256:bb7c5c4e49b9f3860d0f5aca5ccf2ff66e43921f512726551de7fc8c60ee8a11"
TARGET_IMAGE = f"{LOGIN_SERVER}/{REPOSITORY}@{TARGET_DIGEST}"
API_VERSION = "2025-01-01"
RESOURCE_GRAPH_API_VERSION = "2022-10-01"
ARM_RESOURCE = "https://management.azure.com/"
ARM_ENDPOINT = ARM_RESOURCE.rstrip("/")
APP_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}/providers/"
    f"Microsoft.App/containerApps/{APP}"
)
APP_URL = f"{ARM_ENDPOINT}{APP_ID}?api-version={API_VERSION}"
REVISIONS_URL = f"{ARM_ENDPOINT}{APP_ID}/revisions?api-version={API_VERSION}"
ACR_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}/providers/"
    f"Microsoft.ContainerRegistry/registries/{REGISTRY}"
)
ACR_PULL_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}/providers/"
    "Microsoft.ManagedIdentity/userAssignedIdentities/fcag-dev-acr-pull"
)
RESOURCE_GRAPH_URL = (
    f"{ARM_ENDPOINT}/providers/Microsoft.ResourceGraph/resources"
    f"?api-version={RESOURCE_GRAPH_API_VERSION}"
)
MAX_ARM_RESPONSE_BYTES = 8 * 1024 * 1024
POLL_SECONDS = 5
ROLLOUT_TIMEOUT_SECONDS = 300

CONFIGURATION_FIELDS = frozenset(
    {
        "activeRevisionsMode",
        "agentSettings",
        "dapr",
        "identitySettings",
        "ingress",
        "maxInactiveRevisions",
        "registries",
        "revisionTransitionThreshold",
        "runtime",
        "secrets",
        "service",
        "targetLabel",
    }
)
CONFIGURATION_QUERY_FIELDS = {
    "activeRevisionsMode": "activeRevisionsMode",
    "agentSettings": "agentSettings",
    "dapr": "dapr",
    "identitySettings": "identitySettings",
    "ingress": "ingress",
    "maxInactiveRevisions": "maxInactiveRevisions",
    "registries": "registries",
    "revisionTransitionThreshold": "revisionTransitionThreshold",
    "runtime": "runtime",
    "service": "service",
    "targetLabel": "targetLabel",
}
SECRET_FIELDS = frozenset({"name", "identity", "keyVaultUrl", "value"})
TEMPLATE_FIELDS = frozenset(
    {
        "containers",
        "initContainers",
        "revisionSuffix",
        "scale",
        "serviceBinds",
        "terminationGracePeriodSeconds",
        "volumes",
    }
)
REVISION_PROPERTY_FIELDS = frozenset(
    {
        "active",
        "createdTime",
        "fqdn",
        "healthState",
        "lastActiveTime",
        "provisioningError",
        "provisioningState",
        "replicas",
        "runningState",
        "template",
        "trafficWeight",
    }
)
DIRECT_VALUE_ENV_NAMES = frozenset(
    {
        "AGENT_GENERATION_ENABLED",
        "AI_MODE",
        "APP_ENV",
        "AUDIT_RETENTION_DAYS",
        "AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING",
        "BLOB_CONTAINER_NAME",
        "BLOB_ENDPOINT",
        "CONTENT_SAFETY_API_VERSION",
        "CONTENT_SAFETY_ENDPOINT",
        "CONTENT_SAFETY_MAX_HATE_SEVERITY",
        "CONTENT_SAFETY_MAX_SELF_HARM_SEVERITY",
        "CONTENT_SAFETY_MAX_SEXUAL_SEVERITY",
        "CONTENT_SAFETY_MAX_VIOLENCE_SEVERITY",
        "COSMOS_CONTAINER_NAME",
        "COSMOS_DATABASE_NAME",
        "COSMOS_ENDPOINT",
        "ENTRA_CLIENT_ID",
        "ENTRA_POST_LOGOUT_REDIRECT_URI",
        "ENTRA_REDIRECT_URI",
        "FOUNDRY_AGENT_API_VERSION",
        "FOUNDRY_AGENT_EXPECTED_VERSION",
        "FOUNDRY_AGENT_NAME",
        "FOUNDRY_API_VERSION",
        "FOUNDRY_ENDPOINT",
        "FOUNDRY_IMAGE_DEPLOYMENT",
        "FOUNDRY_PROJECT_ENDPOINT",
        "FOUNDRY_TEXT_DEPLOYMENT",
        "HEALTHZ_BLOB_TIMEOUT_MS",
        "HEALTHZ_COSMOS_TIMEOUT_MS",
        "IMAGE_MAX_RETRIES",
        "IMAGE_QUALITY",
        "IMAGE_SIZE",
        "IMAGE_TIMEOUT_SECONDS",
        "KEY_VAULT_URI",
        "MODERATION_POLICY_NAME",
        "MODERATION_SERVICE",
        "OTEL_RESOURCE_ATTRIBUTES",
        "OTEL_SERVICE_NAME",
        "OTEL_TRACES_SAMPLER",
        "OTEL_TRACES_SAMPLER_ARG",
        "OVERALL_TIMEOUT_SECONDS",
        "PERSISTENCE_MODE",
        "PROFILE_PHOTOS_CONTAINER_NAME",
        "RATE_LIMIT_IP_REQUESTS",
        "RATE_LIMIT_IP_WINDOW_SECONDS",
        "RATE_LIMIT_USER_REQUESTS",
        "RATE_LIMIT_USER_WINDOW_SECONDS",
        "SAVED_PHOTO_MAX_BYTES",
        "SAVED_PHOTO_MAX_COUNT",
        "SAVED_PHOTO_THUMBNAIL_SIZE",
        "SECRET_PROVIDER_BACKEND",
        "SECRET_PROVIDER_CACHE_TTL_SECONDS",
        "SECRET_PROVIDER_MAX_RETRIES",
        "SECRET_PROVIDER_MAX_STALE_SECONDS",
        "SECRET_PROVIDER_REQUEST_TIMEOUT_SECONDS",
        "SECRET_PROVIDER_RETRY_BACKOFF_SECONDS",
        "TELEMETRY_ENABLED",
        "TELEMETRY_SAMPLING_RATIO",
        "TEXT_TIMEOUT_SECONDS",
        "TRUSTED_PROXY_HOPS",
        "UPSTREAM_BASE_BACKOFF_SECONDS",
        "UPSTREAM_MAX_RETRIES",
    }
)
SECRET_REFERENCE_ENV = {
    "APPLICATIONINSIGHTS_CONNECTION_STRING": "applicationinsights-connection-string"
}
PROVIDER_ONLY_ENV_NAMES = frozenset({"APP_SESSION_SECRET_KEY", "ENTRA_CLIENT_SECRET"})

APP_CONFIGURATION_QUERY = f"""
resources
| where type =~ 'microsoft.app/containerapps'
| where id =~ '{APP_ID}'
| project recordType = 'app', data = bag_pack(
    'id', id,
    'name', name,
    'type', type,
    'location', location,
    'tagFields', bag_keys(tags),
    'azdServiceName', tostring(tags['azd-service-name']),
    'identity', identity,
    'provisioningState', tostring(properties.provisioningState),
    'runningStatus', tostring(properties.runningStatus),
    'latestRevisionName', tostring(properties.latestRevisionName),
    'latestReadyRevisionName', tostring(properties.latestReadyRevisionName),
    'latestRevisionFqdn', tostring(properties.latestRevisionFqdn),
    'environmentId', tostring(properties.environmentId),
    'managedEnvironmentId', tostring(properties.managedEnvironmentId),
    'workloadProfileName', tostring(properties.workloadProfileName),
    'configurationFields', bag_keys(properties.configuration),
    'secretCount', array_length(properties.configuration.secrets),
    'activeRevisionsMode', properties.configuration.activeRevisionsMode,
    'agentSettings', properties.configuration.agentSettings,
    'dapr', properties.configuration.dapr,
    'identitySettings', properties.configuration.identitySettings,
    'ingress', properties.configuration.ingress,
    'maxInactiveRevisions', properties.configuration.maxInactiveRevisions,
    'registries', properties.configuration.registries,
    'revisionTransitionThreshold', properties.configuration.revisionTransitionThreshold,
    'runtime', properties.configuration.runtime,
    'service', properties.configuration.service,
    'targetLabel', properties.configuration.targetLabel
)
""".strip()
APP_SECRET_METADATA_QUERY = f"""
resources
| where type =~ 'microsoft.app/containerapps'
| where id =~ '{APP_ID}'
| mv-expand secret = properties.configuration.secrets
| project recordType = 'secret', data = bag_pack(
    'latestRevisionName', tostring(properties.latestRevisionName),
    'fields', bag_keys(secret),
    'name', tostring(secret.name),
    'identity', tostring(secret.identity),
    'keyVaultUrl', tostring(secret.keyVaultUrl)
)
""".strip()


def template_metadata_queries(group: str) -> tuple[str, str]:
    if group not in {"containers", "initContainers"}:
        raise ValueError("unsupported template group")
    container_query = f"""
resources
| where type =~ 'microsoft.app/containerapps'
| where id =~ '{APP_ID}'
| mv-expand with_itemindex=containerIndex container = properties.template.{group}
| project recordType = 'container', data = bag_pack(
    'latestRevisionName', tostring(properties.latestRevisionName),
    'containerGroup', '{group}',
    'containerIndex', containerIndex,
    'name', tostring(container.name),
    'fields', bag_keys(container),
    'environmentCount', array_length(container.env)
)
""".strip()
    environment_query = f"""
resources
| where type =~ 'microsoft.app/containerapps'
| where id =~ '{APP_ID}'
| mv-expand with_itemindex=containerIndex container = properties.template.{group}
| mv-expand with_itemindex=environmentIndex environment = container.env
| project recordType = 'environment', data = bag_pack(
    'latestRevisionName', tostring(properties.latestRevisionName),
    'containerGroup', '{group}',
    'containerIndex', containerIndex,
    'environmentIndex', environmentIndex,
    'name', tostring(environment.name),
    'secretRef', tostring(environment.secretRef),
    'fields', bag_keys(environment)
)
""".strip()
    return container_query, environment_query


MAIN_CONTAINER_METADATA_QUERY, MAIN_ENVIRONMENT_METADATA_QUERY = template_metadata_queries(
    "containers"
)
INIT_CONTAINER_METADATA_QUERY, INIT_ENVIRONMENT_METADATA_QUERY = template_metadata_queries(
    "initContainers"
)
ACR_METADATA_QUERY = f"""
resources
| where type =~ 'microsoft.containerregistry/registries'
| where id =~ '{ACR_ID}'
| project id, name, type, location, loginServer = tostring(properties.loginServer)
""".strip()


class PreflightError(Exception):
    """Carries only fixed, non-sensitive failure codes."""


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(NoRedirectHandler())


def require(condition: bool, code: str) -> None:
    if not condition:
        raise PreflightError(code)


def open_url(request: urllib.request.Request, timeout: int):
    return _OPENER.open(request, timeout=timeout)


def run_azure_json(args: list[str]) -> object:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise PreflightError("azure_transport_failed") from None
    require(result.returncode == 0, "azure_command_failed")
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise PreflightError("azure_response_invalid") from None


def get_arm_token() -> str:
    result = run_azure_json(
        [
            "az",
            "account",
            "get-access-token",
            "--subscription",
            SUBSCRIPTION,
            "--resource",
            ARM_RESOURCE,
            "--only-show-errors",
            "--output",
            "json",
        ]
    )
    require(isinstance(result, dict), "arm_token_invalid")
    token = result.get("accessToken")
    require(
        isinstance(token, str)
        and bool(token)
        and token == token.strip()
        and not any(character.isspace() for character in token),
        "arm_token_invalid",
    )
    return token


def arm_request(
    token: str,
    *,
    method: str,
    url: str,
    payload: object | None = None,
    read_response: bool = True,
    not_found_code: str = "azure_resource_not_found",
    resource_graph_query: bool = False,
) -> object | None:
    require(url.startswith(f"{ARM_ENDPOINT}/"), "arm_url_invalid")
    require(
        isinstance(token, str)
        and bool(token)
        and not any(character.isspace() for character in token),
        "arm_token_invalid",
    )
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        response = open_url(request, 120)
    except urllib.error.HTTPError as exc:
        try:
            if exc.code == 404:
                raise PreflightError(not_found_code) from None
            if exc.code in {401, 403}:
                raise PreflightError("azure_authorization_failed") from None
            if resource_graph_query and exc.code == 400:
                raise PreflightError(resource_graph_error_detail(exc)) from None
            raise PreflightError("azure_request_failed") from None
        finally:
            exc.close()
    except (OSError, TimeoutError, urllib.error.URLError):
        raise PreflightError("azure_transport_failed") from None

    try:
        status = getattr(response, "status", response.getcode())
        require(status in ({200, 202} if method == "PATCH" else {200}), "azure_status_invalid")
        require(getattr(response, "geturl", lambda: url)() == url, "azure_redirect_blocked")
        if not read_response:
            return None
        raw = response.read(MAX_ARM_RESPONSE_BYTES + 1)
        require(len(raw) <= MAX_ARM_RESPONSE_BYTES, "azure_response_too_large")
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            raise PreflightError("azure_response_invalid") from None
    finally:
        response.close()


def resource_graph_error_detail(exc: urllib.error.HTTPError) -> str:
    raw = exc.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        return "resource_graph_query_invalid"
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        return "resource_graph_query_invalid"
    error = payload.get("error") if isinstance(payload, dict) else None
    details = error.get("details") if isinstance(error, dict) else None
    if not isinstance(details, list):
        return "resource_graph_query_invalid"
    for detail in reversed(details):
        if not isinstance(detail, dict):
            continue
        code = detail.get("code")
        message = detail.get("message")
        if (
            isinstance(code, str)
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", code)
            and isinstance(message, str)
            and len(message) <= 500
            and re.fullmatch(r"""[A-Za-z0-9 _.,:;'"()\[\]/=-]+""", message)
        ):
            return f"resource_graph_query_invalid:{code}:{message}"
    return "resource_graph_query_invalid"


def resource_graph_records(token: str, query: str) -> list[dict[str, object]]:
    result = arm_request(
        token,
        method="POST",
        url=RESOURCE_GRAPH_URL,
        payload={
            "subscriptions": [SUBSCRIPTION],
            "query": query,
            "options": {"resultFormat": "objectArray"},
        },
        resource_graph_query=True,
    )
    require(isinstance(result, dict), "resource_graph_response_invalid")
    records = result.get("data")
    require(isinstance(records, list), "resource_graph_response_invalid")
    require(
        result.get("resultTruncated") in {False, "false"}
        and result.get("count", len(records)) == len(records),
        "resource_graph_response_invalid",
    )
    for record in records:
        require(isinstance(record, dict), "resource_graph_response_invalid")
    return records


def app_metadata_records(token: str) -> list[dict[str, object]]:
    return [
        *resource_graph_records(token, APP_CONFIGURATION_QUERY),
        *resource_graph_records(token, APP_SECRET_METADATA_QUERY),
    ]


def manifest(digest: str) -> None:
    result = run_azure_json(
        [
            "az",
            "acr",
            "manifest",
            "show-metadata",
            "--registry",
            REGISTRY,
            "--name",
            f"{REPOSITORY}@{digest}",
            "--subscription",
            SUBSCRIPTION,
            "--only-show-errors",
            "--output",
            "json",
        ]
    )
    require(
        isinstance(result, dict)
        and result.get("digest") == digest
        and result.get("mediaType") == "application/vnd.oci.image.index.v1+json",
        "image_manifest_invalid",
    )


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def casefold_id(value: object) -> str:
    require(isinstance(value, str) and bool(value), "arm_identity_invalid")
    return value.casefold()


def known_object(value: object, fields: frozenset[str], code: str) -> dict[str, object]:
    require(isinstance(value, dict) and set(value) <= fields, code)
    return value


def known_list(value: object, code: str) -> list[object]:
    require(isinstance(value, list), code)
    return value


def integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_optional_types(
    value: dict[str, object],
    *,
    strings: frozenset[str] = frozenset(),
    integers: frozenset[str] = frozenset(),
    booleans: frozenset[str] = frozenset(),
    string_lists: frozenset[str] = frozenset(),
) -> None:
    for field in strings & value.keys():
        require(
            value[field] is None or isinstance(value[field], str),
            "configuration_schema_invalid",
        )
    for field in integers & value.keys():
        require(
            value[field] is None or integer(value[field]),
            "configuration_schema_invalid",
        )
    for field in booleans & value.keys():
        require(
            value[field] is None or isinstance(value[field], bool),
            "configuration_schema_invalid",
        )
    for field in string_lists & value.keys():
        require(
            value[field] is None
            or (
                isinstance(value[field], list)
                and all(isinstance(item, str) for item in value[field])
            ),
            "configuration_schema_invalid",
        )


def validate_configuration(configuration: object) -> dict[str, object]:
    config = known_object(configuration, CONFIGURATION_FIELDS, "configuration_schema_invalid")

    if "activeRevisionsMode" in config:
        require(
            config["activeRevisionsMode"] in {"Single", "Multiple"},
            "configuration_schema_invalid",
        )
    if "maxInactiveRevisions" in config:
        require(
            config["maxInactiveRevisions"] is None
            or (integer(config["maxInactiveRevisions"]) and config["maxInactiveRevisions"] >= 0),
            "configuration_schema_invalid",
        )
    if "agentSettings" in config and config["agentSettings"] is not None:
        agent_settings = known_object(
            config["agentSettings"],
            frozenset({"discoveryMode", "isAgent"}),
            "configuration_schema_invalid",
        )
        validate_optional_types(
            agent_settings,
            strings=frozenset({"discoveryMode"}),
            booleans=frozenset({"isAgent"}),
        )
    if "revisionTransitionThreshold" in config:
        require(
            config["revisionTransitionThreshold"] is None
            or (
                integer(config["revisionTransitionThreshold"])
                and config["revisionTransitionThreshold"] >= 0
            ),
            "configuration_schema_invalid",
        )
    if "targetLabel" in config:
        require(
            config["targetLabel"] is None or isinstance(config["targetLabel"], str),
            "configuration_schema_invalid",
        )
    if "dapr" in config and config["dapr"] is not None:
        dapr = known_object(
            config["dapr"],
            frozenset(
                {
                    "appId",
                    "appPort",
                    "appProtocol",
                    "enableApiLogging",
                    "enabled",
                    "httpMaxRequestSize",
                    "httpReadBufferSize",
                    "logLevel",
                }
            ),
            "configuration_schema_invalid",
        )
        validate_optional_types(
            dapr,
            strings=frozenset({"appId", "appProtocol", "logLevel"}),
            integers=frozenset({"appPort", "httpMaxRequestSize", "httpReadBufferSize"}),
            booleans=frozenset({"enableApiLogging", "enabled"}),
        )
    if "identitySettings" in config and config["identitySettings"] is not None:
        for setting in known_list(config["identitySettings"], "configuration_schema_invalid"):
            identity_setting = known_object(
                setting,
                frozenset({"identity", "lifecycle"}),
                "configuration_schema_invalid",
            )
            validate_optional_types(
                identity_setting,
                strings=frozenset({"identity", "lifecycle"}),
            )
    if "ingress" in config and config["ingress"] is not None:
        ingress = known_object(
            config["ingress"],
            frozenset(
                {
                    "additionalPortMappings",
                    "allowInsecure",
                    "clientCertificateMode",
                    "corsPolicy",
                    "customDomains",
                    "exposedPort",
                    "external",
                    "fqdn",
                    "ipSecurityRestrictions",
                    "stickySessions",
                    "targetPort",
                    "targetPortHttpScheme",
                    "traffic",
                    "transport",
                }
            ),
            "configuration_schema_invalid",
        )
        nested_lists = {
            "additionalPortMappings": frozenset({"exposedPort", "external", "targetPort"}),
            "customDomains": frozenset({"bindingType", "certificateId", "name"}),
            "ipSecurityRestrictions": frozenset(
                {"action", "description", "ipAddressRange", "name"}
            ),
            "traffic": frozenset({"label", "latestRevision", "revisionName", "weight"}),
        }
        for name, fields in nested_lists.items():
            if name in ingress and ingress[name] is not None:
                for item in known_list(ingress[name], "configuration_schema_invalid"):
                    nested = known_object(item, fields, "configuration_schema_invalid")
                    if name == "additionalPortMappings":
                        validate_optional_types(
                            nested,
                            integers=frozenset({"exposedPort", "targetPort"}),
                            booleans=frozenset({"external"}),
                        )
                    else:
                        validate_optional_types(
                            nested,
                            strings=fields - {"latestRevision", "weight"},
                            integers=frozenset({"weight"}),
                            booleans=frozenset({"latestRevision"}),
                        )
        if "corsPolicy" in ingress and ingress["corsPolicy"] is not None:
            cors = known_object(
                ingress["corsPolicy"],
                frozenset(
                    {
                        "allowCredentials",
                        "allowedHeaders",
                        "allowedMethods",
                        "allowedOrigins",
                        "exposeHeaders",
                        "maxAge",
                    }
                ),
                "configuration_schema_invalid",
            )
            validate_optional_types(
                cors,
                integers=frozenset({"maxAge"}),
                booleans=frozenset({"allowCredentials"}),
                string_lists=frozenset(
                    {
                        "allowedHeaders",
                        "allowedMethods",
                        "allowedOrigins",
                        "exposeHeaders",
                    }
                ),
            )
        if "stickySessions" in ingress and ingress["stickySessions"] is not None:
            sticky_sessions = known_object(
                ingress["stickySessions"],
                frozenset({"affinity"}),
                "configuration_schema_invalid",
            )
            validate_optional_types(
                sticky_sessions,
                strings=frozenset({"affinity"}),
            )
        validate_optional_types(
            ingress,
            strings=frozenset(
                {"clientCertificateMode", "fqdn", "targetPortHttpScheme", "transport"}
            ),
            integers=frozenset({"exposedPort", "targetPort"}),
            booleans=frozenset({"allowInsecure", "external"}),
        )
    if "registries" in config and config["registries"] is not None:
        for registry in known_list(config["registries"], "configuration_schema_invalid"):
            registry_metadata = known_object(
                registry,
                frozenset({"identity", "passwordSecretRef", "server", "username"}),
                "configuration_schema_invalid",
            )
            validate_optional_types(
                registry_metadata,
                strings=frozenset({"identity", "passwordSecretRef", "server", "username"}),
            )
    if "runtime" in config and config["runtime"] is not None:
        runtime = known_object(
            config["runtime"], frozenset({"java"}), "configuration_schema_invalid"
        )
        if "java" in runtime:
            java = known_object(
                runtime["java"],
                frozenset({"enableMetrics"}),
                "configuration_schema_invalid",
            )
            validate_optional_types(java, booleans=frozenset({"enableMetrics"}))
    if "secrets" in config:
        for secret in known_list(config["secrets"], "configuration_schema_invalid"):
            secret_metadata = known_object(
                secret,
                frozenset({"name", "identity", "keyVaultUrl"}),
                "configuration_schema_invalid",
            )
            require(
                isinstance(secret_metadata.get("name"), str) and bool(secret_metadata["name"]),
                "configuration_schema_invalid",
            )
            validate_optional_types(
                secret_metadata,
                strings=frozenset({"name", "identity", "keyVaultUrl"}),
            )
    if "service" in config and config["service"] is not None:
        service = known_object(
            config["service"], frozenset({"type"}), "configuration_schema_invalid"
        )
        validate_optional_types(service, strings=frozenset({"type"}))
    return config


def app_metadata_from_records(records: list[dict[str, object]]) -> dict[str, object]:
    app_records = [record for record in records if record.get("recordType") == "app"]
    secret_records = [record for record in records if record.get("recordType") == "secret"]
    require(
        len(app_records) == 1 and len(app_records) + len(secret_records) == len(records),
        "app_metadata_invalid",
    )
    app_data = app_records[0].get("data")
    require(isinstance(app_data, dict), "app_metadata_invalid")
    required_app_fields = {
        "id",
        "name",
        "type",
        "location",
        "tagFields",
        "azdServiceName",
        "identity",
        "provisioningState",
        "runningStatus",
        "latestRevisionName",
        "latestReadyRevisionName",
        "latestRevisionFqdn",
        "environmentId",
        "managedEnvironmentId",
        "workloadProfileName",
        "configurationFields",
        "secretCount",
        *CONFIGURATION_QUERY_FIELDS.values(),
    }
    require(set(app_data) == required_app_fields, "app_metadata_invalid")
    configuration_fields = app_data["configurationFields"]
    require(
        isinstance(configuration_fields, list)
        and all(isinstance(field, str) for field in configuration_fields)
        and set(configuration_fields) <= CONFIGURATION_FIELDS,
        "configuration_schema_invalid",
    )
    secret_count = app_data["secretCount"]
    if "secrets" not in configuration_fields and secret_count is None:
        secret_count = 0
    require(
        isinstance(secret_count, int)
        and not isinstance(secret_count, bool)
        and secret_count >= 0
        and secret_count == len(secret_records),
        "secret_metadata_invalid",
    )

    configuration: dict[str, object] = {}
    for field, query_field in CONFIGURATION_QUERY_FIELDS.items():
        if field in configuration_fields:
            configuration[field] = copy.deepcopy(app_data[query_field])

    secrets: list[dict[str, object]] = []
    for record in secret_records:
        data = record.get("data")
        require(
            isinstance(data, dict)
            and set(data) == {"latestRevisionName", "fields", "name", "identity", "keyVaultUrl"}
            and data["latestRevisionName"] == app_data["latestRevisionName"],
            "secret_metadata_invalid",
        )
        fields = data["fields"]
        require(
            isinstance(fields, list)
            and all(isinstance(field, str) for field in fields)
            and set(fields) <= SECRET_FIELDS,
            "secret_metadata_invalid",
        )
        name = data["name"]
        require(isinstance(name, str) and bool(name), "secret_metadata_invalid")
        sanitized: dict[str, object] = {"name": name}
        for field in ("identity", "keyVaultUrl"):
            value = data[field]
            require(isinstance(value, str), "secret_metadata_invalid")
            if value:
                sanitized[field] = value
        secrets.append(sanitized)
    require(
        len({secret["name"] for secret in secrets}) == len(secrets),
        "secret_metadata_invalid",
    )
    if "secrets" in configuration_fields:
        configuration["secrets"] = secrets
    else:
        require(not secrets, "secret_metadata_invalid")

    result = {
        key: copy.deepcopy(app_data[key])
        for key in (
            "id",
            "name",
            "type",
            "location",
            "tagFields",
            "azdServiceName",
            "identity",
        )
    }
    result["properties"] = {
        key: copy.deepcopy(app_data[key])
        for key in (
            "provisioningState",
            "runningStatus",
            "latestRevisionName",
            "latestReadyRevisionName",
            "latestRevisionFqdn",
            "environmentId",
            "managedEnvironmentId",
            "workloadProfileName",
        )
    }
    result["properties"]["configuration"] = validate_configuration(configuration)
    return result


def validate_environment_metadata(
    *,
    name: object,
    fields: object,
    secret_ref: object,
) -> dict[str, object]:
    require(
        isinstance(name, str)
        and bool(name)
        and isinstance(fields, list)
        and all(isinstance(field, str) for field in fields)
        and set(fields) <= {"name", "value", "secretRef"}
        and "name" in fields
        and isinstance(secret_ref, str),
        "environment_inventory_invalid",
    )
    require(name not in PROVIDER_ONLY_ENV_NAMES, "provider_secret_env_present")
    has_value = "value" in fields
    has_secret_ref = "secretRef" in fields
    require(has_value != has_secret_ref, "environment_inventory_invalid")
    if has_value:
        require(name in DIRECT_VALUE_ENV_NAMES, "direct_environment_not_allowlisted")
        require(not secret_ref, "environment_inventory_invalid")
    else:
        require(
            name in SECRET_REFERENCE_ENV and secret_ref == SECRET_REFERENCE_ENV[name],
            "secret_reference_invalid",
        )
    return {
        "name": name,
        "hasValue": has_value,
        "secretRef": secret_ref or None,
    }


def template_inventory_from_records(
    records: list[dict[str, object]], group: str
) -> tuple[str | None, list[dict[str, object]]]:
    require(group in {"containers", "initContainers"}, "template_group_invalid")
    container_records = [record for record in records if record.get("recordType") == "container"]
    environment_records = [
        record for record in records if record.get("recordType") == "environment"
    ]
    require(
        len(container_records) + len(environment_records) == len(records),
        "environment_inventory_invalid",
    )
    revision_names: set[str] = set()
    containers: list[dict[str, object]] = []
    seen_indexes: set[int] = set()
    matched_environment_records = 0
    for record in container_records:
        data = record.get("data")
        require(
            isinstance(data, dict)
            and set(data)
            == {
                "latestRevisionName",
                "containerGroup",
                "containerIndex",
                "name",
                "fields",
                "environmentCount",
            },
            "environment_inventory_invalid",
        )
        revision_name = data["latestRevisionName"]
        index = data["containerIndex"]
        environment_count = data["environmentCount"]
        require(
            isinstance(revision_name, str)
            and bool(revision_name)
            and data["containerGroup"] == group
            and isinstance(index, int)
            and not isinstance(index, bool)
            and index >= 0
            and index not in seen_indexes
            and isinstance(data["name"], str)
            and bool(data["name"])
            and isinstance(data["fields"], list)
            and "name" in data["fields"]
            and isinstance(environment_count, (int, type(None))),
            "environment_inventory_invalid",
        )
        revision_names.add(revision_name)
        seen_indexes.add(index)
        matching = []
        for environment_record in environment_records:
            environment = environment_record.get("data")
            require(isinstance(environment, dict), "environment_inventory_invalid")
            if (
                environment.get("containerGroup") == group
                and environment.get("containerIndex") == index
            ):
                require(
                    set(environment)
                    == {
                        "latestRevisionName",
                        "containerGroup",
                        "containerIndex",
                        "environmentIndex",
                        "name",
                        "secretRef",
                        "fields",
                    }
                    and environment["latestRevisionName"] == revision_name
                    and isinstance(environment["environmentIndex"], int)
                    and not isinstance(environment["environmentIndex"], bool)
                    and environment["environmentIndex"] >= 0,
                    "environment_inventory_invalid",
                )
                matching.append(environment)
                matched_environment_records += 1
        matching.sort(key=lambda item: item["environmentIndex"])
        require(
            [item["environmentIndex"] for item in matching] == list(range(len(matching)))
            and (environment_count or 0) == len(matching),
            "environment_inventory_invalid",
        )
        environments = [
            validate_environment_metadata(
                name=item["name"],
                fields=item["fields"],
                secret_ref=item["secretRef"],
            )
            for item in matching
        ]
        require(
            len({item["name"] for item in environments}) == len(environments),
            "duplicate_environment_name",
        )
        containers.append(
            {
                "group": group,
                "index": index,
                "name": data["name"],
                "environment": environments,
            }
        )
    containers.sort(key=lambda item: item["index"])
    require(
        [item["index"] for item in containers] == list(range(len(containers))),
        "environment_inventory_invalid",
    )
    if environment_records:
        require(container_records, "environment_inventory_invalid")
    require(
        matched_environment_records == len(environment_records),
        "environment_inventory_invalid",
    )
    require(len(revision_names) <= 1, "environment_inventory_invalid")
    return next(iter(revision_names), None), containers


def revision_environment_inventory(template: dict[str, object]) -> list[dict[str, object]]:
    inventory: list[dict[str, object]] = []
    for group in ("containers", "initContainers"):
        raw_containers = template.get(group, [])
        if raw_containers is None:
            raw_containers = []
        require(isinstance(raw_containers, list), "revision_template_invalid")
        for index, container in enumerate(raw_containers):
            require(
                isinstance(container, dict)
                and isinstance(container.get("name"), str)
                and bool(container["name"]),
                "revision_template_invalid",
            )
            raw_environment = container.get("env", [])
            require(isinstance(raw_environment, list), "revision_template_invalid")
            environment = []
            for variable in raw_environment:
                require(isinstance(variable, dict), "revision_template_invalid")
                environment.append(
                    validate_environment_metadata(
                        name=variable.get("name"),
                        fields=list(variable),
                        secret_ref=variable.get("secretRef", ""),
                    )
                )
            require(
                len({item["name"] for item in environment}) == len(environment),
                "duplicate_environment_name",
            )
            inventory.append(
                {
                    "group": group,
                    "index": index,
                    "name": container["name"],
                    "environment": environment,
                }
            )
    return inventory


def revision_created_time(revision: dict[str, object]) -> datetime.datetime:
    properties = revision.get("properties")
    require(isinstance(properties, dict), "revision_metadata_invalid")
    value = properties.get("createdTime")
    require(isinstance(value, str) and bool(value), "revision_metadata_invalid")
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise PreflightError("revision_metadata_invalid") from None
    require(
        parsed.tzinfo is not None and parsed.utcoffset() is not None,
        "revision_metadata_invalid",
    )
    return parsed.astimezone(datetime.timezone.utc)


def validate_revision_resource(
    result: object,
    *,
    expected_name: str | None = None,
    require_template: bool,
) -> dict[str, object]:
    require(
        isinstance(result, dict)
        and set(result) <= {"id", "name", "type", "properties", "systemData"},
        "revision_metadata_invalid",
    )
    name = result.get("name")
    require(
        isinstance(name, str)
        and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", name) is not None
        and (expected_name is None or name == expected_name)
        and casefold_id(result.get("id")) == f"{APP_ID}/revisions/{name}".casefold()
        and casefold_id(result.get("type")) == "microsoft.app/containerapps/revisions",
        "revision_metadata_invalid",
    )
    properties = result.get("properties")
    require(
        isinstance(properties, dict) and set(properties) <= REVISION_PROPERTY_FIELDS,
        "revision_metadata_invalid",
    )
    revision_created_time(result)
    if "lastActiveTime" in properties:
        last_active = properties["lastActiveTime"]
        require(isinstance(last_active, str) and bool(last_active), "revision_metadata_invalid")
        try:
            parsed = datetime.datetime.fromisoformat(last_active.replace("Z", "+00:00"))
        except ValueError:
            raise PreflightError("revision_metadata_invalid") from None
        require(
            parsed.tzinfo is not None and parsed.utcoffset() is not None,
            "revision_metadata_invalid",
        )
    template = properties.get("template")
    require(
        (not require_template and template is None)
        or (isinstance(template, dict) and set(template) <= TEMPLATE_FIELDS),
        "revision_metadata_invalid",
    )
    if require_template:
        require(isinstance(template, dict), "revision_metadata_invalid")
    return result


def validate_revisions_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    expected = urllib.parse.urlsplit(REVISIONS_URL)
    require(
        parsed.scheme == expected.scheme
        and parsed.netloc == expected.netloc
        and parsed.path.casefold() == expected.path.casefold()
        and bool(parsed.query),
        "revision_pagination_invalid",
    )
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    require(
        query.get("api-version") == [API_VERSION]
        and set(query)
        in (
            {"api-version"},
            {"api-version", "pageSize", "skipToken"},
        ),
        "revision_pagination_invalid",
    )
    if "skipToken" in query:
        page_sizes = query["pageSize"]
        skip_tokens = query["skipToken"]
        require(
            len(page_sizes) == 1
            and re.fullmatch(r"[0-9]{1,4}", page_sizes[0]) is not None
            and 1 <= int(page_sizes[0]) <= 1000
            and len(skip_tokens) == 1
            and 1 <= len(skip_tokens[0]) <= 4096
            and re.fullmatch(r"[A-Za-z0-9._~+/=-]+", skip_tokens[0]) is not None,
            "revision_pagination_invalid",
        )


def list_revisions(token: str) -> list[dict[str, object]]:
    url: str | None = REVISIONS_URL
    visited: set[str] = set()
    revisions: list[dict[str, object]] = []
    while url is not None:
        validate_revisions_url(url)
        require(url not in visited and len(visited) < 100, "revision_pagination_invalid")
        visited.add(url)
        result = arm_request(token, method="GET", url=url)
        require(
            isinstance(result, dict) and set(result) <= {"value", "nextLink"},
            "revision_list_invalid",
        )
        value = result.get("value")
        require(isinstance(value, list), "revision_list_invalid")
        revisions.extend(validate_revision_resource(item, require_template=False) for item in value)
        next_link = result.get("nextLink")
        require(next_link is None or isinstance(next_link, str), "revision_list_invalid")
        url = next_link
    names = [revision["name"] for revision in revisions]
    require(
        revisions and len(names) == len(set(names)),
        "revision_list_invalid",
    )
    return revisions


def revision_is_in_progress(revision: dict[str, object]) -> bool:
    properties = revision["properties"]
    require(isinstance(properties, dict), "revision_metadata_invalid")
    return properties.get("provisioningState") == "Provisioning" or properties.get(
        "runningState"
    ) in {"Processing", "Unknown"}


def revision_is_active_healthy(revision: dict[str, object]) -> bool:
    properties = revision["properties"]
    require(isinstance(properties, dict), "revision_metadata_invalid")
    replicas = properties.get("replicas")
    return (
        properties.get("active") is True
        and properties.get("healthState") == "Healthy"
        and properties.get("provisioningState") == "Provisioned"
        and properties.get("runningState") == "Running"
        and isinstance(replicas, int)
        and not isinstance(replicas, bool)
        and replicas >= 1
    )


def stable_direct_revision(token: str) -> dict[str, object]:
    revisions = list_revisions(token)
    require(
        not any(revision_is_in_progress(revision) for revision in revisions),
        "revision_in_progress",
    )
    active = [
        revision
        for revision in revisions
        if isinstance(revision["properties"], dict) and revision["properties"].get("active") is True
    ]
    active_healthy = [revision for revision in revisions if revision_is_active_healthy(revision)]
    require(
        len(active) == 1
        and len(active_healthy) == 1
        and active[0]["name"] == active_healthy[0]["name"],
        "active_revision_contract_invalid",
    )
    selected = active_healthy[0]
    selected_time = revision_created_time(selected)
    require(
        not any(
            revision["name"] != selected["name"]
            and revision_created_time(revision) >= selected_time
            for revision in revisions
        ),
        "newer_revision_exists",
    )
    direct = read_revision(token, selected["name"])
    require(
        revision_created_time(direct) == selected_time and revision_is_active_healthy(direct),
        "revision_metadata_drift",
    )
    require(
        canonical_hash(list_revisions(token)) == canonical_hash(revisions),
        "revision_metadata_drift",
    )
    return direct


def read_revision(token: str, revision_name: str) -> dict[str, object]:
    require(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", revision_name) is not None,
        "revision_name_invalid",
    )
    url = f"{ARM_ENDPOINT}{APP_ID}/revisions/{revision_name}?api-version={API_VERSION}"
    result = arm_request(
        token,
        method="GET",
        url=url,
        not_found_code="revision_not_available",
    )
    return validate_revision_resource(
        result,
        expected_name=revision_name,
        require_template=True,
    )


def registry_metadata(token: str) -> dict[str, object]:
    records = resource_graph_records(token, ACR_METADATA_QUERY)
    require(len(records) == 1, "registry_metadata_invalid")
    result = records[0]
    require(
        set(result) == {"id", "name", "type", "location", "loginServer"}
        and casefold_id(result.get("id")) == ACR_ID.casefold()
        and result.get("name") == REGISTRY
        and casefold_id(result.get("type")) == "microsoft.containerregistry/registries"
        and isinstance(result.get("location"), str)
        and bool(result["location"])
        and isinstance(result.get("loginServer"), str)
        and result["loginServer"].casefold() == LOGIN_SERVER.casefold(),
        "registry_metadata_invalid",
    )
    return result


def safe_snapshot(token: str) -> dict[str, object]:
    first_app = app_metadata_from_records(app_metadata_records(token))
    revision = stable_direct_revision(token)
    revision_properties = revision["properties"]
    require(isinstance(revision_properties, dict), "revision_metadata_invalid")
    template = revision_properties["template"]
    require(isinstance(template, dict), "revision_metadata_invalid")
    revision_environment_inventory(template)

    second_app = app_metadata_from_records(app_metadata_records(token))
    require(first_app == second_app, "metadata_drift_during_read")
    result = copy.deepcopy(second_app)
    result_properties = result["properties"]
    require(isinstance(result_properties, dict), "app_metadata_invalid")
    result_properties["latestRevisionName"] = revision["name"]
    result_properties["latestReadyRevisionName"] = revision["name"]
    if isinstance(revision_properties.get("fqdn"), str):
        result_properties["latestRevisionFqdn"] = revision_properties["fqdn"]
    result_properties["template"] = copy.deepcopy(template)
    result_properties["revisionState"] = {
        "name": revision["name"],
        "active": revision_properties.get("active"),
        "healthState": revision_properties.get("healthState"),
        "provisioningState": revision_properties.get("provisioningState"),
        "replicas": revision_properties.get("replicas"),
        "runningState": revision_properties.get("runningState"),
    }
    validate_snapshot(result, require_stable=False)
    return result


def validate_registry_contract(
    snapshot: dict[str, object], registry_resource: dict[str, object]
) -> None:
    require(
        set(registry_resource) == {"id", "name", "type", "location", "loginServer"}
        and casefold_id(registry_resource.get("id")) == ACR_ID.casefold()
        and registry_resource.get("name") == REGISTRY
        and casefold_id(registry_resource.get("type")) == "microsoft.containerregistry/registries"
        and isinstance(registry_resource.get("location"), str)
        and bool(registry_resource["location"])
        and isinstance(registry_resource.get("loginServer"), str)
        and registry_resource["loginServer"].casefold() == LOGIN_SERVER.casefold(),
        "registry_metadata_invalid",
    )
    identity = snapshot.get("identity")
    require(isinstance(identity, dict), "pull_identity_invalid")
    identity_type = identity.get("type")
    require(isinstance(identity_type, str), "pull_identity_invalid")
    identity_types = {part.strip().casefold() for part in identity_type.split(",")}
    user_assigned = identity.get("userAssignedIdentities")
    require(
        identity_types == {"systemassigned", "userassigned"}
        and isinstance(user_assigned, dict)
        and {casefold_id(resource_id) for resource_id in user_assigned} == {ACR_PULL_ID.casefold()},
        "pull_identity_invalid",
    )
    properties = snapshot.get("properties")
    require(isinstance(properties, dict), "app_metadata_invalid")
    configuration = properties.get("configuration")
    require(isinstance(configuration, dict), "configuration_schema_invalid")
    registries = configuration.get("registries")
    require(isinstance(registries, list) and len(registries) == 1, "registry_contract_invalid")
    configured = registries[0]
    require(
        isinstance(configured, dict)
        and set(configured) <= {"server", "identity", "passwordSecretRef", "username"}
        and isinstance(configured.get("server"), str)
        and configured["server"].casefold() == LOGIN_SERVER.casefold()
        and casefold_id(configured.get("identity")) == ACR_PULL_ID.casefold(),
        "registry_contract_invalid",
    )
    require(
        configured.get("passwordSecretRef", "") == "" and configured.get("username", "") == "",
        "registry_contract_invalid",
    )


def validate_snapshot(snapshot: dict[str, object], *, require_stable: bool) -> None:
    require(
        set(snapshot)
        == {
            "id",
            "name",
            "type",
            "location",
            "tagFields",
            "azdServiceName",
            "identity",
            "properties",
        }
        and snapshot.get("name") == APP
        and casefold_id(snapshot.get("id")) == APP_ID.casefold()
        and casefold_id(snapshot.get("type")) == "microsoft.app/containerapps"
        and isinstance(snapshot.get("location"), str)
        and bool(snapshot["location"])
        and snapshot.get("azdServiceName") == "web-nat"
        and isinstance(snapshot.get("tagFields"), list)
        and "azd-service-name" in snapshot["tagFields"],
        "app_identity_invalid",
    )
    properties = snapshot.get("properties")
    require(
        isinstance(properties, dict)
        and set(properties)
        == {
            "provisioningState",
            "runningStatus",
            "latestRevisionName",
            "latestReadyRevisionName",
            "latestRevisionFqdn",
            "environmentId",
            "managedEnvironmentId",
            "workloadProfileName",
            "configuration",
            "template",
            "revisionState",
        },
        "app_metadata_invalid",
    )
    configuration = validate_configuration(properties["configuration"])
    require(configuration.get("activeRevisionsMode") == "Single", "app_contract_invalid")
    ingress = configuration.get("ingress")
    require(
        isinstance(ingress, dict)
        and ingress.get("traffic") == [{"latestRevision": True, "weight": 100}],
        "traffic_contract_invalid",
    )
    template = properties["template"]
    require(
        isinstance(template, dict) and set(template) <= TEMPLATE_FIELDS,
        "revision_template_invalid",
    )
    containers = template.get("containers")
    require(isinstance(containers, list) and len(containers) == 1, "container_count_invalid")
    web = containers[0]
    expected_image_prefix = f"{LOGIN_SERVER}/{REPOSITORY}".casefold()
    require(
        isinstance(web, dict)
        and web.get("name") == "web"
        and isinstance(web.get("image"), str)
        and web["image"]
        .casefold()
        .startswith((f"{expected_image_prefix}:", f"{expected_image_prefix}@")),
        "web_container_invalid",
    )
    revision_state = properties["revisionState"]
    require(
        isinstance(revision_state, dict)
        and set(revision_state)
        == {
            "name",
            "active",
            "healthState",
            "provisioningState",
            "replicas",
            "runningState",
        }
        and revision_state["name"] == properties["latestRevisionName"],
        "revision_metadata_invalid",
    )
    if require_stable:
        require(
            properties["provisioningState"] == "Succeeded"
            and properties["runningStatus"] in {"Running", "Ready"}
            and properties["latestRevisionName"] == properties["latestReadyRevisionName"]
            and revision_state["active"] is True
            and revision_state["healthState"] == "Healthy"
            and revision_state["provisioningState"] == "Provisioned"
            and isinstance(revision_state["replicas"], int)
            and revision_state["replicas"] >= 1
            and revision_state["runningState"] == "Running",
            "app_not_stable",
        )


def strip_read_only_container_fields(containers: object) -> list[object]:
    require(isinstance(containers, list), "container_count_invalid")
    result = copy.deepcopy(containers)
    for container in result:
        require(isinstance(container, dict), "revision_template_invalid")
        resources = container.get("resources")
        if isinstance(resources, dict):
            resources.pop("ephemeralStorage", None)
    return result


def desired_containers(snapshot: dict[str, object]) -> list[object]:
    properties = snapshot["properties"]
    require(isinstance(properties, dict), "app_metadata_invalid")
    template = properties["template"]
    require(isinstance(template, dict), "revision_template_invalid")
    containers = strip_read_only_container_fields(template["containers"])
    require(len(containers) == 1 and isinstance(containers[0], dict), "container_count_invalid")
    containers[0]["image"] = TARGET_IMAGE
    return containers


def patch_payload(snapshot: dict[str, object]) -> dict[str, object]:
    return {
        "location": snapshot["location"],
        "properties": {"template": {"containers": desired_containers(snapshot)}},
    }


def rest_patch(token: str, payload: dict[str, object]) -> None:
    arm_request(
        token,
        method="PATCH",
        url=APP_URL,
        payload=payload,
        read_response=False,
    )


def normalized_template(template: dict[str, object]) -> dict[str, object]:
    result = copy.deepcopy(template)
    containers = strip_read_only_container_fields(result.get("containers"))
    require(len(containers) == 1 and isinstance(containers[0], dict), "container_count_invalid")
    containers[0]["image"] = "<reviewed-image>"
    result["containers"] = containers
    if isinstance(result.get("initContainers"), list):
        result["initContainers"] = strip_read_only_container_fields(result["initContainers"])
    else:
        require(result.get("initContainers") is None, "revision_template_invalid")
    return result


def preserved_contract(snapshot: dict[str, object]) -> dict[str, object]:
    properties = snapshot["properties"]
    require(isinstance(properties, dict), "app_metadata_invalid")
    template = properties["template"]
    require(isinstance(template, dict), "revision_template_invalid")
    return {
        "id": snapshot["id"],
        "name": snapshot["name"],
        "type": snapshot["type"],
        "location": snapshot["location"],
        "tagFields": copy.deepcopy(snapshot["tagFields"]),
        "azdServiceName": snapshot["azdServiceName"],
        "identity": copy.deepcopy(snapshot["identity"]),
        "properties": {
            "environmentId": properties["environmentId"],
            "managedEnvironmentId": properties["managedEnvironmentId"],
            "workloadProfileName": properties["workloadProfileName"],
            "configuration": copy.deepcopy(properties["configuration"]),
            "template": normalized_template(template),
        },
    }


def reviewed_fingerprint(snapshot: dict[str, object], registry_resource: dict[str, object]) -> str:
    return canonical_hash({"app": snapshot, "registry": registry_resource})


def report(snapshot: dict[str, object], registry_resource: dict[str, object]) -> dict[str, object]:
    properties = snapshot["properties"]
    require(isinstance(properties, dict), "app_metadata_invalid")
    template = properties["template"]
    require(isinstance(template, dict), "revision_template_invalid")
    containers = template["containers"]
    require(
        isinstance(containers, list) and isinstance(containers[0], dict),
        "web_container_invalid",
    )
    return {
        "schema": 3,
        "status": "ready",
        "target": TARGET_IMAGE,
        "current_reference": containers[0]["image"],
        "current_revision": properties["latestRevisionName"],
        "baseline_fingerprint": reviewed_fingerprint(snapshot, registry_resource),
        "preservation_hash": canonical_hash(preserved_contract(snapshot)),
        "configuration_metadata_hash": canonical_hash(properties["configuration"]),
        "registry_metadata_hash": canonical_hash(registry_resource),
        "blast_radius": {
            "resource": APP_ID,
            "operation": "2025-01-01 JSON Merge PATCH of location and template.containers",
            "expected_change": "one web container image and one new application revision",
            "configuration_transmitted": False,
            "provision": False,
            "registry_write": False,
            "secret_read": False,
        },
    }


def healthz(snapshot: dict[str, object]) -> bool:
    properties = snapshot["properties"]
    require(isinstance(properties, dict), "app_metadata_invalid")
    configuration = properties["configuration"]
    require(isinstance(configuration, dict), "configuration_schema_invalid")
    ingress = configuration.get("ingress")
    require(isinstance(ingress, dict), "health_endpoint_invalid")
    fqdn = ingress.get("fqdn")
    require(
        isinstance(fqdn, str)
        and re.fullmatch(r"fcag-dev-app\.[a-z0-9.-]+\.azurecontainerapps\.io", fqdn) is not None,
        "health_endpoint_invalid",
    )
    url = f"https://{fqdn}/healthz"
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        response = open_url(request, 15)
    except (OSError, TimeoutError, urllib.error.HTTPError, urllib.error.URLError):
        return False
    try:
        return (
            getattr(response, "status", response.getcode()) == 200
            and getattr(response, "geturl", lambda: url)() == url
        )
    finally:
        response.close()


def rollout_revision_ready(revision: dict[str, object]) -> bool:
    properties = revision["properties"]
    require(isinstance(properties, dict), "revision_metadata_invalid")
    revision_provisioning = properties.get("provisioningState")
    revision_running = properties.get("runningState")
    revision_health = properties.get("healthState")
    if revision_provisioning in {"Failed", "Deprovisioning", "Deprovisioned"}:
        raise PreflightError("rollout_terminal_failure")
    require(
        revision_provisioning in {"Provisioning", "Provisioned"},
        "rollout_state_invalid",
    )
    if revision_running in {"Stopped", "Degraded", "Failed"}:
        raise PreflightError("rollout_terminal_failure")
    require(
        revision_running in {"Processing", "Running", "Unknown"},
        "rollout_state_invalid",
    )
    if revision_health == "Unhealthy":
        raise PreflightError("rollout_terminal_failure")
    require(revision_health in {"Healthy", "None"}, "rollout_state_invalid")
    replicas = properties.get("replicas")
    require(
        isinstance(replicas, int) and not isinstance(replicas, bool) and replicas >= 0,
        "rollout_state_invalid",
    )
    return (
        properties.get("active") is True
        and revision_provisioning == "Provisioned"
        and revision_running == "Running"
        and revision_health == "Healthy"
        and replicas >= 1
    )


def wait_for_rollout(
    token: str,
    before: dict[str, object],
    *,
    timeout_seconds: int = ROLLOUT_TIMEOUT_SECONDS,
    poll_seconds: int = POLL_SECONDS,
) -> dict[str, object]:
    before_properties = before["properties"]
    require(isinstance(before_properties, dict), "app_metadata_invalid")
    before_revision = before_properties["latestRevisionName"]
    require(isinstance(before_revision, str), "revision_metadata_invalid")
    before_direct = read_revision(token, before_revision)
    before_created_time = revision_created_time(before_direct)
    expected_preservation = canonical_hash(preserved_contract(before))
    expected_configuration = canonical_hash(before_properties["configuration"])
    before_template = before_properties["template"]
    require(isinstance(before_template, dict), "revision_template_invalid")
    before_containers = before_template["containers"]
    require(
        isinstance(before_containers, list) and isinstance(before_containers[0], dict),
        "web_container_invalid",
    )
    expected_template = canonical_hash(normalized_template(before_template))
    deadline = time.monotonic() + timeout_seconds
    candidate_revision: str | None = None
    while time.monotonic() < deadline:
        revisions = list_revisions(token)
        newer = [
            revision
            for revision in revisions
            if revision["name"] != before_revision
            and revision_created_time(revision) > before_created_time
        ]
        if not newer:
            current = next(
                (revision for revision in revisions if revision["name"] == before_revision),
                None,
            )
            require(current is not None, "revision_not_available")
            require(revision_is_active_healthy(current), "rollout_state_invalid")
            time.sleep(poll_seconds)
            continue
        require(len(newer) == 1, "unexpected_revision_churn")
        candidate = read_revision(token, newer[0]["name"])
        if candidate_revision is None:
            candidate_revision = candidate["name"]
        require(candidate["name"] == candidate_revision, "unexpected_revision_churn")
        candidate_properties = candidate["properties"]
        require(isinstance(candidate_properties, dict), "revision_metadata_invalid")
        candidate_template = candidate_properties.get("template")
        require(isinstance(candidate_template, dict), "revision_template_invalid")
        candidate_containers = candidate_template.get("containers")
        require(
            isinstance(candidate_containers, list)
            and isinstance(candidate_containers[0], dict)
            and candidate_containers[0].get("image") == TARGET_IMAGE,
            "rollout_image_drift",
        )
        require(
            canonical_hash(normalized_template(candidate_template)) == expected_template,
            "preservation_drift",
        )
        if rollout_revision_ready(candidate):
            require(
                [
                    revision["name"]
                    for revision in revisions
                    if isinstance(revision["properties"], dict)
                    and revision["properties"].get("active") is True
                ]
                == [candidate_revision]
                and not any(
                    revision["name"] != candidate_revision and revision_is_in_progress(revision)
                    for revision in revisions
                ),
                "active_revision_contract_invalid",
            )
            after = safe_snapshot(token)
            after_properties = after["properties"]
            require(isinstance(after_properties, dict), "app_metadata_invalid")
            require(
                after_properties["latestRevisionName"] == candidate_revision
                and canonical_hash(preserved_contract(after)) == expected_preservation
                and canonical_hash(after_properties["configuration"]) == expected_configuration,
                "preservation_drift",
            )
            if healthz(after):
                return after
        time.sleep(poll_seconds)
    raise PreflightError("rollout_not_proven")


def preflight(
    token: str,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    manifest(TARGET_DIGEST)
    registry_resource = registry_metadata(token)
    snapshot = safe_snapshot(token)
    validate_snapshot(snapshot, require_stable=True)
    validate_registry_contract(snapshot, registry_resource)
    properties = snapshot["properties"]
    require(isinstance(properties, dict), "app_metadata_invalid")
    template = properties["template"]
    require(isinstance(template, dict), "revision_template_invalid")
    containers = template["containers"]
    require(
        isinstance(containers, list) and isinstance(containers[0], dict),
        "web_container_invalid",
    )
    require(containers[0]["image"] != TARGET_IMAGE, "target_image_already_current")
    return snapshot, registry_resource, report(snapshot, registry_resource)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("action", choices=("preview", "deploy"))
    value.add_argument("--subscription", required=True)
    value.add_argument("--expect-fingerprint")
    value.add_argument("--approve-change", action="store_true")
    value.add_argument("--reviewed", action="store_true")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        require(args.subscription == SUBSCRIPTION, "subscription_not_allowed")
        token = get_arm_token()
        baseline, baseline_registry, evidence = preflight(token)
        print(json.dumps(evidence, sort_keys=True), flush=True)
        if args.action == "preview" or not (args.approve_change and args.reviewed):
            print(
                "PLAN ONLY: no Azure mutation started. Re-run web-pinned with "
                "--expect-fingerprint <baseline_fingerprint> --approve-change --reviewed."
            )
            return 0
        require(bool(args.expect_fingerprint), "reviewed_fingerprint_required")
        require(
            args.expect_fingerprint == evidence["baseline_fingerprint"],
            "reviewed_baseline_drift",
        )
        immediately_before_registry = registry_metadata(token)
        immediately_before = safe_snapshot(token)
        validate_snapshot(immediately_before, require_stable=True)
        validate_registry_contract(immediately_before, immediately_before_registry)
        require(
            reviewed_fingerprint(immediately_before, immediately_before_registry)
            == evidence["baseline_fingerprint"],
            "concurrent_drift_before_patch",
        )
        rest_patch(token, patch_payload(immediately_before))
        after = wait_for_rollout(token, immediately_before)
        after_registry = registry_metadata(token)
        validate_registry_contract(after, after_registry)
        require(
            canonical_hash(after_registry) == canonical_hash(baseline_registry),
            "registry_preservation_drift",
        )
        after_properties = after["properties"]
        require(isinstance(after_properties, dict), "app_metadata_invalid")
        print(
            json.dumps(
                {
                    "schema": 3,
                    "status": "succeeded",
                    "resource": APP_ID,
                    "revision": after_properties["latestRevisionName"],
                    "image": TARGET_IMAGE,
                    "healthz": 200,
                    "preservation_hash": canonical_hash(preserved_contract(after)),
                    "configuration_metadata_hash": canonical_hash(
                        after_properties["configuration"]
                    ),
                    "registry_metadata_hash": canonical_hash(after_registry),
                    "configuration_transmitted": False,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 0
    except PreflightError as exc:
        print(json.dumps({"schema": 3, "status": "blocked", "reason": str(exc)}))
        return 1
    except (KeyError, TypeError, ValueError):
        print(json.dumps({"schema": 3, "status": "blocked", "reason": "unexpected_response_shape"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
