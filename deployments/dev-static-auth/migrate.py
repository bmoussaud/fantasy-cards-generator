"""Guarded dev-only migration from Key Vault runtime reads to ACA native secrets."""

import argparse
import copy
import datetime
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SUBSCRIPTION = "b8ff3e15-7e2d-4fac-a773-992fb59ccedd"
RESOURCE_GROUP = "rg-fcag-dev"
APP_NAME = "fcag-dev-app"
VAULT_NAME = "kvfcagdevqhg3qc4rlbt4g"
REGISTRY_NAME = "fcagdevqhg3qc4rlbt4gacr"
REGISTRY_SERVER = f"{REGISTRY_NAME}.azurecr.io"
IMAGE_REPOSITORY = "fantasy-cards-generator/web-nat-dev"
PRINCIPAL_ID = "946d8701-48f2-4fa5-8efd-bf053c7b4e4c"
GROUP = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{RESOURCE_GROUP}"
APP = f"{GROUP}/providers/Microsoft.App/containerApps/{APP_NAME}"
VAULT = f"{GROUP}/providers/Microsoft.KeyVault/vaults/{VAULT_NAME}"
ROLE_DEFINITION = (
    f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Authorization/"
    "roleDefinitions/4633458b-17de-408a-b874-0445c86b69e6"
)
APP_API = "2025-01-01"
VAULT_API = "2023-07-01"
DEPLOY_API = "2025-04-01"
HERE = Path(__file__).resolve().parent
SOURCE_ROOT = HERE.parents[1]
SOURCE_PATHS = ("Dockerfile", "azure.yaml", "app", "pyproject.toml", "uv.lock")
RECEIPT_SCHEMA = "dev-static-auth/v2"
RECEIPT_NAME = "dev-static-auth-receipt-v2.json"
ENDPOINT_SPEC = importlib.util.spec_from_file_location(
    "dev_endpoint_guard", HERE.parent / "dev-endpoint" / "persist_endpoint.py"
)
ENDPOINT_GUARD = importlib.util.module_from_spec(ENDPOINT_SPEC)
ENDPOINT_SPEC.loader.exec_module(ENDPOINT_GUARD)
TARGET_SECRETS = {
    "APP_SESSION_SECRET_KEY": "app-session-secret-key",
    "ENTRA_CLIENT_SECRET": "entra-client-secret",
}
ROTATION_ONLY_ENV = {
    "SECRET_PROVIDER_BACKEND",
    "SECRET_PROVIDER_CACHE_TTL_SECONDS",
    "SECRET_PROVIDER_REQUEST_TIMEOUT_SECONDS",
    "SECRET_PROVIDER_MAX_RETRIES",
    "SECRET_PROVIDER_RETRY_BACKOFF_SECONDS",
    "SECRET_PROVIDER_MAX_STALE_SECONDS",
}


class GateError(Exception):
    """A fixed non-sensitive deployment refusal."""


def require(condition, message):
    if not condition:
        raise GateError(message)


def command(args, payload=None):
    result = subprocess.run(
        args,
        input=None if payload is None else json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode:
        diagnostic = result.stderr + result.stdout
        code = next(
            (
                value
                for value in (
                    "AuthorizationFailed",
                    "ForbiddenByRbac",
                    "InvalidTemplate",
                    "InvalidTemplateDeployment",
                    "ContainerAppSecretInvalid",
                    "RoleAssignmentDoesNotExist",
                )
                if value in diagnostic
            ),
            "unclassified",
        )
        raise GateError(f"Azure/tool command failed: {code}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def local_command(args):
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise GateError("Local provenance command failed")
    return result.stdout.strip()


def azd_command(args, failure_code):
    try:
        result = subprocess.run(
            args,
            cwd=SOURCE_ROOT,
            capture_output=True,
            text=True,
            timeout=1200,
        )
    except subprocess.TimeoutExpired:
        raise GateError(f"Owned azd command failed: {failure_code}-timeout") from None
    if result.returncode:
        raise GateError(f"Owned azd command failed: {failure_code}")


def receipt_path():
    value = local_command(
        [
            "git",
            "-C",
            str(HERE),
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            RECEIPT_NAME,
        ]
    )
    path = Path(value)
    require(path.is_absolute(), "Receipt path is not private Git metadata")
    return path


def receipt_digest(receipt):
    return fingerprint(receipt)


def load_receipt(required=True):
    path = receipt_path()
    if not path.exists():
        require(not required, "Migration receipt is required")
        return None
    try:
        receipt = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        raise GateError("Migration receipt is invalid") from None
    require(
        receipt.get("schema") == RECEIPT_SCHEMA
        and receipt.get("subscription") == SUBSCRIPTION
        and receipt.get("resourceGroup") == RESOURCE_GROUP
        and receipt.get("app") == APP
        and receipt.get("vault") == VAULT
        and re.fullmatch(r"[0-9a-f]{40}", receipt.get("reviewedSource", "")),
        "Migration receipt scope is invalid",
    )
    baseline = receipt.get("vaultBaseline")
    require(
        isinstance(baseline, dict) and receipt.get("vaultBaselineHash") == fingerprint(baseline),
        "Migration receipt baseline hash is invalid",
    )
    return receipt


def save_receipt(receipt):
    path = receipt_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
        temporary.chmod(0o600)
        temporary.replace(path)
    except OSError:
        raise GateError("Migration receipt could not be persisted") from None


def validate_source(source):
    require(re.fullmatch(r"[0-9a-f]{40}", source or ""), "Rollback source must be a full SHA")
    require(
        not source.startswith("f9081f3"),
        "Rollback source must be the reviewed new rollback commit",
    )
    head = local_command(["git", "-C", str(SOURCE_ROOT), "rev-parse", "HEAD"])
    require(head == source, "Rollback source is not the checked-out commit")
    dirty = local_command(
        [
            "git",
            "-C",
            str(SOURCE_ROOT),
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            *SOURCE_PATHS,
        ]
    )
    require(not dirty, "Reviewed deploy source context is not clean")
    return source_fingerprint(source)


def source_fingerprint(source):
    tree = local_command(
        [
            "git",
            "-C",
            str(SOURCE_ROOT),
            "ls-tree",
            "-r",
            "--full-tree",
            source,
            "--",
            *SOURCE_PATHS,
        ]
    )
    require(tree, "Reviewed deploy source context is empty")
    return hashlib.sha256(tree.encode()).hexdigest()


def validate_image(image):
    pattern = rf"{re.escape(REGISTRY_SERVER)}/{re.escape(IMAGE_REPOSITORY)}" r"@sha256:[0-9a-f]{64}"
    require(re.fullmatch(pattern, image or ""), "Rollback image must be the approved full digest")
    return image.rsplit("@", 1)[1]


def rest(method, path, payload=None):
    require(path.startswith(GROUP), "Unexpected ARM request scope")
    args = [
        "az",
        "rest",
        "--method",
        method,
        "--url",
        f"https://management.azure.com{path}",
        "--output",
        "json",
    ]
    if payload is not None:
        args += ["--body", "@/dev/stdin"]
    return command(args, payload)


def get_app():
    return rest("get", f"{APP}?api-version={APP_API}")


def get_vault():
    return rest("get", f"{VAULT}?api-version={VAULT_API}")


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def validate_app(raw):
    try:
        guarded_snapshot = ENDPOINT_GUARD.snapshot(raw)
    except ENDPOINT_GUARD.GateError as error:
        raise GateError(str(error)) from None
    require(raw.get("id", "").lower() == APP.lower(), "Unexpected app ownership")
    require(raw.get("name") == APP_NAME, "Unexpected app name")
    require(raw.get("identity", {}).get("principalId") == PRINCIPAL_ID, "Unexpected app principal")
    properties = raw.get("properties", {})
    require(
        properties.get("provisioningState") == "Succeeded"
        and properties.get("runningStatus") == "Running"
        and properties.get("latestRevisionName") == properties.get("latestReadyRevisionName"),
        "Existing app is not healthy",
    )
    environment = (GROUP + "/providers/Microsoft.App/managedEnvironments/fcag-dev-cae").lower()
    require(
        properties.get("managedEnvironmentId", "").lower() == environment
        and properties.get("environmentId", "").lower() == environment,
        "Unexpected managed environment",
    )
    configuration = properties.get("configuration", {})
    require(configuration.get("activeRevisionsMode") == "Single", "Unexpected revision mode")
    require(
        configuration.get("ingress", {}).get("traffic")
        == [{"latestRevision": True, "weight": 100}],
        "Unexpected traffic policy",
    )
    containers = properties.get("template", {}).get("containers")
    require(isinstance(containers, list), "Missing containers")
    require([item.get("name") for item in containers].count("web") == 1, "Ambiguous web container")
    for container in containers + (properties.get("template", {}).get("initContainers") or []):
        names = [item.get("name") for item in container.get("env") or []]
        require(all(isinstance(name, str) and name for name in names), "Malformed environment")
        require(len(names) == len(set(names)), "Duplicate environment name")
    secrets = configuration.get("secrets")
    require(isinstance(secrets, list), "Missing secret inventory")
    names = [item.get("name") for item in secrets]
    require(all(isinstance(name, str) and name for name in names), "Malformed secret metadata")
    require(len(names) == len(set(names)), "Duplicate secret metadata")
    for secret in secrets:
        require(
            set(secret) <= {"name", "keyVaultUrl", "identity"} and "value" not in secret,
            "Unexpected secret metadata",
        )
    return guarded_snapshot


def app_snapshot(raw):
    return validate_app(raw)


def validate_vault_identity(raw):
    require(raw.get("id", "").lower() == VAULT.lower(), "Unexpected vault ownership")
    require(
        raw.get("name") == VAULT_NAME
        and raw.get("location", "").lower().replace(" ", "") == "eastus2",
        "Unexpected vault metadata",
    )
    properties = raw.get("properties", {})
    require(
        properties.get("tenantId") == "31b6a5c6-8762-4d6b-bf6e-f37931c67a75",
        "Unexpected vault tenant",
    )
    require(
        properties.get("sku") == {"family": "A", "name": "standard"}
        and properties.get("enablePurgeProtection") is True
        and properties.get("enableRbacAuthorization") is True
        and properties.get("publicNetworkAccess") == "Disabled",
        "Unexpected immutable vault posture",
    )
    require(properties.get("softDeleteRetentionInDays", 90) == 90, "Unexpected retention")


def validate_vault(raw):
    validate_vault_identity(raw)
    properties = raw.get("properties", {})
    acls = properties.get("networkAcls")
    require(
        acls is None
        or (
            isinstance(acls, dict)
            and set(acls) <= {"bypass", "defaultAction", "ipRules", "virtualNetworkRules"}
            and acls.get("bypass") in (None, "None")
            and acls.get("defaultAction") in (None, "Deny")
            and isinstance(acls.get("ipRules", []), list)
            and isinstance(acls.get("virtualNetworkRules", []), list)
        ),
        "Vault ACL differs from approved baseline",
    )
    require(
        properties.get("enabledForTemplateDeployment") in (None, False),
        "Vault template access is already outside the approved baseline",
    )


def vault_snapshot(raw):
    validate_vault(raw)
    return project_vault_snapshot(raw)


def project_vault_snapshot(raw):
    writable = {
        "tenantId",
        "sku",
        "accessPolicies",
        "enabledForDeployment",
        "enabledForDiskEncryption",
        "enabledForTemplateDeployment",
        "enableSoftDelete",
        "softDeleteRetentionInDays",
        "enableRbacAuthorization",
        "enablePurgeProtection",
        "createMode",
        "networkAcls",
        "publicNetworkAccess",
    }
    read_only = {"vaultUri", "provisioningState", "privateEndpointConnections"}
    require(set(raw["properties"]) <= writable | read_only, "Unknown vault field")
    return {
        "location": raw["location"],
        "tags": raw.get("tags") or {},
        "properties": {
            key: copy.deepcopy(value) for key, value in raw["properties"].items() if key in writable
        },
    }


def expected_transfer_vault(baseline):
    expected = copy.deepcopy(baseline)
    properties = expected["properties"]
    prior = properties.get("networkAcls")
    transfer = copy.deepcopy(prior) if isinstance(prior, dict) else {}
    transfer.update(
        {
            "bypass": "AzureServices",
            "defaultAction": "Deny",
            "ipRules": copy.deepcopy((prior or {}).get("ipRules", [])),
            "virtualNetworkRules": copy.deepcopy((prior or {}).get("virtualNetworkRules", [])),
        }
    )
    properties["enabledForTemplateDeployment"] = True
    properties["networkAcls"] = transfer
    return expected


def validate_transfer_vault(raw, baseline=None):
    validate_vault_identity(raw)
    if baseline is None:
        properties = raw.get("properties", {})
        acls = properties.get("networkAcls") or {}
        require(
            properties.get("enabledForTemplateDeployment") is True
            and properties.get("publicNetworkAccess") == "Disabled"
            and acls.get("bypass") == "AzureServices"
            and acls.get("defaultAction") == "Deny",
            "Vault is not in the temporary transfer posture",
        )
        return
    require(
        project_vault_snapshot(raw) == expected_transfer_vault(baseline),
        "Vault is not in the exact receipt-bound transfer posture",
    )


def validate_restored_vault(raw, baseline):
    validate_vault_identity(raw)
    require(
        project_vault_snapshot(raw) == baseline,
        "Vault does not match the recorded original baseline",
    )


def baseline_receipt(raw, source):
    snapshot = vault_snapshot(raw)
    return {
        "schema": RECEIPT_SCHEMA,
        "subscription": SUBSCRIPTION,
        "resourceGroup": RESOURCE_GROUP,
        "app": APP,
        "vault": VAULT,
        "reviewedSource": source,
        "vaultBaseline": snapshot,
        "vaultBaselineHash": fingerprint(snapshot),
    }


def desired(snapshot, phase, suffix, image=None):
    result = copy.deepcopy(snapshot)
    result["properties"]["template"]["revisionSuffix"] = suffix
    web = next(
        item for item in result["properties"]["template"]["containers"] if item["name"] == "web"
    )
    env = [
        item
        for item in web.get("env") or []
        if item["name"] not in TARGET_SECRETS
        and (phase != "cleanup-app" or item["name"] not in ROTATION_ONLY_ENV)
    ]
    env.extend(
        {"name": env_name, "secretRef": secret_name}
        for env_name, secret_name in TARGET_SECRETS.items()
    )
    web["env"] = env
    if phase == "transfer":
        secrets = [
            item
            for item in result["properties"]["configuration"]["secrets"]
            if item["name"] not in TARGET_SECRETS.values()
        ]
        secrets.extend({"name": name} for name in TARGET_SECRETS.values())
        result["properties"]["configuration"]["secrets"] = secrets
    if phase == "deploy-image":
        require(image is not None, "Approved rollback image is required")
        web["image"] = image
    return result


def require_static_configuration(raw):
    secret_names = {item["name"] for item in raw["properties"]["configuration"]["secrets"]}
    require(
        set(TARGET_SECRETS.values()) <= secret_names,
        "Static ACA secrets are not installed",
    )
    web = next(
        item for item in raw["properties"]["template"]["containers"] if item["name"] == "web"
    )
    env = {item["name"]: item for item in web["env"]}
    for env_name, secret_name in TARGET_SECRETS.items():
        require(
            env.get(env_name) == {"name": env_name, "secretRef": secret_name},
            "Static auth environment references are not installed",
        )


def web_image(raw):
    return next(
        item for item in raw["properties"]["template"]["containers"] if item["name"] == "web"
    )["image"]


def require_approved_rollback(raw, receipt, image, source):
    require_static_configuration(raw)
    validate_image(image)
    validate_source(source)
    artifact = require_artifact(receipt, source, image)
    require(
        receipt.get("reviewedSource") == source,
        "Rollback provenance receipt does not match",
    )
    properties = raw["properties"]
    require(
        web_image(raw) == image
        and properties["latestRevisionName"] == properties["latestReadyRevisionName"]
        and artifact.get("pinnedRevision") == properties["latestRevisionName"],
        "Healthy latest revision is not the approved rollback digest",
    )


def cleanup_app_intent(receipt, raw, expected, suffix, image, source):
    artifact = require_artifact(receipt, source, image)
    revision = f"{APP_NAME}--{suffix}"
    require(
        artifact.get("pinnedRevision") == raw["properties"]["latestRevisionName"],
        "Cleanup intent does not start from the approved pinned revision",
    )
    return {
        "status": "started",
        "source": source,
        "sourceFingerprint": artifact["sourceFingerprint"],
        "image": image,
        "priorPinnedRevision": artifact["pinnedRevision"],
        "baselineAppFingerprint": fingerprint(raw),
        "expectedSnapshotFingerprint": fingerprint(expected),
        "expectedRevision": revision,
    }


def require_cleanup_app_intent(receipt, raw, image, source):
    artifact = require_artifact(receipt, source, image)
    intent = artifact.get("cleanupAppIntent")
    require(
        isinstance(intent, dict)
        and intent.get("status") == "started"
        and intent.get("source") == source
        and intent.get("sourceFingerprint") == artifact.get("sourceFingerprint")
        and intent.get("image") == image
        and intent.get("priorPinnedRevision") == artifact.get("pinnedRevision")
        and re.fullmatch(
            rf"{re.escape(APP_NAME)}--static-cleanup-[0-9a-f]{{8}}",
            intent.get("expectedRevision", ""),
        )
        and re.fullmatch(r"[0-9a-f]{64}", intent.get("baselineAppFingerprint", ""))
        and re.fullmatch(r"[0-9a-f]{64}", intent.get("expectedSnapshotFingerprint", "")),
        "Cleanup receipt intent is missing or invalid",
    )
    require(
        raw["properties"]["latestRevisionName"] == intent["expectedRevision"]
        and raw["properties"]["latestReadyRevisionName"] == intent["expectedRevision"]
        and web_image(raw) == image
        and fingerprint(app_snapshot(raw)) == intent["expectedSnapshotFingerprint"]
        and not rotation_environment_present(raw),
        "Current app does not match the exact recorded cleanup intent",
    )
    return intent


def promote_cleanup_revision(receipt, raw, image, source):
    intent = require_cleanup_app_intent(receipt, raw, image, source)
    require(healthz(raw), "Cleanup revision health check failed")
    verify_auth_surface(raw)
    fresh = get_app()
    require(
        fingerprint(fresh) == fingerprint(raw),
        "Cleanup app changed during final verification",
    )
    require_cleanup_app_intent(receipt, fresh, image, source)
    artifact = copy.deepcopy(receipt["artifact"])
    completed = copy.deepcopy(intent)
    completed["status"] = "succeeded"
    completed["completedAt"] = utc_now().isoformat()
    artifact["pinnedRevision"] = intent["expectedRevision"]
    artifact["cleanupApp"] = completed
    artifact.pop("cleanupAppIntent")
    updated = copy.deepcopy(receipt)
    updated["artifact"] = artifact
    return updated


def rotation_environment_present(raw):
    web = next(
        item for item in raw["properties"]["template"]["containers"] if item["name"] == "web"
    )
    return bool({item["name"] for item in web["env"]} & ROTATION_ONLY_ENV)


def secret_versions():
    versions = {}
    for secret_name in TARGET_SECRETS.values():
        metadata = rest(
            "get",
            f"{VAULT}/secrets/{secret_name}?api-version={VAULT_API}",
        )
        uri = metadata.get("properties", {}).get("secretUriWithVersion", "")
        match = re.fullmatch(
            rf"https://{VAULT_NAME}\.vault\.azure\.net/secrets/{secret_name}/([0-9a-f]+)",
            uri,
        )
        require(match is not None, "Secret version metadata unavailable")
        versions[secret_name] = match.group(1)
    return versions


def build(file_name):
    return command(["az", "bicep", "build", "--file", str(HERE / "infra" / file_name), "--stdout"])


def deployment_body(template, parameters):
    return {
        "properties": {
            "mode": "Incremental",
            "debugSetting": {"detailLevel": "none"},
            "template": template,
            "parameters": parameters,
        }
    }


def parameter_reference(secret_name, version):
    return {
        "reference": {
            "keyVault": {"id": VAULT},
            "secretName": secret_name,
            "secretVersion": version,
        }
    }


def preview(name, template, parameters, allowed_resources):
    scope_template = copy.deepcopy(template)
    for parameter_name, parameter in parameters.items():
        if "value" in parameter:
            scope_template["parameters"][parameter_name]["defaultValue"] = parameter["value"]
        elif parameter_name in {"appSessionSecretKey", "entraClientSecret"}:
            scope_template["parameters"][parameter_name]["defaultValue"] = "preview-redacted"
    plan = command(
        [
            "az",
            "deployment",
            "group",
            "what-if",
            "--subscription",
            SUBSCRIPTION,
            "--resource-group",
            RESOURCE_GROUP,
            "--name",
            name,
            "--mode",
            "Incremental",
            "--template-file",
            "/dev/stdin",
            "--result-format",
            "ResourceIdOnly",
            "--no-pretty-print",
            "--output",
            "json",
        ],
        scope_template,
    )
    require(plan.get("status") == "Succeeded", "What-if did not succeed")
    require(
        not any(plan.get(key) for key in ("error", "diagnostics", "potentialChanges")),
        "What-if contains diagnostics",
    )
    changes = plan.get("changes")
    require(isinstance(changes, list), "What-if lacks changes")
    if isinstance(allowed_resources, set):
        allowed_resources = {resource_id: {"Deploy", "Modify"} for resource_id in allowed_resources}
    for change in changes:
        resource_id = change.get("resourceId", "").lower()
        if change.get("changeType") == "Ignore":
            require(
                resource_id.startswith(GROUP.lower() + "/providers/"), "Foreign ignored resource"
            )
            continue
        matching_id = next(
            (item for item in allowed_resources if item.lower() == resource_id),
            None,
        )
        require(matching_id is not None, "Foreign what-if resource")
        require(
            change.get("changeType") in allowed_resources[matching_id],
            "Unexpected change type",
        )
        require(
            all(
                change.get(key) is None for key in ("before", "after", "delta", "unsupportedReason")
            ),
            "What-if exposed payload data",
        )
    require(
        any(
            item.get("resourceId", "").lower() in {value.lower() for value in allowed_resources}
            and item.get("changeType") != "Ignore"
            for item in changes
        ),
        "What-if contains no bounded change",
    )
    return {
        "changes": sorted(
            {item["resourceId"] for item in changes if item.get("changeType") != "Ignore"}
        )
    }


def deploy(name, body):
    path = f"{GROUP}/providers/Microsoft.Resources/deployments/{name}?api-version={DEPLOY_API}"
    rest("put", path, body)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        result = rest("get", path)
        state = result.get("properties", {}).get("provisioningState")
        require(state in {"Accepted", "Running", "Succeeded"}, "Deployment failed")
        if state == "Succeeded":
            return
        time.sleep(5)
    raise GateError("Deployment verification timed out")


def healthz(raw):
    fqdn = raw["properties"]["configuration"]["ingress"]["fqdn"]
    require(
        re.fullmatch(r"fcag-dev-app\.[a-z0-9.-]+\.azurecontainerapps\.io", fqdn),
        "Unexpected health endpoint",
    )
    try:
        with urllib.request.urlopen(f"https://{fqdn}/healthz", timeout=15) as response:
            return response.status == 200
    except (OSError, urllib.error.HTTPError):
        return False


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def request_status(opener, url):
    try:
        response = opener.open(url, timeout=15)
        return response.status, response.headers
    except urllib.error.HTTPError as error:
        return error.code, error.headers


def verify_auth_surface(raw):
    web = next(
        item for item in raw["properties"]["template"]["containers"] if item["name"] == "web"
    )
    settings = {
        item["name"]: item.get("value")
        for item in web["env"]
        if item["name"] in {"ENTRA_CLIENT_ID", "ENTRA_REDIRECT_URI"}
    }
    require(
        set(settings) == {"ENTRA_CLIENT_ID", "ENTRA_REDIRECT_URI"} and all(settings.values()),
        "Auth configuration metadata is incomplete",
    )
    fqdn = raw["properties"]["configuration"]["ingress"]["fqdn"]
    base = f"https://{fqdn}"
    opener = urllib.request.build_opener(
        NoRedirect(),
        urllib.request.HTTPCookieProcessor(),
    )
    status, headers = request_status(opener, f"{base}/auth/login")
    require(status in {302, 307}, "Auth login did not redirect")
    location = headers.get("Location", "")
    parsed = urllib.parse.urlparse(location)
    query = urllib.parse.parse_qs(parsed.query)
    require(
        parsed.scheme == "https"
        and parsed.hostname == "login.microsoftonline.com"
        and query.get("client_id") == [settings["ENTRA_CLIENT_ID"]]
        and query.get("redirect_uri") == [settings["ENTRA_REDIRECT_URI"]],
        "Auth redirect metadata mismatch",
    )
    cookie = headers.get("Set-Cookie", "").lower()
    require(
        "fantasy_cards_session=" in cookie
        and "httponly" in cookie
        and "secure" in cookie
        and "samesite=lax" in cookie,
        "Auth cookie attributes mismatch",
    )
    clean_opener = urllib.request.build_opener(NoRedirect())
    missing_status, _ = request_status(
        clean_opener,
        f"{base}/auth/callback?code=missing-session&state=missing-session",
    )
    require(missing_status == 400, "Missing callback state guard failed")
    invalid_status, _ = request_status(
        opener,
        f"{base}/auth/callback?code=invalid&state=definitely-not-the-issued-state",
    )
    require(invalid_status == 400, "Invalid callback state guard failed")


def verify_app_after(expected, suffix):
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        raw = get_app()
        try:
            snapshot = app_snapshot(raw)
        except GateError:
            time.sleep(5)
            continue
        if (
            snapshot == expected
            and raw["properties"]["latestRevisionName"] == f"{APP_NAME}--{suffix}"
            and raw["properties"]["latestReadyRevisionName"] == f"{APP_NAME}--{suffix}"
            and healthz(raw)
        ):
            return
        time.sleep(5)
    raise GateError("App health/config verification incomplete")


def role_assignments():
    assignments = command(
        [
            "az",
            "role",
            "assignment",
            "list",
            "--subscription",
            SUBSCRIPTION,
            "--scope",
            VAULT,
            "--assignee-object-id",
            PRINCIPAL_ID,
            "--query",
            "[].{id:id,principalId:principalId,roleDefinitionId:roleDefinitionId,scope:scope}",
            "--output",
            "json",
        ]
    )
    require(isinstance(assignments, list), "Role assignment inventory is invalid")
    matches = [
        item
        for item in assignments
        if item.get("principalId") == PRINCIPAL_ID
        and item.get("scope", "").lower() == VAULT.lower()
        and item.get("roleDefinitionId", "").lower() == ROLE_DEFINITION.lower()
    ]
    require(
        len(matches) <= 1
        and len(assignments) == len(matches)
        and all(
            set(item) == {"id", "principalId", "roleDefinitionId", "scope"}
            and isinstance(item.get("id"), str)
            for item in assignments
        ),
        "Runtime vault role assignment inventory is not exact",
    )
    return matches


def require_apply_gates(args):
    require(
        args.apply and args.approve_change and args.reviewed and args.expect_fingerprint,
        "Apply gates missing",
    )


def require_receipt_source(receipt, source):
    source_hash = validate_source(source)
    require(receipt["reviewedSource"] == source, "Reviewed rollback source changed")
    return source_hash


def run_access(args):
    validate_source(args.rollback_source)
    raw = get_vault()
    baseline = fingerprint(raw)
    receipt = load_receipt(required=False)
    if receipt is None:
        require(
            raw.get("properties", {}).get("enabledForTemplateDeployment") in (None, False),
            "Transfer access is enabled without a trusted original-state receipt",
        )
        receipt = baseline_receipt(raw, args.rollback_source)
        save_receipt(receipt)
    require_receipt_source(receipt, args.rollback_source)
    if args.expect_fingerprint:
        require(args.expect_fingerprint == baseline, "Reviewed vault fingerprint drifted")
    if raw.get("properties", {}).get("enabledForTemplateDeployment") is True:
        validate_transfer_vault(raw, receipt["vaultBaseline"])
        print(
            json.dumps(
                {
                    "phase": "access",
                    "fingerprint": baseline,
                    "receiptFingerprint": receipt_digest(receipt),
                    "applied": False,
                    "alreadyApplied": True,
                    "changes": [],
                }
            )
        )
        return
    current_snapshot = vault_snapshot(raw)
    require(
        current_snapshot == receipt["vaultBaseline"],
        "Vault baseline differs from the recorded original state",
    )
    template = build("vault-transfer-access.bicep")
    parameters = {
        "snapshot": {"value": receipt["vaultBaseline"]},
        "enableTransferAccess": {"value": True},
    }
    report = preview(
        "dev-static-auth-access-preview",
        template,
        parameters,
        {VAULT: {"Deploy", "Modify"}},
    )
    print(
        json.dumps(
            {
                "phase": "access",
                "fingerprint": baseline,
                "receiptFingerprint": receipt_digest(receipt),
                "applied": False,
                **report,
            }
        )
    )
    if not args.apply:
        return
    require_apply_gates(args)
    require_receipt_source(load_receipt(), args.rollback_source)
    require(fingerprint(get_vault()) == baseline, "Vault changed immediately before apply")
    require(
        receipt_digest(load_receipt()) == receipt_digest(receipt),
        "Migration receipt changed immediately before apply",
    )
    deploy("dev-static-auth-access", deployment_body(template, parameters))
    validate_transfer_vault(get_vault(), receipt["vaultBaseline"])


def app_phase_parameters(snapshot, phase, image=None, versions=None):
    parameters = {
        "snapshot": {"value": snapshot},
        "phase": {"value": phase},
        "rollbackImage": {"value": image or ""},
    }
    if phase == "transfer":
        require(versions is not None, "Secret versions are required for transfer")
        parameters.update(
            {
                "appSessionSecretKey": parameter_reference(
                    TARGET_SECRETS["APP_SESSION_SECRET_KEY"],
                    versions[TARGET_SECRETS["APP_SESSION_SECRET_KEY"]],
                ),
                "entraClientSecret": parameter_reference(
                    TARGET_SECRETS["ENTRA_CLIENT_SECRET"],
                    versions[TARGET_SECRETS["ENTRA_CLIENT_SECRET"]],
                ),
            }
        )
    return parameters


def run_transfer(args):
    receipt = load_receipt()
    require_receipt_source(receipt, args.rollback_source)
    raw_vault = get_vault()
    validate_transfer_vault(raw_vault, receipt["vaultBaseline"])
    raw = get_app()
    snapshot = app_snapshot(raw)
    versions = secret_versions()
    stage_state = transfer_stage_state(receipt, raw, raw_vault, versions)
    baseline = fingerprint(stage_state)
    if args.expect_fingerprint:
        require(args.expect_fingerprint == baseline, "Reviewed transfer fingerprint drifted")
    suffix = f"static-transfer-{baseline[:8]}"
    snapshot["properties"]["template"]["revisionSuffix"] = suffix
    expected = desired(snapshot, "transfer", suffix)
    template = build("main.bicep")
    parameters = app_phase_parameters(snapshot, "transfer", versions=versions)
    allowed = {
        APP: {"Deploy", "Modify"},
        f"{GROUP}/providers/Microsoft.Resources/deployments/dev-static-auth-app": {
            "Create",
            "Deploy",
            "Modify",
        },
    }
    report = preview("dev-static-auth-transfer-preview", template, parameters, allowed)
    print(
        json.dumps(
            {
                "phase": "transfer",
                "fingerprint": baseline,
                "receiptFingerprint": receipt_digest(receipt),
                "secretVersions": versions,
                "applied": False,
                **report,
            }
        )
    )
    if not args.apply:
        return
    require_apply_gates(args)
    fresh_receipt = load_receipt()
    require_receipt_source(fresh_receipt, args.rollback_source)
    fresh_vault = get_vault()
    validate_transfer_vault(fresh_vault, fresh_receipt["vaultBaseline"])
    fresh_app = get_app()
    fresh_versions = secret_versions()
    require(
        receipt_digest(fresh_receipt) == receipt_digest(receipt),
        "Migration receipt changed immediately before apply",
    )
    require(
        fingerprint(fresh_app) == fingerprint(raw),
        "App changed immediately before apply",
    )
    require(
        project_vault_snapshot(fresh_vault) == project_vault_snapshot(raw_vault),
        "Vault transfer posture changed immediately before apply",
    )
    require(fresh_versions == versions, "Secret versions changed immediately before apply")
    require(
        fingerprint(transfer_stage_state(fresh_receipt, fresh_app, fresh_vault, fresh_versions))
        == baseline,
        "Transfer state changed immediately before apply",
    )
    deploy(
        f"dev-static-auth-transfer-{baseline[:12]}",
        deployment_body(template, parameters),
    )
    verify_app_after(expected, suffix)


def transfer_stage_state(receipt, raw_app, raw_vault, versions):
    return {
        "receipt": receipt_digest(receipt),
        "app": fingerprint(raw_app),
        "vault": project_vault_snapshot(raw_vault),
        "secretVersions": versions,
    }


def published_tags():
    tags = command(
        [
            "az",
            "acr",
            "repository",
            "show-tags",
            "--name",
            REGISTRY_NAME,
            "--repository",
            IMAGE_REPOSITORY,
            "--detail",
            "--output",
            "json",
        ]
    )
    require(isinstance(tags, list), "Published tag inventory is invalid")
    result = {}
    for item in tags:
        name = item.get("name")
        if isinstance(name, str) and re.fullmatch(r"azd-deploy-[0-9]+", name):
            require(name not in result, "Published tag inventory is ambiguous")
            result[name] = {
                key: item.get(key) for key in ("digest", "createdTime", "lastUpdateTime")
            }
    return result


def parse_timestamp(value):
    require(isinstance(value, str) and value, "Published tag timestamp is unavailable")
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise GateError("Published tag timestamp is invalid") from None
    require(parsed.tzinfo is not None, "Published tag timestamp lacks timezone")
    return parsed.astimezone(datetime.timezone.utc)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def resolve_published_digest(tagged_image):
    prefix = f"{REGISTRY_SERVER}/{IMAGE_REPOSITORY}:"
    require(
        tagged_image.startswith(prefix)
        and re.fullmatch(r"azd-deploy-[0-9]+", tagged_image.removeprefix(prefix)),
        "Published image must be the actual azd deployment tag",
    )
    metadata = command(
        [
            "az",
            "acr",
            "manifest",
            "show-metadata",
            "--registry",
            REGISTRY_NAME,
            "--name",
            f"{IMAGE_REPOSITORY}:{tagged_image.removeprefix(prefix)}",
            "--query",
            "{digest:digest,tags:tags}",
            "--output",
            "json",
        ]
    )
    digest = metadata.get("digest", "")
    tags = metadata.get("tags")
    require(
        re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
        and (
            tags is None or (isinstance(tags, list) and tagged_image.removeprefix(prefix) in tags)
        ),
        "Published azd artifact metadata is not exact",
    )
    return digest


def deploy_stage_state(receipt, raw_app, raw_vault, source_hash, tags):
    return {
        "receipt": receipt_digest(receipt),
        "app": fingerprint(raw_app),
        "vault": project_vault_snapshot(raw_vault),
        "source": receipt["reviewedSource"],
        "sourceFingerprint": source_hash,
        "publishedTags": fingerprint(tags),
    }


def wait_for_owned_azd_revision(before, timeout=300, interval=5):
    baseline = app_snapshot(before)
    baseline_revision = before["properties"]["latestRevisionName"]
    baseline_image = web_image(before)
    expected_revision = None
    expected_tag = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = get_app()
        try:
            snapshot = ENDPOINT_GUARD.snapshot(raw)
        except ENDPOINT_GUARD.GateError as error:
            raise GateError(str(error)) from None
        require_static_configuration(raw)
        properties = raw["properties"]
        provisioning = properties.get("provisioningState")
        running = properties.get("runningStatus")
        require(
            provisioning != "Failed" and running != "Failed",
            "Published azd revision failed",
        )
        require(
            provisioning in {"InProgress", "Succeeded"} and running in {"Progressing", "Running"},
            "Published azd revision entered an unexpected state",
        )
        revision = properties.get("latestRevisionName")
        ready_revision = properties.get("latestReadyRevisionName")
        image = web_image(raw)
        if revision == baseline_revision:
            require(
                snapshot == baseline and image == baseline_image,
                "App configuration drifted while awaiting azd revision",
            )
        else:
            require(
                re.fullmatch(rf"{re.escape(APP_NAME)}--azd-[0-9]+", revision or ""),
                "Owned azd deploy published an unexpected revision",
            )
            prefix = f"{REGISTRY_SERVER}/{IMAGE_REPOSITORY}:"
            require(
                image.startswith(prefix)
                and re.fullmatch(r"azd-deploy-[0-9]+", image.removeprefix(prefix)),
                "Owned azd deploy published an unexpected image",
            )
            if expected_revision is None:
                expected_revision = revision
                expected_tag = image
            require(
                revision == expected_revision and image == expected_tag,
                "Published azd revision changed while awaiting readiness",
            )
            normalized = copy.deepcopy(snapshot)
            normalized["properties"]["template"]["revisionSuffix"] = baseline["properties"][
                "template"
            ]["revisionSuffix"]
            next(
                item
                for item in normalized["properties"]["template"]["containers"]
                if item["name"] == "web"
            )["image"] = baseline_image
            require(
                normalized == baseline,
                "App configuration drifted while awaiting azd revision",
            )
            if (
                provisioning == "Succeeded"
                and running == "Running"
                and ready_revision == revision
                and healthz(raw)
            ):
                return raw
        time.sleep(interval)
    raise GateError("Published azd revision readiness timed out")


def require_artifact(receipt, source, image):
    artifact = receipt.get("artifact")
    attempt = receipt.get("buildAttempt")
    current_source_hash = validate_source(source)
    require(
        isinstance(artifact, dict)
        and isinstance(attempt, dict)
        and attempt.get("status") == "succeeded"
        and attempt.get("source") == source
        and attempt.get("sourceFingerprint") == current_source_hash
        and attempt.get("sourceFingerprint") == artifact.get("sourceFingerprint")
        and attempt.get("publishedTag") == artifact.get("publishedTag")
        and attempt.get("publishedRevision") == artifact.get("publishedRevision")
        and attempt.get("image") == artifact.get("image")
        and attempt.get("packageCommand") == artifact.get("packageCommand")
        and attempt.get("deployCommand") == artifact.get("deployCommand")
        and artifact.get("source") == source
        and artifact.get("image") == image
        and artifact.get("packageCommand") == ["azd", "package", "web-nat", "--no-prompt"]
        and artifact.get("deployCommand")
        == ["azd", "deploy", "web-nat", "--no-prompt", "--timeout", "1200"],
        "Approved artifact provenance is not recorded by an owned build",
    )
    return artifact


def run_deploy_artifact(args):
    receipt = load_receipt()
    source_hash = require_receipt_source(receipt, args.rollback_source)
    raw_vault = get_vault()
    validate_transfer_vault(raw_vault, receipt["vaultBaseline"])
    raw = get_app()
    app_snapshot(raw)
    require_static_configuration(raw)
    tags_before = published_tags()
    state = deploy_stage_state(receipt, raw, raw_vault, source_hash, tags_before)
    baseline = fingerprint(state)
    if args.expect_fingerprint:
        require(args.expect_fingerprint == baseline, "Reviewed deploy fingerprint drifted")
    print(
        json.dumps(
            {
                "phase": "deploy",
                "fingerprint": baseline,
                "receiptFingerprint": receipt_digest(receipt),
                "source": args.rollback_source,
                "sourceFingerprint": source_hash,
                "applied": False,
                "changes": [APP],
            }
        )
    )
    if not args.apply:
        return
    require_apply_gates(args)
    fresh_receipt = load_receipt()
    fresh_source_hash = require_receipt_source(fresh_receipt, args.rollback_source)
    fresh_vault = get_vault()
    validate_transfer_vault(fresh_vault, fresh_receipt["vaultBaseline"])
    fresh_app = get_app()
    app_snapshot(fresh_app)
    require_static_configuration(fresh_app)
    fresh_tags = published_tags()
    require(
        fingerprint(
            deploy_stage_state(
                fresh_receipt,
                fresh_app,
                fresh_vault,
                fresh_source_hash,
                fresh_tags,
            )
        )
        == baseline,
        "Deploy state changed immediately before build",
    )
    started = utc_now()
    attempt = {
        "status": "started",
        "source": args.rollback_source,
        "sourceFingerprint": source_hash,
        "startedAt": started.isoformat(),
        "baselineRevision": fresh_app["properties"]["latestRevisionName"],
        "baselineImage": web_image(fresh_app),
        "baselineTagsFingerprint": fingerprint(fresh_tags),
        "packageCommand": ["azd", "package", "web-nat", "--no-prompt"],
        "deployCommand": [
            "azd",
            "deploy",
            "web-nat",
            "--no-prompt",
            "--timeout",
            "1200",
        ],
    }
    receipt.pop("artifact", None)
    receipt["buildAttempt"] = attempt
    save_receipt(receipt)
    azd_command(attempt["packageCommand"], "package")
    require(
        validate_source(args.rollback_source) == source_hash,
        "Reviewed source changed during package",
    )
    azd_command(attempt["deployCommand"], "deploy")
    require(
        validate_source(args.rollback_source) == source_hash,
        "Reviewed source changed during deploy",
    )
    after = wait_for_owned_azd_revision(fresh_app)
    require(
        validate_source(args.rollback_source) == source_hash,
        "Reviewed source changed while awaiting deploy readiness",
    )
    app_snapshot(after)
    require_static_configuration(after)
    published_tag = web_image(after)
    prefix = f"{REGISTRY_SERVER}/{IMAGE_REPOSITORY}:"
    require(
        published_tag.startswith(prefix),
        "Owned azd deploy did not publish the expected repository tag",
    )
    tag_name = published_tag.removeprefix(prefix)
    tags_after = published_tags()
    require(
        tag_name not in fresh_tags and tag_name in tags_after,
        "Owned azd deploy did not create a fresh tag",
    )
    tag_metadata = tags_after[tag_name]
    tag_time = parse_timestamp(
        tag_metadata.get("createdTime") or tag_metadata.get("lastUpdateTime")
    )
    require(tag_time >= started, "Published azd tag predates the owned build")
    digest = resolve_published_digest(published_tag)
    require(
        tag_metadata.get("digest") in (None, digest),
        "Published tag and manifest digests differ",
    )
    revision = after["properties"]["latestRevisionName"]
    require(
        revision != attempt["baselineRevision"]
        and revision == after["properties"]["latestReadyRevisionName"],
        "Owned azd deploy did not create a fresh healthy revision",
    )
    image = f"{REGISTRY_SERVER}/{IMAGE_REPOSITORY}@{digest}"
    completed = utc_now().isoformat()
    artifact = {
        "source": args.rollback_source,
        "sourceFingerprint": source_hash,
        "image": image,
        "publishedTag": published_tag,
        "publishedRevision": revision,
        "startedAt": attempt["startedAt"],
        "completedAt": completed,
        "tagCreatedAt": tag_time.isoformat(),
        "packageCommand": attempt["packageCommand"],
        "deployCommand": attempt["deployCommand"],
    }
    attempt.update(
        {
            "status": "succeeded",
            "completedAt": completed,
            "publishedTag": published_tag,
            "publishedRevision": revision,
            "image": image,
        }
    )
    receipt["buildAttempt"] = attempt
    receipt["artifact"] = artifact
    save_receipt(receipt)
    print(
        json.dumps(
            {
                "phase": "deploy",
                "source": args.rollback_source,
                "sourceFingerprint": source_hash,
                "image": image,
                "publishedTag": published_tag,
                "publishedRevision": revision,
                "receiptFingerprint": receipt_digest(receipt),
                "recorded": True,
            }
        )
    )


def run_deploy_image(args):
    receipt = load_receipt()
    require_receipt_source(receipt, args.rollback_source)
    validate_image(args.rollback_image)
    artifact = require_artifact(receipt, args.rollback_source, args.rollback_image)
    raw = get_app()
    baseline = fingerprint(raw)
    if args.expect_fingerprint:
        require(args.expect_fingerprint == baseline, "Reviewed app fingerprint drifted")
    require_static_configuration(raw)
    if (
        web_image(raw) == args.rollback_image
        and artifact.get("pinnedRevision") == raw["properties"]["latestRevisionName"]
        and healthz(raw)
    ):
        print(
            json.dumps(
                {
                    "phase": "deploy-image",
                    "fingerprint": baseline,
                    "applied": False,
                    "alreadyApplied": True,
                    "changes": [],
                }
            )
        )
        return
    require(
        web_image(raw) == artifact.get("publishedTag"),
        "Current image is neither the recorded azd artifact nor its approved digest",
    )
    snapshot = app_snapshot(raw)
    suffix = f"static-rollback-{baseline[:8]}"
    snapshot["properties"]["template"]["revisionSuffix"] = suffix
    expected = desired(snapshot, "deploy-image", suffix, args.rollback_image)
    template = build("main.bicep")
    parameters = app_phase_parameters(snapshot, "deploy-image", args.rollback_image)
    allowed = {
        APP: {"Deploy", "Modify"},
        f"{GROUP}/providers/Microsoft.Resources/deployments/dev-static-auth-app": {
            "Create",
            "Deploy",
            "Modify",
        },
    }
    report = preview("dev-static-auth-deploy-image-preview", template, parameters, allowed)
    print(
        json.dumps(
            {
                "phase": "deploy-image",
                "fingerprint": baseline,
                "receiptFingerprint": receipt_digest(receipt),
                "source": args.rollback_source,
                "image": args.rollback_image,
                "applied": False,
                **report,
            }
        )
    )
    if not args.apply:
        return
    require_apply_gates(args)
    require_receipt_source(load_receipt(), args.rollback_source)
    require(fingerprint(get_app()) == baseline, "App changed immediately before apply")
    require(
        receipt_digest(load_receipt()) == receipt_digest(receipt),
        "Migration receipt changed immediately before apply",
    )
    deploy(
        f"dev-static-auth-deploy-image-{baseline[:12]}",
        deployment_body(template, parameters),
    )
    verify_app_after(expected, suffix)
    after = get_app()
    require(web_image(after) == args.rollback_image, "Rollback digest pin was not retained")
    artifact["pinnedRevision"] = after["properties"]["latestRevisionName"]
    receipt["artifact"] = artifact
    save_receipt(receipt)


def run_cleanup_app(args):
    receipt = load_receipt()
    raw = get_app()
    baseline = fingerprint(raw)
    if args.expect_fingerprint:
        require(args.expect_fingerprint == baseline, "Reviewed app fingerprint drifted")
    if not rotation_environment_present(raw):
        artifact = require_artifact(receipt, args.rollback_source, args.rollback_image)
        if artifact.get("pinnedRevision") != raw["properties"]["latestRevisionName"]:
            require_cleanup_app_intent(
                receipt,
                raw,
                args.rollback_image,
                args.rollback_source,
            )
            print(
                json.dumps(
                    {
                        "phase": "cleanup-app",
                        "fingerprint": baseline,
                        "receiptFingerprint": receipt_digest(receipt),
                        "applied": False,
                        "reconciliationPending": True,
                        "changes": [str(receipt_path())],
                    }
                )
            )
            if not args.apply:
                return
            require_apply_gates(args)
            require(
                receipt_digest(load_receipt()) == receipt_digest(receipt),
                "Migration receipt changed immediately before reconciliation",
            )
            updated = promote_cleanup_revision(
                receipt,
                raw,
                args.rollback_image,
                args.rollback_source,
            )
            require(
                receipt_digest(load_receipt()) == receipt_digest(receipt),
                "Migration receipt changed immediately before pointer promotion",
            )
            save_receipt(updated)
            return
        require_approved_rollback(raw, receipt, args.rollback_image, args.rollback_source)
        require(healthz(raw), "Approved rollback revision health check failed")
        print(
            json.dumps(
                {
                    "phase": "cleanup-app",
                    "fingerprint": baseline,
                    "applied": False,
                    "alreadyApplied": True,
                    "changes": [],
                }
            )
        )
        return
    require_approved_rollback(raw, receipt, args.rollback_image, args.rollback_source)
    require(healthz(raw), "Approved rollback revision health check failed")
    snapshot = app_snapshot(raw)
    suffix = f"static-cleanup-{baseline[:8]}"
    snapshot["properties"]["template"]["revisionSuffix"] = suffix
    expected = desired(snapshot, "cleanup-app", suffix)
    template = build("main.bicep")
    parameters = app_phase_parameters(snapshot, "cleanup-app")
    allowed = {
        APP: {"Deploy", "Modify"},
        f"{GROUP}/providers/Microsoft.Resources/deployments/dev-static-auth-app": {
            "Create",
            "Deploy",
            "Modify",
        },
    }
    report = preview("dev-static-auth-cleanup-app-preview", template, parameters, allowed)
    print(
        json.dumps(
            {
                "phase": "cleanup-app",
                "fingerprint": baseline,
                "receiptFingerprint": receipt_digest(receipt),
                "applied": False,
                **report,
            }
        )
    )
    if not args.apply:
        return
    require_apply_gates(args)
    fresh = get_app()
    require(fingerprint(fresh) == baseline, "App changed immediately before apply")
    require_approved_rollback(fresh, receipt, args.rollback_image, args.rollback_source)
    require(healthz(fresh), "Approved rollback revision health check failed")
    require(
        receipt_digest(load_receipt()) == receipt_digest(receipt),
        "Migration receipt changed immediately before apply",
    )
    intent_receipt = copy.deepcopy(receipt)
    intent_artifact = copy.deepcopy(intent_receipt["artifact"])
    intent_artifact["cleanupAppIntent"] = cleanup_app_intent(
        receipt,
        fresh,
        expected,
        suffix,
        args.rollback_image,
        args.rollback_source,
    )
    intent_receipt["artifact"] = intent_artifact
    save_receipt(intent_receipt)
    deploy(
        f"dev-static-auth-cleanup-app-{baseline[:12]}",
        deployment_body(template, parameters),
    )
    verify_app_after(expected, suffix)
    after = get_app()
    updated = promote_cleanup_revision(
        intent_receipt,
        after,
        args.rollback_image,
        args.rollback_source,
    )
    require(
        receipt_digest(load_receipt()) == receipt_digest(intent_receipt),
        "Migration receipt changed immediately before pointer promotion",
    )
    save_receipt(updated)


def run_cleanup_vault(args):
    receipt = load_receipt()
    require_receipt_source(receipt, args.rollback_source)
    approved_app = get_app()
    require_approved_rollback(approved_app, receipt, args.rollback_image, args.rollback_source)
    require(healthz(approved_app), "Approved rollback revision health check failed")
    raw = get_vault()
    stage_state = {
        "receipt": receipt_digest(receipt),
        "app": fingerprint(approved_app),
        "vault": fingerprint(raw),
    }
    baseline = fingerprint(stage_state)
    if args.expect_fingerprint:
        require(args.expect_fingerprint == baseline, "Reviewed vault fingerprint drifted")
    original = receipt["vaultBaseline"]
    if project_vault_snapshot(raw) == original:
        validate_restored_vault(raw, original)
        print(
            json.dumps(
                {
                    "phase": "cleanup-vault",
                    "fingerprint": baseline,
                    "applied": False,
                    "alreadyApplied": True,
                    "changes": [],
                }
            )
        )
        return
    validate_transfer_vault(raw, original)
    template = build("vault-transfer-access.bicep")
    parameters = {
        "snapshot": {"value": original},
        "enableTransferAccess": {"value": False},
    }
    report = preview(
        "dev-static-auth-cleanup-vault-preview",
        template,
        parameters,
        {VAULT: {"Deploy", "Modify"}},
    )
    print(
        json.dumps(
            {
                "phase": "cleanup-vault",
                "fingerprint": baseline,
                "receiptFingerprint": receipt_digest(receipt),
                "originalNetworkAcls": (
                    "missing"
                    if "networkAcls" not in original["properties"]
                    else "null" if original["properties"]["networkAcls"] is None else "object"
                ),
                "applied": False,
                **report,
            }
        )
    )
    if not args.apply:
        return
    require_apply_gates(args)
    fresh_receipt = load_receipt()
    fresh_app = get_app()
    fresh_vault = get_vault()
    require(
        fingerprint(
            {
                "receipt": receipt_digest(fresh_receipt),
                "app": fingerprint(fresh_app),
                "vault": fingerprint(fresh_vault),
            }
        )
        == baseline,
        "Vault cleanup state changed immediately before apply",
    )
    require_approved_rollback(fresh_app, fresh_receipt, args.rollback_image, args.rollback_source)
    require(healthz(fresh_app), "Approved rollback revision health check failed")
    deploy(
        f"dev-static-auth-cleanup-vault-{baseline[:12]}",
        deployment_body(template, parameters),
    )
    validate_restored_vault(get_vault(), original)


def role_stage_state(receipt, raw_app, raw_vault, assignments):
    return {
        "receipt": receipt_digest(receipt),
        "app": fingerprint(raw_app),
        "vault": fingerprint(raw_vault),
        "assignment": assignments[0] if assignments else None,
    }


def run_cleanup_role(args):
    receipt = load_receipt()
    raw_app = get_app()
    require_approved_rollback(raw_app, receipt, args.rollback_image, args.rollback_source)
    require(healthz(raw_app), "Approved rollback revision health check failed")
    require(
        not rotation_environment_present(raw_app),
        "App cleanup must complete before role removal",
    )
    raw_vault = get_vault()
    validate_restored_vault(raw_vault, receipt["vaultBaseline"])
    assignments = role_assignments()
    state = role_stage_state(receipt, raw_app, raw_vault, assignments)
    baseline = fingerprint(state)
    if args.expect_fingerprint:
        require(args.expect_fingerprint == baseline, "Reviewed role fingerprint drifted")
    if not assignments:
        print(
            json.dumps(
                {
                    "phase": "cleanup-role",
                    "fingerprint": baseline,
                    "applied": False,
                    "alreadyApplied": True,
                    "changes": [],
                }
            )
        )
        return
    print(
        json.dumps(
            {
                "phase": "cleanup-role",
                "fingerprint": baseline,
                "receiptFingerprint": receipt_digest(receipt),
                "assignmentId": assignments[0]["id"],
                "applied": False,
                "changes": [assignments[0]["id"]],
            }
        )
    )
    if not args.apply:
        return
    require_apply_gates(args)
    fresh_app = get_app()
    fresh_vault = get_vault()
    fresh_assignments = role_assignments()
    require(
        fingerprint(role_stage_state(load_receipt(), fresh_app, fresh_vault, fresh_assignments))
        == baseline,
        "Role cleanup state changed immediately before apply",
    )
    require_approved_rollback(fresh_app, receipt, args.rollback_image, args.rollback_source)
    require(not rotation_environment_present(fresh_app), "App cleanup state changed")
    validate_restored_vault(fresh_vault, receipt["vaultBaseline"])
    command(
        [
            "az",
            "role",
            "assignment",
            "delete",
            "--subscription",
            SUBSCRIPTION,
            "--ids",
            fresh_assignments[0]["id"],
        ]
    )
    require(not role_assignments(), "Runtime vault role removal not confirmed")


def run_cleanup_summary(args):
    require(not args.apply, "Legacy cleanup is plan-only; invoke each cleanup stage separately")
    receipt = load_receipt()
    raw_app = get_app()
    require_approved_rollback(raw_app, receipt, args.rollback_image, args.rollback_source)
    raw_vault = get_vault()
    assignments = role_assignments()
    next_steps = []
    if rotation_environment_present(raw_app):
        next_steps.append("cleanup-app")
    if project_vault_snapshot(raw_vault) != receipt["vaultBaseline"]:
        next_steps.append("cleanup-vault")
    if assignments:
        next_steps.append("cleanup-role")
    print(json.dumps({"phase": "cleanup", "applied": False, "next": next_steps}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phase",
        choices=(
            "access",
            "transfer",
            "deploy",
            "deploy-image",
            "cleanup",
            "cleanup-app",
            "cleanup-vault",
            "cleanup-role",
        ),
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approve-change", action="store_true")
    parser.add_argument("--reviewed", action="store_true")
    parser.add_argument("--expect-fingerprint")
    parser.add_argument("--rollback-source")
    parser.add_argument("--rollback-image")
    args = parser.parse_args()
    if args.expect_fingerprint and not re.fullmatch(r"[0-9a-f]{64}", args.expect_fingerprint):
        parser.error("Fingerprint must be a SHA-256 digest")
    if (
        args.phase
        in {
            "access",
            "transfer",
            "deploy",
            "deploy-image",
            "cleanup",
            "cleanup-app",
            "cleanup-vault",
            "cleanup-role",
        }
        and not args.rollback_source
    ):
        parser.error("--rollback-source is required")
    if (
        args.phase
        in {
            "deploy-image",
            "cleanup",
            "cleanup-app",
            "cleanup-vault",
            "cleanup-role",
        }
        and not args.rollback_image
    ):
        parser.error("--rollback-image is required")
    try:
        runners = {
            "access": run_access,
            "transfer": run_transfer,
            "deploy": run_deploy_artifact,
            "deploy-image": run_deploy_image,
            "cleanup": run_cleanup_summary,
            "cleanup-app": run_cleanup_app,
            "cleanup-vault": run_cleanup_vault,
            "cleanup-role": run_cleanup_role,
        }
        runners[args.phase](args)
    except GateError as error:
        print(json.dumps({"status": "blocked", "reason": str(error)}))
        return 1
    except (KeyError, TypeError, ValueError, StopIteration, OSError, subprocess.SubprocessError):
        print(json.dumps({"status": "blocked", "reason": "Safe migration gate failed"}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
