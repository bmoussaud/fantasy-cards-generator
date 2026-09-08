"""Dev-only, drift-preserving Bicep leaf deployment. Default action is read-only.

ARM requests and responses stay in memory; only allowlisted evidence is printed.
No root azd hooks, env files, client secret-list operations, or raw CLI diagnostics.
"""

import argparse
import copy
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SUBSCRIPTION = "b8ff3e15-7e2d-4fac-a773-992fb59ccedd"
GROUP = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-fcag-dev"
APP = f"{GROUP}/providers/Microsoft.App/containerApps/fcag-dev-app"
PROJECT = (
    f"{GROUP}/providers/Microsoft.CognitiveServices/accounts/"
    "aifcagdevqhg3qc4rlbt4g/projects/fantasy-cards-dev"
)
ENDPOINT = "https://aifcagdevqhg3qc4rlbt4g.services.ai.azure.com/api/projects/fantasy-cards-dev"
PRINCIPAL = "946d8701-48f2-4fa5-8efd-bf053c7b4e4c"
API = "2025-01-01"
KEY = "FOUNDRY_PROJECT_ENDPOINT"
HERE = Path(__file__).resolve().parent
# Pin the entire compiled executable contract, excluding compiler provenance.
# Any expression/resource/parameter change requires independent re-review.
COMPILED_CONTRACT = "56505e8c7bfe5512c58bdc8846ea0fb2fc735a357152d865732bb0ebaabf5af1"


class GateError(Exception):
    """Only constant, non-sensitive messages may cross the operator boundary."""


# Reviewed 2025-01-01 writable schema. None accepts scalar values, not arbitrary
# objects. "*" is restricted to documented string maps / MI resource-ID maps.
ENV = {"name": None, "value": None, "secretRef": None}
AUTH = [{"secretRef": None, "triggerParameter": None}]
RULE = {"auth": AUTH, "identity": None, "metadata": {"*": None}}
PROBE = {
    **dict.fromkeys(
        "failureThreshold initialDelaySeconds periodSeconds successThreshold "
        "terminationGracePeriodSeconds timeoutSeconds type".split()
    ),
    "httpGet": {
        **dict.fromkeys("host path port scheme".split()),
        "httpHeaders": [{"name": None, "value": None}],
    },
    "tcpSocket": {"host": None, "port": None},
}
CONTAINER = {
    **dict.fromkeys("name image".split()),
    "args": [None],
    "command": [None],
    "env": [ENV],
    "probes": [PROBE],
    "resources": {"cpu": None, "memory": None, "ephemeralStorage": None},
    "volumeMounts": [{"mountPath": None, "subPath": None, "volumeName": None}],
}
INGRESS = {
    **dict.fromkeys(
        "allowInsecure clientCertificateMode exposedPort external targetPort transport fqdn".split()
    ),
    "additionalPortMappings": [{"exposedPort": None, "external": None, "targetPort": None}],
    "corsPolicy": {
        "allowCredentials": None,
        "maxAge": None,
        **{
            k: [None]
            for k in ("allowedHeaders", "allowedMethods", "allowedOrigins", "exposeHeaders")
        },
    },
    "customDomains": [{"bindingType": None, "certificateId": None, "name": None}],
    "ipSecurityRestrictions": [dict.fromkeys("action description ipAddressRange name".split())],
    "stickySessions": {"affinity": None},
    "traffic": [dict.fromkeys("label latestRevision revisionName weight".split())],
}
CONFIG = {
    **dict.fromkeys("activeRevisionsMode maxInactiveRevisions".split()),
    "dapr": dict.fromkeys(
        "appId appPort appProtocol enableApiLogging enabled httpMaxRequestSize "
        "httpReadBufferSize logLevel".split()
    ),
    "identitySettings": [{"identity": None, "lifecycle": None}],
    "ingress": INGRESS,
    "registries": [dict.fromkeys("identity passwordSecretRef server username".split())],
    # GET returns only metadata. Reject any secret value, including null.
    "secrets": [dict.fromkeys("name identity keyVaultUrl".split())],
    "runtime": {"java": {"enableMetrics": None}},
    "service": {"type": None},
}
TEMPLATE = {
    "revisionSuffix": None,
    "terminationGracePeriodSeconds": None,
    "containers": [CONTAINER],
    "initContainers": [{k: v for k, v in CONTAINER.items() if k != "probes"}],
    "scale": {
        **dict.fromkeys("cooldownPeriod maxReplicas minReplicas pollingInterval".split()),
        "rules": [
            {
                "name": None,
                "azureQueue": {
                    **dict.fromkeys("accountName queueLength queueName identity".split()),
                    "auth": AUTH,
                },
                "custom": {**RULE, "type": None},
                "http": RULE,
                "tcp": RULE,
            }
        ],
    },
    "serviceBinds": [{"name": None, "serviceId": None}],
    "volumes": [
        {
            **dict.fromkeys("mountOptions name storageName storageType".split()),
            "secrets": [{"path": None, "secretRef": None}],
        }
    ],
}
READ_ONLY = (
    "provisioningState runningStatus latestRevisionName latestReadyRevisionName "
    "latestRevisionFqdn customDomainVerificationId eventStreamEndpoint outboundIpAddresses"
).split()
SCHEMA = {
    **dict.fromkeys("id name type location etag".split()),
    "tags": {"*": None},
    "identity": {
        **dict.fromkeys("type principalId tenantId".split()),
        "userAssignedIdentities": {"*": {"clientId": None, "principalId": None}},
    },
    "systemData": dict.fromkeys(
        "createdAt createdBy createdByType lastModifiedAt lastModifiedBy lastModifiedByType".split()
    ),
    "properties": {
        **dict.fromkeys(k for k in READ_ONLY if k != "outboundIpAddresses"),
        "outboundIpAddresses": [None],
        "delegatedIdentities": [],
        **dict.fromkeys("environmentId managedEnvironmentId workloadProfileName".split()),
        "configuration": CONFIG,
        "template": TEMPLATE,
    },
}


def require(condition, message):
    if not condition:
        raise GateError(message)


def validate_shape(value, schema):
    if value is None:
        return
    if isinstance(schema, dict):
        require(isinstance(value, dict), "Unexpected configuration object")
        for key, child in value.items():
            require(key in schema or "*" in schema, "Unknown configuration field")
            validate_shape(child, schema.get(key, schema.get("*")))
    elif isinstance(schema, list):
        require(isinstance(value, list), "Unexpected configuration list")
        if not schema:
            require(not value, "Unreviewed delegated identities")
        else:
            for child in value:
                validate_shape(child, schema[0])
    else:
        require(not isinstance(value, (dict, list)), "Unexpected nested configuration")


def snapshot(raw):
    validate_shape(raw, SCHEMA)
    require(raw["id"].lower() == APP.lower(), "Unexpected app ownership")
    require(raw["name"] == "fcag-dev-app", "Unexpected app name")
    require(raw["type"].lower() == "microsoft.app/containerapps", "Unexpected app type")
    identity = raw["identity"]
    require(identity["principalId"] == PRINCIPAL, "Unexpected system principal")
    require(identity["type"] == "SystemAssigned, UserAssigned", "Unexpected identity type")
    for resource_id in identity["userAssignedIdentities"]:
        require(
            resource_id.lower().startswith(
                GROUP.lower() + "/providers/microsoft.managedidentity/userassignedidentities/"
            ),
            "Unexpected user-assigned identity ownership",
        )
    properties = raw["properties"]
    environment = GROUP.lower() + "/providers/microsoft.app/managedenvironments/fcag-dev-cae"
    require(
        properties["environmentId"].lower() == environment
        and properties["managedEnvironmentId"].lower() == environment,
        "Unexpected managed environment",
    )
    config = properties["configuration"]
    require_replayable_secrets(raw)
    require(config["activeRevisionsMode"] == "Single", "Only Single revision mode is allowed")
    require(
        config["ingress"]["traffic"] == [{"latestRevision": True, "weight": 100}],
        "Only 100 percent latest traffic is allowed",
    )
    containers = properties["template"]["containers"]
    names = [c["name"] for c in containers]
    require(len(names) == len(set(names)) and names.count("web") == 1, "Ambiguous web container")
    for container in containers + (properties["template"].get("initContainers") or []):
        names = []
        for env in container.get("env") or []:
            require(
                isinstance(env, dict)
                and isinstance(env.get("name"), str)
                and env["name"]
                and set(env) in ({"name", "value"}, {"name", "secretRef"})
                and all(isinstance(v, str) for v in env.values()),
                "Malformed environment entry",
            )
            names.append(env["name"])
        require(len(names) == len(set(names)), "Duplicate environment entry")
    web = next(c for c in containers if c["name"] == "web")
    require(isinstance(web.get("env"), list), "Missing web environment")
    entry = next((e for e in web["env"] if e["name"] == KEY), None)
    require(
        entry is None or entry == {"name": KEY, "value": ENDPOINT},
        "Existing endpoint differs from approved project",
    )
    result = copy.deepcopy({k: raw[k] for k in ("location", "tags", "identity", "properties")})
    result["identity"] = {
        "type": identity["type"],
        "userAssignedIdentities": {key: {} for key in identity["userAssignedIdentities"]},
    }
    p = result["properties"]
    for key in READ_ONLY + ["delegatedIdentities"]:
        p.pop(key, None)
    p["configuration"]["ingress"].pop("fqdn", None)
    for container in p["template"]["containers"] + (p["template"].get("initContainers") or []):
        container.get("resources", {}).pop("ephemeralStorage", None)
    return result


def fingerprint(raw):
    # Includes read-only revision/identity/secret metadata and systemData so a
    # concurrent secret/config write also invalidates the reviewed baseline.
    return hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()


def desired(current, suffix, operation="persist"):
    result = copy.deepcopy(current)
    template = result["properties"]["template"]
    template["revisionSuffix"] = suffix
    web = next(c for c in template["containers"] if c["name"] == "web")
    web["env"] = [e for e in web["env"] if e["name"] != KEY]
    if operation == "persist":
        web["env"].append({"name": KEY, "value": ENDPOINT})
    return result


def require_replayable_secrets(current):
    secrets = current["properties"]["configuration"].get("secrets")
    require(isinstance(secrets, list), "Missing secret inventory")
    names = []
    for secret in secrets:
        require(
            isinstance(secret, dict)
            and set(secret) <= {"name", "keyVaultUrl", "identity"}
            and isinstance(secret.get("name"), str)
            and bool(secret["name"])
            and all(v is None or isinstance(v, str) for v in secret.values())
            and bool(secret.get("keyVaultUrl")) == bool(secret.get("identity")),
            "Malformed secret metadata",
        )
        names.append(secret["name"])
    require(len(names) == len(set(names)), "Duplicate secret metadata")


def verify_compiled(template):
    contract = {
        k: v for k, v in template.items() if k not in ("metadata", "$schema", "contentVersion")
    }
    require(
        hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()
        == COMPILED_CONTRACT,
        "Compiled Azure pass-through contract changed; independent review required",
    )


def command(args, payload=None):
    result = subprocess.run(
        args,
        input=None if payload is None else json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=180,
    )
    require(result.returncode == 0, "Azure/tool command failed; raw diagnostics suppressed")
    return json.loads(result.stdout)


def rest(method, path, payload=None):
    require(
        path.startswith(GROUP) or path.startswith("https://management.azure.com/"),
        "Unexpected ARM request scope",
    )
    url = path if path.startswith("https://") else "https://management.azure.com" + path
    args = ["az", "rest", "--method", method, "--url", url, "--output", "json"]
    if payload is not None:
        args += ["--body", "@/dev/stdin"]
    return command(args, payload)


def get_app():
    return rest("get", f"{APP}?api-version={API}")


def verify_project():
    project = rest("get", f"{PROJECT}?api-version=2025-06-01")
    require(project["id"].lower() == PROJECT.lower(), "Unexpected Foundry project")
    require(project["properties"]["provisioningState"] == "Succeeded", "Project is not ready")
    require(
        project["properties"]["endpoints"]["AI Foundry API"] == ENDPOINT,
        "Project ARM endpoint mismatch",
    )


def deployment_body(current, suffix, operation):
    require_replayable_secrets(current)
    template = command(
        ["az", "bicep", "build", "--file", str(HERE / "infra/main.bicep"), "--stdout"]
    )
    verify_compiled(template)
    resources = template["resources"]
    require(
        len(resources) == 1
        and resources[0]["type"] == "Microsoft.App/containerApps"
        and resources[0]["name"] == "fcag-dev-app"
        and resources[0]["apiVersion"] == API
        and template["parameters"]["snapshot"]["type"] == "secureObject"
        and not template.get("outputs"),
        "Unexpected compiled resource scope or insecure parameters",
    )
    return {
        "properties": {
            "mode": "Incremental",
            "template": template,
            "parameters": {
                "snapshot": {"value": desired(current, suffix, operation)},
            },
        }
    }


def normalized(value):
    """ARM what-if omits nulls and read-only/default response fields."""
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [normalized(v) for v in value]
    return value


def comparable(resource):
    result = normalized(copy.deepcopy(resource))
    result["location"] = result["location"].lower().replace(" ", "")
    config = result["properties"]["configuration"]
    if config.get("identitySettings") == []:
        config.pop("identitySettings")
    for registry in config.get("registries") or []:
        for key in ("passwordSecretRef", "username"):
            if registry.get(key) == "":
                registry.pop(key)
    return result


def inspect_plan(plan, current, expected):
    require(plan.get("status") == "Succeeded", "What-if did not succeed")
    require(
        not any(plan.get(k) for k in ("error", "diagnostics", "potentialChanges")),
        "What-if contains diagnostics or unconfirmed changes",
    )
    changes = plan.get("changes")
    require(isinstance(changes, list), "What-if lacks changes")
    require(
        all(
            c.get("changeType") != "Ignore"
            or c.get("resourceId", "").lower().startswith(GROUP.lower() + "/providers/")
            for c in changes
        ),
        "What-if includes an unexpected scope",
    )
    ignored = sum(c.get("changeType") == "Ignore" for c in changes)
    changes = [c for c in changes if c.get("changeType") != "Ignore"]
    require(
        len(changes) == 1, "What-if is empty, expanded incorrectly, or includes unrelated resources"
    )
    change = changes[0]
    require(change.get("resourceId", "").lower() == APP.lower(), "What-if has unrelated resource")
    require(
        change.get("changeType") in ("Deploy", "Modify"),
        "ResourceIdOnly must redeploy or modify the exact existing app",
    )
    require(
        all(
            set(c)
            <= {
                "resourceId",
                "changeType",
                "before",
                "after",
                "delta",
                "unsupportedReason",
                "deploymentId",
                "extension",
                "identifiers",
                "symbolicName",
            }
            and all(
                c.get(k) is None
                for k in (
                    "before",
                    "after",
                    "delta",
                    "unsupportedReason",
                    "deploymentId",
                    "extension",
                    "identifiers",
                    "symbolicName",
                )
            )
            for c in plan["changes"]
        ),
        "ResourceIdOnly preview unexpectedly contains payloads or diagnostics",
    )
    # Cloud preview proves scope ONLY. Independently compare the deterministic
    # metadata transform, while the pinned ARM expression preserves native values.
    local = copy.deepcopy(expected)
    local["properties"]["template"]["revisionSuffix"] = current["properties"]["template"][
        "revisionSuffix"
    ]
    old_web = next(c for c in current["properties"]["template"]["containers"] if c["name"] == "web")
    new_web = next(c for c in local["properties"]["template"]["containers"] if c["name"] == "web")
    entries = [e for e in new_web["env"] if e["name"] == KEY]
    require(entries in ([], [{"name": KEY, "value": ENDPOINT}]), "Unexpected endpoint transform")
    new_web["env"] = [e for e in new_web["env"] if e["name"] != KEY]
    old = copy.deepcopy(current)
    next(c for c in old["properties"]["template"]["containers"] if c["name"] == "web")["env"] = [
        e for e in old_web["env"] if e["name"] != KEY
    ]
    require(local == old, "Local transform changes unowned configuration")
    return {
        "resource": APP,
        "operation": change["changeType"],
        "allowedChanges": [
            "properties.template.containers[web].env.FOUNDRY_PROJECT_ENDPOINT",
            "properties.template.revisionSuffix",
        ],
        "cloudScopeOnly": True,
        "localMetadataPreservationMatched": True,
        "nativeValues": "Azure-only guarded identity pass-through; not operator inspected",
        "ignoredResources": ignored,
    }


def scope_preview_template(body):
    """Only metadata enters the secure default; list evaluation stays inside ARM."""
    template = copy.deepcopy(body["properties"]["template"])
    verify_compiled(template)

    # ARM evaluates strings in template defaults: escape metadata literal syntax.
    def literal(value):
        if isinstance(value, dict):
            return {k: literal(v) for k, v in value.items()}
        if isinstance(value, list):
            return [literal(v) for v in value]
        return "[" + value if isinstance(value, str) and value.startswith("[") else value

    template["parameters"]["snapshot"]["defaultValue"] = literal(
        body["properties"]["parameters"]["snapshot"]["value"]
    )
    return template


def preview(body):
    return command(
        [
            "az",
            "deployment",
            "group",
            "what-if",
            "--subscription",
            SUBSCRIPTION,
            "--resource-group",
            "rg-fcag-dev",
            "--name",
            "dev-endpoint-preview",
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
        scope_preview_template(body),
    )


def guard_diagnostic_body(body, invalid=False):
    """Derive a resource-free boolean probe from the pinned resource-input guard."""
    result = copy.deepcopy(body)
    template = result["properties"]["template"]
    verify_compiled(template)
    expression = template["resources"][0]["properties"]
    require(expression.startswith("[if("), "Missing compiled input guard")
    # Bicep inlines runtime list calls. Extract the first if argument, respecting
    # nested calls and ARM string literals (including doubled quote escapes).
    depth, quoted = 0, False
    predicate = None
    for index, char in enumerate(expression[4:], 4):
        if char == "'":
            quoted = not quoted
        elif not quoted:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == "," and depth == 0:
                predicate = expression[4:index]
                break
    require(predicate is not None, "Unable to extract compiled input guard")
    template["resources"] = []
    template["outputs"] = {"inventoryValid": {"type": "bool", "value": f"[{predicate}]"}}
    if invalid:
        secrets = result["properties"]["parameters"]["snapshot"]["value"]["properties"][
            "configuration"
        ]["secrets"]
        # A duplicate guarantees failure independent of the real inventory.
        if secrets:
            secrets.append(copy.deepcopy(secrets[0]))
        else:
            secrets.extend([{"name": "diagnostic-invalid"}, {"name": "diagnostic-invalid"}])
    require(
        template["resources"] == []
        and set(template["outputs"]) == {"inventoryValid"}
        and template["outputs"]["inventoryValid"] == {"type": "bool", "value": f"[{predicate}]"}
        and set(template["parameters"]) == {"snapshot"}
        and template["parameters"]["snapshot"]["type"] == "secureObject",
        "Unsafe diagnostic resource or output scope",
    )
    return result


def validate_guard(body, baseline):
    for invalid in (False, True):
        diagnostic = guard_diagnostic_body(body, invalid)
        variant = "invalid" if invalid else "valid"
        path = (
            f"{GROUP}/providers/Microsoft.Resources/deployments/"
            f"dev-endpoint-guard-{variant}-{baseline[:12]}?api-version=2025-04-01"
        )
        require(fingerprint(get_app()) == baseline, "Baseline changed before guard diagnostic")
        result = rest("put", path, diagnostic)
        deadline = time.monotonic() + 180
        while result["properties"]["provisioningState"] in ("Accepted", "Running"):
            require(time.monotonic() < deadline, "Guard diagnostic timed out")
            time.sleep(3)
            result = rest("get", path)
        require(
            result["properties"]["provisioningState"] == "Succeeded",
            "Guard diagnostic failed; raw diagnostics suppressed",
        )
        outputs = result["properties"].get("outputs")
        require(
            isinstance(outputs, dict)
            and set(outputs) == {"inventoryValid"}
            and isinstance(outputs["inventoryValid"], dict)
            and set(outputs["inventoryValid"]) == {"type", "value"}
            and outputs["inventoryValid"]["type"] in ("Bool", "bool")
            and outputs["inventoryValid"]["value"] is (not invalid),
            "Guard diagnostic did not return the expected strict boolean",
        )
        require(fingerprint(get_app()) == baseline, "Baseline changed during guard diagnostic")
        print(
            json.dumps({"guardDiagnostic": variant, "inventoryValid": not invalid, "resources": 0})
        )


def healthy(raw):
    properties = raw["properties"]
    return (
        properties.get("provisioningState") == "Succeeded"
        and properties.get("runningStatus") == "Running"
        and properties.get("latestRevisionName") == properties.get("latestReadyRevisionName")
    )


def healthz(raw):
    fqdn = raw["properties"]["configuration"]["ingress"]["fqdn"]
    require(
        re.fullmatch(r"fcag-dev-app\.[a-z0-9.-]+\.azurecontainerapps\.io", fqdn) is not None,
        "Unexpected health endpoint hostname",
    )
    try:
        with urllib.request.urlopen(f"https://{fqdn}/healthz", timeout=15) as response:
            return response.status == 200
    except OSError:
        return False


def run(args):
    verify_project()
    raw = get_app()
    current = snapshot(raw)
    require(healthy(raw), "Existing app is not healthy")
    baseline = fingerprint(raw)
    if args.expect_fingerprint:
        require(args.expect_fingerprint == baseline, "Reviewed baseline has drifted")
    suffix = f"endpoint-{'off-' if args.remove else ''}{baseline[:12]}"
    operation = "remove" if args.remove else "persist"
    expected = desired(current, suffix, operation)
    body = deployment_body(current, suffix, operation)
    plan = preview(body)
    report = inspect_plan(plan, current, expected)
    require(fingerprint(get_app()) == baseline, "Baseline changed during preview")
    report.update(baselineFingerprint=baseline, endpoint=ENDPOINT, action=operation, applied=False)
    print(json.dumps(report), flush=True)
    if not args.apply:
        if args.validate_guard:
            validate_guard(body, baseline)
        return
    require(bool(args.expect_fingerprint), "Apply requires the independently reviewed fingerprint")
    validate_guard(body, baseline)
    verify_project()
    require(fingerprint(get_app()) == baseline, "Baseline changed immediately before apply")
    # No ETag is returned by this ACA API. Immediate re-read is an optimistic
    # concurrency gate, not an atomic lock; serialize the operator change window.
    path = f"{GROUP}/providers/Microsoft.Resources/deployments/dev-endpoint-{baseline[:12]}"
    rest("put", f"{path}?api-version=2025-04-01", body)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        after = get_app()
        if (
            comparable(snapshot(after)) == comparable(expected)
            and healthy(after)
            and after["properties"]["latestRevisionName"] == f"fcag-dev-app--{suffix}"
            and healthz(after)
        ):
            print(
                json.dumps(
                    {
                        "applied": True,
                        "healthy": True,
                        "resource": APP,
                        "revision": after["properties"]["latestRevisionName"],
                        "endpoint": ENDPOINT if operation == "persist" else None,
                        "systemPrincipalMatched": after["identity"]["principalId"] == PRINCIPAL,
                        "allWritableMetadataMatched": True,
                        "healthz": 200,
                    }
                )
            )
            return
        time.sleep(5)
    raise GateError("Apply health/config verification incomplete; do not retry or invoke models")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--validate-guard",
        action="store_true",
        help="Run resource-free Azure guard diagnostics (deployment records only)",
    )
    parser.add_argument("--expect-fingerprint")
    parser.add_argument(
        "--remove", action="store_true", help="Rollback only this endpoint; preview by default"
    )
    args = parser.parse_args()
    if args.expect_fingerprint and not re.fullmatch("[0-9a-f]{64}", args.expect_fingerprint):
        parser.error("Fingerprint must be a SHA-256 hex digest")
    try:
        run(args)
    except GateError as error:
        print(json.dumps({"status": "blocked", "reason": str(error)}))
        return 1
    except (KeyError, TypeError, ValueError, StopIteration, OSError, subprocess.SubprocessError):
        # Never expose raw payloads through exceptions or CLI stderr.
        print(json.dumps({"status": "blocked", "reason": "Safe deployment gate failed"}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
