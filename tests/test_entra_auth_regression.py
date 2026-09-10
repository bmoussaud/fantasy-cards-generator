"""Structural ARM regression coverage for Entra redeployments.

Regenerate infra/main.json with az bicep build before running these tests.
Live guard enforcement and login smoke tests remain separate deployment gates.
"""

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def arm() -> dict:
    return json.loads((REPO_ROOT / "infra/main.json").read_text())


def test_existing_auth_is_the_default_when_registration_is_skipped(arm: dict) -> None:
    mode = arm["parameters"]["entraAuthMode"]
    assert mode["defaultValue"] == "existing"
    assert set(mode["allowedValues"]) == {"existing", "disabled"}
    assert arm["parameters"]["deployEntraAppRegistration"]["defaultValue"] is True
    assert arm["parameters"]["entraClientIdOverride"]["defaultValue"] == ""


def test_guard_only_runs_for_existing_auth_without_registration(arm: dict) -> None:
    guard = arm["resources"]["entraAuthGuard"]
    assert guard["type"] == "Microsoft.Resources/deployments"
    assert guard["condition"] == (
        "[and(not(parameters('deployEntraAppRegistration')), "
        "equals(parameters('entraAuthMode'), 'existing'))]"
    )


def test_guard_trims_input_before_enforcing_minimum_length(arm: dict) -> None:
    guard = arm["resources"]["entraAuthGuard"]["properties"]
    assert guard["parameters"]["entraClientIdOverride"] == {
        "value": "[trim(parameters('entraClientIdOverride'))]"
    }
    parameter = guard["template"]["parameters"]["entraClientIdOverride"]
    assert parameter["type"] == "string"
    assert parameter["minLength"] == 1
    assert "defaultValue" not in parameter
    assert guard["template"]["outputs"]["validatedClientId"]["value"] == (
        "[parameters('entraClientIdOverride')]"
    )


def test_container_app_waits_for_validated_client_id(arm: dict) -> None:
    container_apps = arm["resources"]["containerApps"]
    assert "entraAuthGuard" in container_apps["dependsOn"]
    expression = container_apps["properties"]["parameters"]["entraClientId"]
    assert expression.startswith("[if(parameters('deployEntraAppRegistration'),")
    assert "reference('appRegistration').outputs.appId.value" in expression
    assert "equals(parameters('entraAuthMode'), 'existing')" in expression
    assert "reference('entraAuthGuard').outputs.validatedClientId.value" in expression
    assert "createObject('value', '')" in expression


@pytest.mark.parametrize("name", ["entraRedirectUri", "entraPostLogoutRedirectUri"])
def test_redirects_follow_resolved_client_id(arm: dict, name: str) -> None:
    expression = arm["resources"]["containerApps"]["properties"]["parameters"][name]
    assert "reference('entraAuthGuard').outputs.validatedClientId.value" in expression
    assert "empty(" in expression
    assert "containerAppsEnvironment" in expression


@pytest.mark.parametrize("name", ["entraClientId", "ENTRA_CLIENT_ID"])
def test_outputs_expose_resolved_existing_registration(arm: dict, name: str) -> None:
    expression = arm["outputs"][name]["value"]
    assert "reference('entraAuthGuard').outputs.validatedClientId.value" in expression
    assert "reference('appRegistration').outputs.appId.value" in expression


def test_output_exposes_authoritative_registration_ownership(arm: dict) -> None:
    output = arm["outputs"]["ENTRA_APP_REGISTRATION_MANAGED"]
    assert output["type"] == "bool"
    assert output["value"] == "[parameters('deployEntraAppRegistration')]"


def test_azd_exposes_explicit_auth_intent_and_client_override() -> None:
    parameters = json.loads((REPO_ROOT / "infra/main.parameters.json").read_text())["parameters"]
    assert parameters["entraAuthMode"]["value"] == "${ENTRA_AUTH_MODE=existing}"
    assert parameters["entraClientIdOverride"]["value"] == "${ENTRA_CLIENT_ID_OVERRIDE=}"


def test_direct_deployment_docs_do_not_pass_azd_placeholders_to_azure_cli() -> None:
    docs = (REPO_ROOT / "docs/auth-setup.md").read_text()
    section = docs.split("## Direct redeploy", 1)[1].split("## Rotating", 1)[0]
    assert "--parameters infra/main.parameters.json" not in section
    assert "--parameters @resolved.parameters.json" in section
