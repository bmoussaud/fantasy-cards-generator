"""Tests for agent operational monitoring: Bicep wiring, KQL invariants, deployment contracts."""

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
AGENT_DEPLOYMENT = ROOT / "deployments/card-orchestrator"
AGENT_INFRA = AGENT_DEPLOYMENT / "infra"
AGENT_MODULE = AGENT_INFRA / "modules/agent-monitoring.bicep"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bicep_source(path: Path) -> str:
    return path.read_text()


def _agent_monitoring_source() -> str:
    return _bicep_source(AGENT_MODULE)


def _card_orchestrator_infra_source() -> str:
    return _bicep_source(AGENT_INFRA / "main.bicep")


def _root_infra_source() -> str:
    return _bicep_source(ROOT / "infra/main.bicep")


def _agent_azure_yaml() -> dict:
    return json.loads((AGENT_DEPLOYMENT / "azure.yaml").read_text())


# ---------------------------------------------------------------------------
# Bicep compilation
# ---------------------------------------------------------------------------


def test_agent_monitoring_bicep_compiles() -> None:
    result = subprocess.run(
        ["az", "bicep", "build", "--file", str(AGENT_MODULE), "--stdout"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Bicep build failed:\n{result.stderr}"


def test_card_orchestrator_infra_main_bicep_compiles() -> None:
    result = subprocess.run(
        ["az", "bicep", "build", "--file", str(AGENT_INFRA / "main.bicep"), "--stdout"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Bicep build failed:\n{result.stderr}"


def test_root_infra_main_bicep_still_compiles() -> None:
    result = subprocess.run(
        ["az", "bicep", "build", "--file", str(ROOT / "infra/main.bicep"), "--stdout"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Bicep build failed:\n{result.stderr}"


# ---------------------------------------------------------------------------
# Module parameters and defaults
# ---------------------------------------------------------------------------


def test_agent_monitoring_has_required_params() -> None:
    source = _agent_monitoring_source()
    for param in (
        "location",
        "environmentName",
        "containerAppName",
        "appInsightsResourceId",
        "logAnalyticsWorkspaceResourceId",
    ):
        assert f"param {param}" in source, f"Missing required param: {param}"


def test_agent_monitoring_alerts_disabled_by_default() -> None:
    source = _agent_monitoring_source()
    assert "param enableAlerts bool = false" in source


def test_agent_monitoring_action_group_receivers_default_empty() -> None:
    source = _agent_monitoring_source()
    assert "param actionGroupEmailReceivers array = []" in source
    assert "param actionGroupWebhookReceivers array = []" in source


def test_agent_monitoring_default_service_name_is_card_orchestrator() -> None:
    source = _agent_monitoring_source()
    assert "param agentServiceName string = 'card-orchestrator'" in source


def test_agent_monitoring_is_dev_prod_isolated() -> None:
    source = _agent_monitoring_source()
    assert "@allowed([\n  'dev'\n  'prod'\n])" in source
    assert "environmentName" in source
    # Resource names include environmentName for isolation.
    assert "resourceToken = 'fcg-agent-${environmentName}'" in source


# ---------------------------------------------------------------------------
# Alert KQL invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "alert_name",
    [
        "agent-invocation-adverse",
        "agent-model-throttles",
        "agent-exceptions",
        "agent-container-restarts",
    ],
)
def test_alert_names_are_present(alert_name: str) -> None:
    source = _agent_monitoring_source()
    assert f"name: '{alert_name}'" in source


def test_alert_kql_uses_agent_service_name_format_param() -> None:
    source = _agent_monitoring_source()
    # All alert queries are constructed via format() with agentServiceName or containerAppName.
    assert "format('''\nAppMetrics\n| where AppRoleName ==" in source
    assert "format('''\nAppDependencies\n| where AppRoleName ==" in source
    assert "format('''\nAppExceptions\n| where AppRoleName ==" in source
    assert "format('''\nContainerAppSystemLogs_CL" in source


def test_model_throttle_alert_uses_result_code_429() -> None:
    source = _agent_monitoring_source()
    assert 'ResultCode == "429"' in source


def test_alert_kql_excludes_content_fields() -> None:
    """Alert queries must not select prompt, response, card, or user-content fields."""
    source = _agent_monitoring_source()
    forbidden_patterns = [
        "prompt",
        "response_body",
        "card_text",
        "request_body",
        "user_content",
        'Properties["text"]',
        "Properties['text']",
    ]
    # KQL in alert variables only — workbook queries are evaluated separately.
    alert_section_start = source.index("var invocationAdverseQuery")
    alert_section_end = source.index("var alertDefinitions")
    alert_kql_section = source[alert_section_start:alert_section_end]
    for pattern in forbidden_patterns:
        assert (
            pattern.lower() not in alert_kql_section.lower()
        ), f"Alert KQL must not reference content field: {pattern!r}"


def test_workbook_kql_excludes_content_fields() -> None:
    """Workbook queries must not select prompt, response, or raw content columns."""
    source = _agent_monitoring_source()
    workbook_start = source.index("var workbookData")
    workbook_end = source.index("resource agentWorkbook")
    workbook_section = source[workbook_start:workbook_end]
    # These column names must not appear as KQL project/extend targets.
    forbidden_columns = ["response_body", "card_text", "request_body", "PromptText", "ResponseBody"]
    for col in forbidden_columns:
        assert col not in workbook_section, f"Workbook KQL must not select content column: {col!r}"
    # Prompt text as a KQL property access must not appear.
    assert 'Properties["prompt"]' not in workbook_section
    assert "| project prompt" not in workbook_section.lower()


def test_workbook_queries_filter_on_app_role_name() -> None:
    source = _agent_monitoring_source()
    workbook_start = source.index("var workbookData")
    workbook_end = source.index("resource agentWorkbook")
    workbook_section = source[workbook_start:workbook_end]
    # Agent service name param is injected into workbook queries.
    assert "AppRoleName == '{AgentService}'" in workbook_section


def test_workbook_has_version_and_session_parameters() -> None:
    source = _agent_monitoring_source()
    workbook_start = source.index("var workbookData")
    workbook_end = source.index("resource agentWorkbook")
    workbook_section = source[workbook_start:workbook_end]
    assert "'AgentVersion'" in workbook_section
    assert "'SessionId'" in workbook_section


def test_alert_queries_use_breach_projection_pattern() -> None:
    source = _agent_monitoring_source()
    # Each alert query must project a Breach=1 column so the scheduled query rule
    # can use Count > 0 as the threshold.
    alert_section_start = source.index("var invocationAdverseQuery")
    alert_section_end = source.index("var alertDefinitions")
    alert_kql = source[alert_section_start:alert_section_end]
    assert (
        alert_kql.count("| project Breach=1") >= 4
    ), "Every alert query must end with '| project Breach=1'"


def test_alert_rules_use_count_greater_than_zero_threshold() -> None:
    source = _agent_monitoring_source()
    alert_rules_section = source[source.index("resource alertRules") :]
    assert "timeAggregation: 'Count'" in alert_rules_section
    assert "operator: 'GreaterThan'" in alert_rules_section
    assert "threshold: 0" in alert_rules_section


def test_alert_rules_skip_query_validation() -> None:
    """skipQueryValidation must be true to allow format()-constructed KQL."""
    source = _agent_monitoring_source()
    assert "skipQueryValidation: true" in source


# ---------------------------------------------------------------------------
# No availability webtest (Foundry-internal /readiness)
# ---------------------------------------------------------------------------


def test_no_availability_webtest_in_agent_monitoring() -> None:
    source = _agent_monitoring_source()
    assert "Microsoft.Insights/webtests" not in source, (
        "Agent monitoring must not define a public availability webtest; "
        "the /readiness endpoint is Foundry-internal."
    )


# ---------------------------------------------------------------------------
# main.bicep wiring
# ---------------------------------------------------------------------------


def test_card_orchestrator_infra_wires_agent_monitoring_module() -> None:
    source = _card_orchestrator_infra_source()
    assert "module agentMonitoring './modules/agent-monitoring.bicep'" in source
    assert "logAnalyticsWorkspaceResourceId: logAnalyticsWorkspaceResourceId" in source
    assert "appInsightsResourceId: appInsightsResourceId" in source
    assert "containerAppName: containerAppName" in source


def test_card_orchestrator_monitoring_module_is_conditional_on_resource_ids() -> None:
    source = _card_orchestrator_infra_source()
    # The module must be conditional so it does not fail when monitoring IDs are absent.
    conditional = (
        "= if (!empty(logAnalyticsWorkspaceResourceId)" " && !empty(appInsightsResourceId))"
    )
    assert conditional in source


def test_card_orchestrator_infra_has_monitoring_params() -> None:
    source = _card_orchestrator_infra_source()
    for param in (
        "logAnalyticsWorkspaceResourceId",
        "appInsightsResourceId",
        "enableAgentAlerts",
        "agentAlertEmailReceivers",
        "agentAlertWebhookReceivers",
    ):
        assert f"param {param}" in source, f"Missing param in card-orchestrator main.bicep: {param}"


def test_card_orchestrator_monitoring_params_default_safe() -> None:
    source = _card_orchestrator_infra_source()
    # Defaults must be empty/false so existing environments are not broken.
    assert "param logAnalyticsWorkspaceResourceId string = ''" in source
    assert "param appInsightsResourceId string = ''" in source
    assert "param enableAgentAlerts bool = false" in source
    assert "param agentAlertEmailReceivers array = []" in source
    assert "param agentAlertWebhookReceivers array = []" in source


# ---------------------------------------------------------------------------
# Parameters file
# ---------------------------------------------------------------------------


def test_card_orchestrator_parameters_include_monitoring_keys() -> None:
    params = json.loads((AGENT_INFRA / "main.parameters.json").read_text())["parameters"]
    for key in ("logAnalyticsWorkspaceResourceId", "appInsightsResourceId", "enableAgentAlerts"):
        assert key in params, f"Missing parameter in main.parameters.json: {key}"


def test_card_orchestrator_monitoring_params_use_safe_env_defaults() -> None:
    params = json.loads((AGENT_INFRA / "main.parameters.json").read_text())["parameters"]
    assert params["logAnalyticsWorkspaceResourceId"]["value"].endswith("=}")
    assert params["appInsightsResourceId"]["value"].endswith("=}")
    assert params["enableAgentAlerts"]["value"].endswith("=false}")


# ---------------------------------------------------------------------------
# azure.yaml telemetry wiring
# ---------------------------------------------------------------------------


def test_azure_yaml_injects_otel_service_name() -> None:
    service = _agent_azure_yaml()["services"]["card-orchestrator"]
    env_names = [e["name"] for e in service["environmentVariables"]]
    assert "OTEL_SERVICE_NAME" in env_names


def test_azure_yaml_otel_service_name_is_card_orchestrator() -> None:
    service = _agent_azure_yaml()["services"]["card-orchestrator"]
    env_map = {e["name"]: e["value"] for e in service["environmentVariables"]}
    assert env_map["OTEL_SERVICE_NAME"] == "card-orchestrator"


def test_azure_yaml_injects_app_insights_connection_string() -> None:
    service = _agent_azure_yaml()["services"]["card-orchestrator"]
    env_names = [e["name"] for e in service["environmentVariables"]]
    assert "APPLICATIONINSIGHTS_CONNECTION_STRING" in env_names


def test_azure_yaml_app_insights_connection_string_has_safe_default() -> None:
    service = _agent_azure_yaml()["services"]["card-orchestrator"]
    env_map = {e["name"]: e["value"] for e in service["environmentVariables"]}
    # Must use empty-string default so the agent starts without telemetry when not configured.
    expected = "${APPLICATIONINSIGHTS_CONNECTION_STRING=}"
    assert env_map["APPLICATIONINSIGHTS_CONNECTION_STRING"] == expected


# ---------------------------------------------------------------------------
# Root infra outputs for agent use
# ---------------------------------------------------------------------------


def test_root_infra_exposes_log_analytics_resource_id_for_agent() -> None:
    source = _root_infra_source()
    assert "output AZURE_LOG_ANALYTICS_WORKSPACE_RESOURCE_ID string" in source


def test_root_infra_exposes_app_insights_resource_id_for_agent() -> None:
    source = _root_infra_source()
    assert "output AZURE_APP_INSIGHTS_RESOURCE_ID string" in source


def test_root_infra_exposes_app_insights_connection_string_for_agent() -> None:
    source = _root_infra_source()
    assert "output APPLICATIONINSIGHTS_CONNECTION_STRING string" in source


# ---------------------------------------------------------------------------
# Telemetry initialisation in hosted-agent entrypoint
# ---------------------------------------------------------------------------


def test_main_entrypoint_configures_telemetry_before_host() -> None:
    main_source = (ROOT / "hosted_agents/card_orchestrator/__main__.py").read_text()
    assert "from app.telemetry import configure_telemetry" in main_source
    assert "configure_telemetry()" in main_source
    # Telemetry must be initialised before create_host to ensure it starts
    # before the host disables payload instrumentation.
    telemetry_line = main_source.index("configure_telemetry()")
    create_host_line = main_source.index("create_host(settings)")
    assert (
        telemetry_line < create_host_line
    ), "configure_telemetry() must be called before create_host()"


def test_main_entrypoint_telemetry_is_fail_open() -> None:
    main_source = (ROOT / "hosted_agents/card_orchestrator/__main__.py").read_text()
    # configure_telemetry() must be a plain call outside the settings try/except,
    # so it fails open when APPLICATIONINSIGHTS_CONNECTION_STRING is absent.
    settings_block_end = main_source.index("raise SystemExit")
    telemetry_line = main_source.index("configure_telemetry()")
    assert (
        telemetry_line > settings_block_end
    ), "configure_telemetry() must be outside the settings validation try/except"


# ---------------------------------------------------------------------------
# Runbook document invariants
# ---------------------------------------------------------------------------


def test_agent_operational_ownership_doc_exists() -> None:
    assert (ROOT / "docs/agent-operational-ownership.md").is_file()


def test_operational_ownership_doc_has_ownership_matrix() -> None:
    content = (ROOT / "docs/agent-operational-ownership.md").read_text()
    assert "Ownership matrix" in content
    assert "Gimli" in content
    assert "Rollback" in content


def test_operational_ownership_doc_has_rollout_procedure() -> None:
    content = (ROOT / "docs/agent-operational-ownership.md").read_text()
    assert "Pre-rollout gates" in content
    assert "Post-rollout health gates" in content
    assert "web app is unchanged" in content.lower() or "UNCHANGED" in content


def test_operational_ownership_doc_does_not_claim_automatic_rollback() -> None:
    content = (ROOT / "docs/agent-operational-ownership.md").read_text()
    # Rollback must be documented as manual, not automatic.
    assert "does not happen automatically" in content


def test_operational_ownership_doc_has_privacy_boundary() -> None:
    content = (ROOT / "docs/agent-operational-ownership.md").read_text()
    assert "Privacy" in content or "telemetry boundary" in content.lower()
    assert "prompt" in content.lower()


def test_operational_monitoring_doc_cross_references_agent_runbook() -> None:
    content = (ROOT / "docs/operational-monitoring.md").read_text()
    assert "agent-operational-ownership.md" in content


# ---------------------------------------------------------------------------
# IaC security invariants
# ---------------------------------------------------------------------------


def test_agent_monitoring_bicep_has_no_secrets_or_list_keys() -> None:
    source = _agent_monitoring_source()
    for forbidden in ("listKeys(", "listSecrets(", "connectionString"):
        assert forbidden not in source, f"agent-monitoring.bicep must not contain: {forbidden!r}"


def test_agent_monitoring_bicep_has_no_public_network_access() -> None:
    source = _agent_monitoring_source()
    assert "publicNetworkAccess" not in source


def test_webhook_receivers_must_not_embed_credentials_note() -> None:
    source = _agent_monitoring_source()
    assert "Do not embed credentials in serviceUri" in source
