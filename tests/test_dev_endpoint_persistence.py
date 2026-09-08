"""Offline JSON-transform and fail-closed deployment gates; no Azure/model calls."""

import argparse
import copy
import importlib.util
import json
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


def test_native_secret_cannot_be_replayed_or_silently_dropped(raw):
    raw["properties"]["configuration"]["secrets"] = [{"name": "native-secret"}]
    current = endpoint.snapshot(raw)
    assert current["properties"]["configuration"]["secrets"] == [{"name": "native-secret"}]
    with pytest.raises(endpoint.GateError, match="Native ACA secret"):
        endpoint.deployment_body(current, "endpoint-reviewed", "persist")


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
                "before": current,
                "after": expected,
            }
        ],
    }


def test_full_payload_plan_with_ignored_unowned_resource(raw):
    current = endpoint.snapshot(raw)
    expected = endpoint.desired(current, "new")
    actual = plan(current, expected)
    actual["changes"].append(
        {
            "resourceId": endpoint.GROUP + "/providers/Microsoft.Network/virtualNetworks/existing",
            "changeType": "Ignore",
        }
    )
    report = endpoint.inspect_plan(actual, current, expected)
    assert report["fullBeforeAfterMatched"]
    assert report["ignoredResources"] == 1
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
        endpoint.inspect_plan(plan(current, altered), current, expected)


@pytest.mark.parametrize("kind", ["Create", "Delete", "Unsupported", "Deploy", "NoChange"])
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
    actual = plan(current, {**expected, "properties": "[parameters('snapshot').properties]"})
    with pytest.raises((endpoint.GateError, TypeError)):
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


def test_compiled_projection_and_literal_escaping(raw, monkeypatch):
    template = {
        "parameters": {"snapshot": {"type": "secureObject"}},
        "resources": [
            {
                "type": "Microsoft.App/containerApps",
                "name": "fcag-dev-app",
                "apiVersion": endpoint.API,
                **{
                    k: f"[parameters('snapshot').{k}]"
                    for k in ("location", "tags", "identity", "properties")
                },
            }
        ],
    }
    monkeypatch.setattr(endpoint, "command", lambda *a: template)
    raw["properties"]["template"]["containers"][0]["env"][1][
        "value"
    ] = "[literal-not-an-expression]"
    current = endpoint.snapshot(raw)
    body = endpoint.deployment_body(current, "new", "persist")
    preview = endpoint.materialized_preview(body)
    assert "parameters" not in preview
    assert (
        preview["resources"][0]["properties"]["template"]["containers"][0]["env"][1]["value"]
        == "[[literal-not-an-expression]"
    )
    assert body["properties"]["parameters"]["snapshot"]["value"] == endpoint.desired(current, "new")
    assert template["parameters"]["snapshot"]["type"] == "secureObject"
    body["properties"]["template"]["resources"][0]["properties"] = "[reference('other')]"
    with pytest.raises(endpoint.GateError):
        endpoint.materialized_preview(body)


def test_operator_has_no_root_hooks_azd_env_or_secret_lookup():
    source = (ROOT / "deployments/dev-endpoint/persist_endpoint.py").read_text()
    assert "azd" not in source.replace("No root azd hooks", "").replace("azd env", "")
    assert "listSecrets" not in source
    assert "get-access-token" not in source
    assert 'open(".env' not in source and "load_dotenv" not in source
    assert not (ROOT / "deployments/dev-endpoint/azure.yaml").exists()
