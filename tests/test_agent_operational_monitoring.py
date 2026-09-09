"""Compiled monitoring contracts and operational runbook invariants."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT = ROOT / "deployments/card-orchestrator"
AGENT_INFRA = DEPLOYMENT / "infra"
AGENT_MONITORING = AGENT_INFRA / "modules/agent-monitoring.bicep"


def _compile(path: Path) -> dict:
    result = subprocess.run(
        ["az", "bicep", "build", "--file", str(path), "--stdout"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _resource_types(template: dict) -> set[str]:
    return {resource["type"] for resource in template["resources"]}


def test_agent_monitoring_compiles_to_expected_azure_native_resources() -> None:
    template = _compile(AGENT_MONITORING)

    assert _resource_types(template) == {
        "Microsoft.Insights/actionGroups",
        "Microsoft.Insights/scheduledQueryRules",
        "Microsoft.Insights/workbooks",
    }
    assert template["parameters"]["environmentName"]["allowedValues"] == ["dev", "prod"]
    for name in (
        "containerAppName",
        "appInsightsResourceId",
        "logAnalyticsWorkspaceResourceId",
    ):
        assert "defaultValue" not in template["parameters"][name]


def test_agent_infra_requires_monitoring_and_deploys_module_unconditionally() -> None:
    template = _compile(AGENT_INFRA / "main.bicep")
    parameters = template["parameters"]
    assert parameters["environmentName"]["allowedValues"] == ["dev", "prod"]
    assert "defaultValue" not in parameters["appInsightsResourceId"]
    assert "defaultValue" not in parameters["logAnalyticsWorkspaceResourceId"]

    monitoring_deployment = next(
        resource
        for resource in template["resources"]
        if resource["type"] == "Microsoft.Resources/deployments"
        and "agent-monitoring" in resource["name"]
    )
    assert "condition" not in monitoring_deployment

    parameter_file = json.loads((AGENT_INFRA / "main.parameters.json").read_text())
    values = parameter_file["parameters"]
    assert values["appInsightsResourceId"]["value"] == "${AZURE_APP_INSIGHTS_RESOURCE_ID}"
    assert (
        values["logAnalyticsWorkspaceResourceId"]["value"]
        == "${AZURE_LOG_ANALYTICS_WORKSPACE_RESOURCE_ID}"
    )


def test_root_infra_links_application_insights_to_foundry_without_azd_secret_output() -> None:
    template = _compile(ROOT / "infra/main.bicep")
    assert "APPLICATIONINSIGHTS_CONNECTION_STRING" not in template["outputs"]

    foundry_module = _compile(ROOT / "infra/modules/ai-foundry.bicep")
    resource_types = _resource_types(foundry_module)
    assert "Microsoft.CognitiveServices/accounts/connections" in resource_types
    assert "Microsoft.CognitiveServices/accounts/projects/connections" in resource_types
    assert foundry_module["parameters"]["appInsightsConnectionString"]["type"] == "securestring"

    connection_resources = [
        resource
        for resource in foundry_module["resources"]
        if resource["type"]
        in {
            "Microsoft.CognitiveServices/accounts/connections",
            "Microsoft.CognitiveServices/accounts/projects/connections",
        }
    ]
    assert len(connection_resources) == 2
    connection_properties = foundry_module["variables"]["appInsightsConnectionProperties"]
    assert connection_properties["category"] == "AppInsights"
    assert connection_properties["authType"] == "ApiKey"
    assert connection_properties["target"] == "[parameters('appInsightsResourceId')]"
    for resource in connection_resources:
        assert resource["properties"] == "[variables('appInsightsConnectionProperties')]"


def test_manifest_enables_custom_metrics_without_redeclaring_reserved_values() -> None:
    manifest = json.loads((DEPLOYMENT / "azure.yaml").read_text())
    environment = {
        item["name"]: item["value"]
        for item in manifest["services"]["card-orchestrator"]["environmentVariables"]
    }
    assert environment["TELEMETRY_ENABLED"] == "true"
    assert environment["TELEMETRY_ENVIRONMENT"] == "${AZURE_ENV_NAME}"
    assert "APPLICATIONINSIGHTS_CONNECTION_STRING" not in environment
    assert "OTEL_SERVICE_NAME" not in environment


def test_workbook_and_alerts_query_only_emitted_bounded_metrics() -> None:
    source = AGENT_MONITORING.read_text()
    assert 'Properties["fcg.agent_version"]' in source
    assert "fcg.agent_version" in (ROOT / "app/telemetry.py").read_text()
    assert "sum(Sum), MeasurementCount=sum(ItemCount)" in source
    assert "sum(Sum), AttemptCount=sum(ItemCount)" in source
    assert "Count=count()" not in source

    for metric in (
        "fcg.generation.requests",
        "fcg.generation.duration",
        "fcg.dependency.attempts",
        "fcg.dependency.duration",
        "fcg.moderation.decisions",
    ):
        assert metric in source
    alert_source = source[source.index("var invocationAdverseQuery") :]
    assert "AppDependencies" not in alert_source
    assert "AppExceptions" not in alert_source
    assert "ResultCode" not in alert_source


def test_monitoring_queries_are_service_scoped_and_content_free() -> None:
    source = AGENT_MONITORING.read_text()
    assert "AppRoleName == '{AgentService}'" in source
    assert "AppRoleName == '{0}'" in source
    for forbidden in (
        "request_body",
        "response_body",
        "PromptText",
        "ResponseBody",
        'Properties["prompt"]',
        'Properties["session',
        "user_content",
        'Properties["token',
    ):
        assert forbidden.lower() not in source.lower()


def test_runbook_is_restore_first_and_keeps_web_deployment_immutable() -> None:
    runbook = (ROOT / "docs/agent-operational-ownership.md").read_text()
    selector_patch = runbook.index('\\"version_selection_rules\\"')
    delete_version = runbook.index("delete_version")
    assert selector_patch < delete_version
    assert "/agents/${AGENT_NAME}" in runbook
    assert "/agents/${AGENT_NAME}/versions/${BAD_VERSION}" in runbook
    assert "project_client.agents.update_details(" in runbook
    assert "project_client.agents.delete_version(" in runbook
    assert "azd ai agent endpoint show --output json" in runbook
    assert "web ACA" in runbook
    assert "no azure deployment" in runbook.lower()


def test_prod_path_is_executable_but_requires_explicit_second_approval() -> None:
    launcher = (DEPLOYMENT / "deploy.py").read_text()
    runbook = (ROOT / "docs/agent-operational-ownership.md").read_text()
    assert 'choices=("dev", "prod")' in launcher
    assert "--approve-prod" in launcher
    assert "--approve-prod" in (DEPLOYMENT / "aca_identity_probe.py").read_text()
    assert "--environment prod" in runbook
    assert "--approve-prod" in runbook
