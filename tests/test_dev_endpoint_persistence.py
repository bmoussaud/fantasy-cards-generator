"""Offline JSON-transform and fail-closed deployment gates; no Azure/model calls."""

import argparse
import copy
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "dev_endpoint", ROOT / "deployments/dev-endpoint/persist_endpoint.py"
)
endpoint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(endpoint)


@pytest.fixture
def raw():
    environment = endpoint.GROUP + "/providers/Microsoft.App/managedEnvironments/fcag-dev-cae"
    identity_base = endpoint.GROUP + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/"
    return {
        "id": endpoint.APP,
        "name": "fcag-dev-app",
        "type": "Microsoft.App/containerApps",
        "location": "eastus2",
        "tags": {"azd-service-name": "web-nat"},
        "identity": {
            "type": "SystemAssigned, UserAssigned",
            "principalId": endpoint.PRINCIPAL,
            "tenantId": "test-tenant",
            "userAssignedIdentities": {
                identity_base + name: {"clientId": name, "principalId": name}
                for name in ("fcag-dev-acr-pull", "second-existing")
            },
        },
        "properties": {
            "provisioningState": "Succeeded",
            "runningStatus": "Running",
            "latestRevisionName": "old",
            "latestReadyRevisionName": "old",
            "environmentId": environment,
            "managedEnvironmentId": environment,
            "workloadProfileName": "Consumption",
            "delegatedIdentities": [],
            "configuration": {
                "activeRevisionsMode": "Single",
                "ingress": {
                    "external": True,
                    "targetPort": 8000,
                    "traffic": [{"latestRevision": True, "weight": 100}],
                    "fqdn": "example.invalid",
                },
                "secrets": [
                    {
                        "name": "app-secret",
                        "keyVaultUrl": "https://example.vault.azure.net/secrets/test",
                        "identity": "system",
                    }
                ],
                "registries": [
                    {
                        "server": "example.azurecr.io",
                        "identity": identity_base + "fcag-dev-acr-pull",
                        "passwordSecretRef": "",
                        "username": "",
                    }
                ],
                "identitySettings": [],
            },
            "template": {
                "revisionSuffix": "old",
                "containers": [
                    {
                        "name": "web",
                        "image": "example.azurecr.io/web@sha256:immutable",
                        "env": [
                            {"name": "APP_SECRET", "secretRef": "app-secret"},
                            {"name": "OTHER", "value": "unchanged"},
                        ],
                        "resources": {"cpu": 0.5, "memory": "1Gi", "ephemeralStorage": "2Gi"},
                    },
                    {
                        "name": "sidecar",
                        "image": "example.azurecr.io/sidecar:keep",
                        "env": [{"name": endpoint.KEY, "value": "sidecar-is-not-owned"}],
                    },
                ],
                "initContainers": [
                    {
                        "name": "init",
                        "image": "example.azurecr.io/init:keep",
                        "command": ["sh"],
                        "args": ["init"],
                        "env": [{"name": "TOKEN", "secretRef": "app-secret"}],
                    }
                ],
                "volumes": [{"name": "data", "storageType": "AzureFile", "storageName": "data"}],
                "scale": {"minReplicas": 1, "maxReplicas": 2},
            },
        },
        "systemData": {"lastModifiedAt": "2026-09-08T14:00:00Z"},
    }


def test_transform_changes_only_web_endpoint_and_revision(raw):
    original = copy.deepcopy(raw)
    current = endpoint.snapshot(raw)
    result = endpoint.desired(current, "endpoint-reviewed")
    expected = copy.deepcopy(current)
    expected["properties"]["template"]["revisionSuffix"] = "endpoint-reviewed"
    expected["properties"]["template"]["containers"][0]["env"].append(
        {"name": endpoint.KEY, "value": endpoint.ENDPOINT}
    )
    assert result == expected
    assert raw == original
    assert len(result["identity"]["userAssignedIdentities"]) == 2
    assert all(value == {} for value in result["identity"]["userAssignedIdentities"].values())
    assert (
        result["properties"]["template"]["initContainers"]
        == current["properties"]["template"]["initContainers"]
    )


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "X"},
        {"name": "X", "value": None},
        {"name": "X", "value": {}, "secretRef": "s"},
        {"name": "X", "value": "v", "secretRef": "s"},
        {"name": "X", "value": "v", "extra": "no"},
        {"name": "", "value": "v"},
        {"name": "OTHER", "value": "duplicate"},
        {"name": endpoint.KEY, "value": "https://attacker.invalid"},
        {"name": endpoint.KEY, "secretRef": "s"},
    ],
)
def test_malformed_duplicate_extra_and_untrusted_endpoint_fail(raw, entry):
    raw["properties"]["template"]["containers"][0]["env"].append(entry)
    with pytest.raises(endpoint.GateError):
        endpoint.snapshot(raw)


@pytest.mark.parametrize(
    "path",
    [
        ("unknown",),
        ("properties", "unknown"),
        ("properties", "configuration", "newField"),
        ("properties", "template", "containers", 1, "newField"),
        ("properties", "template", "initContainers", 0, "newField"),
        ("properties", "template", "volumes", 0, "newField"),
    ],
)
def test_unknown_fields_fail_even_on_sidecars_and_null(raw, path):
    cursor = raw
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = None
    with pytest.raises(endpoint.GateError):
        endpoint.snapshot(raw)


def test_native_secret_metadata_is_kept_without_client_values(raw):
    raw["properties"]["configuration"]["secrets"] = [{"name": "native-secret"}]
    current = endpoint.snapshot(raw)
    assert current["properties"]["configuration"]["secrets"] == [{"name": "native-secret"}]
    endpoint.require_replayable_secrets(current)


@pytest.mark.parametrize(
    "secrets",
    [
        None,
        {},
        [{"name": ""}],
        [{"name": 1}],
        [{"name": "same"}, {"name": "same"}],
        [{"name": "a", "identity": "system"}],
        [{"name": "a", "keyVaultUrl": "https://example.vault.azure.net/secrets/a"}],
        [{"name": "a", "keyVaultUrl": []}],
        [{"name": "a", "unknown": None}],
        [{"name": "a", "value": None}],
    ],
)
def test_malformed_secret_inventory_fails_before_cli(raw, secrets, monkeypatch):
    raw["properties"]["configuration"]["secrets"] = secrets
    monkeypatch.setattr(endpoint, "command", lambda *a: pytest.fail("No CLI allowed"))
    with pytest.raises(endpoint.GateError):
        endpoint.snapshot(raw)


def test_secret_values_are_rejected_without_rendering_them(raw):
    raw["properties"]["configuration"]["secrets"][0]["value"] = "PRIVATE-SENTINEL"
    with pytest.raises(endpoint.GateError) as error:
        endpoint.snapshot(raw)
    assert "PRIVATE-SENTINEL" not in str(error.value)


def test_rollback_uses_fresh_config_not_old_revision(raw):
    current = endpoint.snapshot(raw)
    added = endpoint.desired(current, "new")
    added["properties"]["template"]["containers"][1]["image"] = "sidecar:parallel-change"
    removed = endpoint.desired(added, "rollback", "remove")
    assert removed["properties"]["template"]["containers"][0]["env"] == (
        current["properties"]["template"]["containers"][0]["env"]
    )
    assert removed["properties"]["template"]["containers"][1]["image"] == "sidecar:parallel-change"
    assert removed["properties"]["template"]["revisionSuffix"] == "rollback"


def plan(current, expected):
    return {
        "status": "Succeeded",
        "changes": [
            {
                "resourceId": endpoint.APP,
                "changeType": "Modify",
            }
        ],
    }


@pytest.mark.parametrize("kind", ["Deploy", "Modify"])
def test_scope_only_plan_with_local_preservation_and_ignored_resource(raw, kind):
    current = endpoint.snapshot(raw)
    expected = endpoint.desired(current, "new")
    actual = plan(current, expected)
    actual["changes"][0]["changeType"] = kind
    actual["changes"][0].update(deploymentId=None, identifiers=None, symbolicName=None)
    actual["changes"].append(
        {
            "resourceId": endpoint.GROUP + "/providers/Microsoft.Network/virtualNetworks/existing",
            "changeType": "Ignore",
        }
    )
    report = endpoint.inspect_plan(actual, current, expected)
    assert report["cloudScopeOnly"] and report["localMetadataPreservationMatched"]
    assert "fullBeforeAfterMatched" not in report
    assert report["ignoredResources"] == 1
    assert report["operation"] == kind
    assert "unchanged" not in json.dumps(report)


@pytest.mark.parametrize(
    "path,value",
    [
        (("properties", "template", "containers", 0, "image"), "wrong:image"),
        (("properties", "template", "containers", 1, "image"), "wrong:sidecar"),
        (("properties", "configuration", "ingress", "external"), False),
        (("properties", "configuration", "secrets"), []),
        (("properties", "template", "scale", "maxReplicas"), 100),
        (("identity", "userAssignedIdentities"), {}),
    ],
)
def test_whatif_rejects_image_network_secrets_scale_identity(raw, path, value):
    current = endpoint.snapshot(raw)
    expected = endpoint.desired(current, "new")
    altered = copy.deepcopy(expected)
    cursor = altered
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    with pytest.raises(endpoint.GateError):
        endpoint.inspect_plan(plan(current, altered), current, altered)


@pytest.mark.parametrize("kind", ["Create", "Delete", "Unsupported", "NoChange", None, "Ignore"])
def test_whatif_rejects_non_modification(raw, kind):
    current = endpoint.snapshot(raw)
    expected = endpoint.desired(current, "new")
    actual = plan(current, expected)
    actual["changes"][0]["changeType"] = kind
    with pytest.raises(endpoint.GateError):
        endpoint.inspect_plan(actual, current, expected)


def test_whatif_rejects_role_creation_and_unexpanded_secure_parameter(raw):
    current = endpoint.snapshot(raw)
    expected = endpoint.desired(current, "new")
    actual = plan(current, expected)
    actual["changes"].append(
        {
            "resourceId": endpoint.GROUP + "/providers/Microsoft.Authorization/roleAssignments/new",
            "changeType": "Create",
        }
    )
    with pytest.raises(endpoint.GateError):
        endpoint.inspect_plan(actual, current, expected)
    actual = plan(current, expected)
    actual["changes"][0]["after"] = "[parameters('snapshot').properties]"
    with pytest.raises(endpoint.GateError):
        endpoint.inspect_plan(actual, current, expected)


@pytest.mark.parametrize(
    "field",
    [
        "before",
        "after",
        "delta",
        "unsupportedReason",
        "deploymentId",
        "extension",
        "identifiers",
        "symbolicName",
        "unknown",
    ],
)
def test_scope_preview_rejects_non_null_optional_fields(raw, field):
    current = endpoint.snapshot(raw)
    expected = endpoint.desired(current, "new")
    actual = plan(current, expected)
    actual["changes"][0][field] = "unreviewed"
    with pytest.raises(endpoint.GateError):
        endpoint.inspect_plan(actual, current, expected)


@pytest.mark.parametrize(
    "mutation", ["error", "diagnostics", "potentialChanges", "missing", "empty"]
)
def test_scope_preview_rejects_diagnostics_and_missing_data(raw, mutation):
    current = endpoint.snapshot(raw)
    expected = endpoint.desired(current, "new")
    actual = plan(current, expected)
    if mutation == "missing":
        del actual["changes"]
    elif mutation == "empty":
        actual["changes"] = []
    else:
        actual[mutation] = ["unreviewed"]
    with pytest.raises(endpoint.GateError):
        endpoint.inspect_plan(actual, current, expected)


def test_drift_stops_before_any_put(raw, monkeypatch):
    reads = iter([raw, {**raw, "systemData": {"lastModifiedAt": "later"}}])
    monkeypatch.setattr(endpoint, "verify_project", lambda: None)
    monkeypatch.setattr(endpoint, "get_app", lambda: next(reads))
    monkeypatch.setattr(endpoint, "deployment_body", lambda *a: {})
    monkeypatch.setattr(endpoint, "preview", lambda *a: {})
    monkeypatch.setattr(endpoint, "inspect_plan", lambda *a: {})
    monkeypatch.setattr(endpoint, "rest", lambda *a: pytest.fail("No ARM mutation allowed"))
    with pytest.raises(endpoint.GateError, match="Baseline changed"):
        endpoint.run(
            argparse.Namespace(
                expect_fingerprint=endpoint.fingerprint(raw), remove=False, apply=True
            )
        )


def test_failed_real_guard_stops_before_app_deployment(raw, monkeypatch):
    monkeypatch.setattr(endpoint, "verify_project", lambda: None)
    monkeypatch.setattr(endpoint, "get_app", lambda: raw)
    monkeypatch.setattr(endpoint, "deployment_body", lambda *a: {})
    monkeypatch.setattr(endpoint, "preview", lambda *a: {})
    monkeypatch.setattr(endpoint, "inspect_plan", lambda *a: {})
    monkeypatch.setattr(endpoint, "rest", lambda *a: pytest.fail("No app deployment allowed"))

    def failed_guard(*args):
        raise endpoint.GateError("Real guard failed")

    monkeypatch.setattr(endpoint, "validate_guard", failed_guard)
    with pytest.raises(endpoint.GateError, match="Real guard failed"):
        endpoint.run(
            argparse.Namespace(
                expect_fingerprint=endpoint.fingerprint(raw),
                remove=False,
                apply=True,
                validate_guard=False,
            )
        )


def test_project_endpoint_requires_exact_arm_contract(monkeypatch):
    monkeypatch.setattr(
        endpoint,
        "rest",
        lambda *a: {
            "id": endpoint.PROJECT,
            "properties": {
                "provisioningState": "Succeeded",
                "endpoints": {"AI Foundry API": endpoint.ENDPOINT + "?other=project"},
            },
        },
    )
    with pytest.raises(endpoint.GateError, match="endpoint mismatch"):
        endpoint.verify_project()


@pytest.fixture(scope="module")
def compiled():
    return endpoint.command(
        ["az", "bicep", "build", "--file", str(endpoint.HERE / "infra/main.bicep"), "--stdout"]
    )


def test_compiled_projection_and_literal_escaping(raw, monkeypatch, compiled):
    template = copy.deepcopy(compiled)
    monkeypatch.setattr(endpoint, "command", lambda *a: template)
    raw["properties"]["template"]["containers"][0]["env"][1][
        "value"
    ] = "[literal-not-an-expression]"
    current = endpoint.snapshot(raw)
    body = endpoint.deployment_body(current, "new", "persist")
    preview = endpoint.scope_preview_template(body)
    assert (
        preview["parameters"]["snapshot"]["defaultValue"]["properties"]["template"]["containers"][
            0
        ]["env"][1]["value"]
        == "[[literal-not-an-expression]"
    )
    assert preview["resources"] == template["resources"]
    assert body["properties"]["parameters"]["snapshot"]["value"] == endpoint.desired(current, "new")
    assert template["parameters"]["snapshot"]["type"] == "secureObject"
    body["properties"]["template"]["resources"][0]["properties"] = "[reference('other')]"
    with pytest.raises(endpoint.GateError):
        endpoint.scope_preview_template(body)


def test_compiled_guard_is_resource_input_without_self_dependency_or_outputs(compiled):
    endpoint.verify_compiled(compiled)
    resource = compiled["resources"][0]
    assert "dependsOn" not in resource and "condition" not in resource
    assert not compiled.get("outputs") and not compiled.get("functions")
    assert resource["properties"].startswith("[if(")
    assert "json(concat('ENDPOINT_SECRET_INVENTORY_MISMATCH', take(resourceGroup().id, 0)))" in (
        resource["properties"]
    )
    lookup = "listSecrets(resourceId('Microsoft.App/containerApps', 'fcag-dev-app'), '2025-01-01')"
    assert lookup in resource["properties"]
    assert "reference(" not in json.dumps(compiled)
    for key in ("location", "tags", "identity"):
        assert resource[key] == f"[parameters('snapshot').{key}]"


@pytest.mark.parametrize("mutation", ["output", "dependency", "unguarded", "extra-resource"])
def test_compiled_contract_rejects_any_executable_change(compiled, mutation):
    candidate = copy.deepcopy(compiled)
    if mutation == "output":
        candidate["outputs"] = {"secret": {"type": "array", "value": "[listSecrets('x', 'y')]"}}
    elif mutation == "dependency":
        candidate["resources"][0]["dependsOn"] = ["fcag-dev-app"]
    elif mutation == "unguarded":
        candidate["resources"][0]["properties"] = "[parameters('snapshot').properties]"
    else:
        candidate["resources"].append({"type": "Microsoft.Authorization/roleAssignments"})
    with pytest.raises(endpoint.GateError):
        endpoint.verify_compiled(candidate)


def test_scope_preview_never_requests_payloads(raw, monkeypatch, compiled):
    monkeypatch.setattr(endpoint, "command", lambda *a: copy.deepcopy(compiled))
    body = endpoint.deployment_body(endpoint.snapshot(raw), "new", "persist")
    calls = []
    monkeypatch.setattr(endpoint, "command", lambda *a: calls.append(a))
    endpoint.preview(body)
    assert calls[0][0][calls[0][0].index("--result-format") + 1] == "ResourceIdOnly"
    assert "FullResourcePayloads" not in json.dumps(calls)


def test_resource_free_diagnostic_uses_actual_compiled_predicate(raw, monkeypatch, compiled):
    monkeypatch.setattr(endpoint, "command", lambda *a: copy.deepcopy(compiled))
    body = endpoint.deployment_body(endpoint.snapshot(raw), "new", "persist")
    original = copy.deepcopy(body)
    for invalid in (False, True):
        probe = endpoint.guard_diagnostic_body(body, invalid)
        template = probe["properties"]["template"]
        assert template["resources"] == []
        assert template["variables"] == compiled["variables"]
        assert template["parameters"] == compiled["parameters"]
        assert list(template["outputs"]) == ["inventoryValid"]
        output = template["outputs"]["inventoryValid"]
        assert output["type"] == "bool"
        predicate = output["value"][1:-1]
        assert compiled["resources"][0]["properties"].startswith(f"[if({predicate}, ")
        assert "listSecrets(" in predicate
        secrets = probe["properties"]["parameters"]["snapshot"]["value"]["properties"][
            "configuration"
        ]["secrets"]
        assert (len(secrets) != len({s["name"] for s in secrets})) is invalid
    assert body == original


@pytest.mark.parametrize(
    "bad_output",
    [
        None,
        {},
        {"extra": True},
        {"inventoryValid": {"type": "Bool", "value": 1}},
        {"inventoryValid": {"type": "String", "value": "true"}},
        {"inventoryValid": {"type": "Bool", "value": False}},
        {"inventoryValid": {"type": "Bool", "value": True, "metadata": "no"}},
    ],
)
def test_guard_diagnostic_rejects_non_boolean_or_unexpected_outputs(
    raw, monkeypatch, compiled, bad_output
):
    monkeypatch.setattr(endpoint, "command", lambda *a: copy.deepcopy(compiled))
    body = endpoint.deployment_body(endpoint.snapshot(raw), "new", "persist")
    monkeypatch.setattr(endpoint, "get_app", lambda: raw)
    monkeypatch.setattr(
        endpoint,
        "rest",
        lambda *a: {"properties": {"provisioningState": "Succeeded", "outputs": bad_output}},
    )
    with pytest.raises(endpoint.GateError, match="strict boolean"):
        endpoint.validate_guard(body, endpoint.fingerprint(raw))


def test_guard_diagnostic_valid_invalid_and_baseline(raw, monkeypatch, compiled, capsys):
    monkeypatch.setattr(endpoint, "command", lambda *a: copy.deepcopy(compiled))
    body = endpoint.deployment_body(endpoint.snapshot(raw), "new", "persist")
    monkeypatch.setattr(endpoint, "get_app", lambda: raw)
    calls = []

    def rest(method, path, payload):
        calls.append(payload)
        assert method == "put"
        assert payload["properties"]["template"]["resources"] == []
        return {
            "properties": {
                "provisioningState": "Succeeded",
                "outputs": {"inventoryValid": {"type": "Bool", "value": len(calls) == 1}},
            }
        }

    monkeypatch.setattr(endpoint, "rest", rest)
    endpoint.validate_guard(body, endpoint.fingerprint(raw))
    assert len(calls) == 2
    evidence = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [e["inventoryValid"] for e in evidence] == [True, False]
    assert all(e["resources"] == 0 for e in evidence)
    monkeypatch.setattr(endpoint, "get_app", lambda: {**raw, "etag": "changed"})
    with pytest.raises(endpoint.GateError, match="Baseline changed"):
        endpoint.validate_guard(body, endpoint.fingerprint(raw))
    assert len(calls) == 2


def evaluate_guarded_properties(compiled, snapshot, live):
    """Small offline interpreter for this pinned ARM expression, not Azure proof."""

    def parse(text):
        tokens = iter(re.findall(r"'(?:[^']|'')*'|[a-zA-Z_][a-zA-Z_0-9]*|\d+|[().,]", text))
        token = next(tokens, None)

        def consume(expected=None):
            nonlocal token
            result = token
            if expected is not None:
                assert token == expected
            token = next(tokens, None)
            return result

        def node():
            value = consume()
            if value.startswith("'"):
                result = ("literal", value[1:-1].replace("''", "'"))
            elif value.isdigit():
                result = ("literal", int(value))
            else:
                consume("(")
                args = []
                if token != ")":
                    args.append(node())
                    while token == ",":
                        consume(",")
                        args.append(node())
                consume(")")
                result = ("call", value, args)
            while token == ".":
                consume(".")
                result = ("get", result, consume())
            return result

        result = node()
        assert token is None
        return result

    def union(*values):
        if isinstance(values[0], dict):
            result = copy.deepcopy(values[0])
            for other in values[1:]:
                for key, value in other.items():
                    result[key] = (
                        union(result[key], value)
                        if isinstance(result.get(key), dict) and isinstance(value, dict)
                        else value
                    )
            return result
        return list(dict.fromkeys(v for value in values for v in value))

    def evaluate(node, bindings=None):
        bindings = bindings or {}
        if node[0] == "literal":
            return node[1]
        if node[0] == "get":
            return evaluate(node[1], bindings)[node[2]]
        _, name, args = node
        if name == "if":
            return evaluate(args[1] if evaluate(args[0], bindings) else args[2], bindings)
        if name == "lambda":
            return lambda value: evaluate(args[1], {**bindings, args[0][1]: value})
        values = [evaluate(arg, bindings) for arg in args]
        if name == "variables":
            value = compiled["variables"][values[0]]
            return evaluate(parse(value[1:-1]), bindings) if isinstance(value, str) else value
        functions = {
            "parameters": lambda key: snapshot,
            "lambdaVariables": lambda key: bindings[key],
            "listSecrets": lambda *a: {"value": live},
            "resourceId": lambda *a: endpoint.APP,
            "resourceGroup": lambda: {"id": endpoint.GROUP},
            "map": lambda values, fn: list(map(fn, values)),
            "filter": lambda values, fn: list(filter(fn, values)),
            "items": lambda value: [{"key": k, "value": v} for k, v in value.items()],
            "length": len,
            "union": union,
            "equals": lambda a, b: type(a) is type(b) and a == b,
            "and": lambda a, b: a and b,
            "or": lambda a, b: a or b,
            "not": lambda a: not a,
            "empty": lambda a: a is None or a == "" or a == [] or a == {},
            "contains": lambda a, b: b in a,
            "string": lambda a: "" if a is None else a if isinstance(a, str) else json.dumps(a),
            "coalesce": lambda *a: next(value for value in a if value is not None),
            "tryGet": lambda a, b: a.get(b),
            "first": lambda a: a[0],
            "createObject": lambda *a: dict(zip(a[::2], a[1::2])),
            "concat": lambda *a: "".join(a),
            "take": lambda a, b: a[:b],
            "json": json.loads,
        }
        return functions[name](*values)

    return evaluate(parse(compiled["resources"][0]["properties"][1:-1]))


def test_compiled_native_identity_and_kv_metadata_preservation(raw, compiled):
    current = endpoint.snapshot(raw)
    metadata = current["properties"]["configuration"]["secrets"]
    metadata.append({"name": "native"})
    live = [{**metadata[0], "value": "FAKE-KV-RESOLUTION"}, {"name": "native", "value": "FAKE"}]
    result = evaluate_guarded_properties(compiled, current, live)
    expected = copy.deepcopy(current["properties"])
    expected["configuration"]["secrets"][1]["value"] = "FAKE"
    assert result == expected
    assert "value" not in result["configuration"]["secrets"][0]


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "extra",
        "duplicate",
        "rename",
        "classification",
        "kv-url",
        "kv-identity",
        "unknown-live",
        "missing-value",
        "null-value",
        "object-value",
        "duplicate-metadata",
        "unknown-metadata",
    ],
)
def test_compiled_inventory_guard_fails_before_resource_input_exists(raw, compiled, mutation):
    current = endpoint.snapshot(raw)
    metadata = current["properties"]["configuration"]["secrets"]
    metadata.append({"name": "native"})
    live = [copy.deepcopy(metadata[0]), {"name": "native", "value": "FAKE"}]
    if mutation == "missing":
        live.pop()
    elif mutation == "extra":
        live.append({"name": "extra", "value": "FAKE"})
    elif mutation == "duplicate":
        live.append(copy.deepcopy(live[0]))
    elif mutation == "rename":
        live[1]["name"] = "other"
    elif mutation == "classification":
        live[0] = {"name": metadata[0]["name"], "value": "FAKE"}
    elif mutation == "kv-url":
        live[0]["keyVaultUrl"] += "/other"
    elif mutation == "kv-identity":
        live[0]["identity"] = "other"
    elif mutation == "unknown-live":
        live[1]["future"] = None
    elif mutation == "missing-value":
        live[1].pop("value")
    elif mutation == "null-value":
        live[1]["value"] = None
    elif mutation == "object-value":
        live[1]["value"] = {}
    elif mutation == "duplicate-metadata":
        metadata.append(copy.deepcopy(metadata[0]))
    else:
        metadata[0]["future"] = None
    writes = []
    with pytest.raises(ValueError):
        properties = evaluate_guarded_properties(compiled, current, live)
        writes.append(properties)
    assert writes == []


def test_operator_has_no_root_hooks_azd_env_or_secret_lookup():
    source = (ROOT / "deployments/dev-endpoint/persist_endpoint.py").read_text()
    assert "azd" not in source.replace("No root azd hooks", "").replace("azd env", "")
    assert "listSecrets" not in source
    assert "get-access-token" not in source
    assert 'open(".env' not in source and "load_dotenv" not in source
    assert not (ROOT / "deployments/dev-endpoint/azure.yaml").exists()
