"""Offline packaging contracts; no Azure lookup, image build, or model invocation."""

import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.test_deployment_config import _bicep_block

ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT = ROOT / "deployments/card-orchestrator"
AGENT = ROOT / "hosted_agents/card_orchestrator"


def _manifest() -> dict:
    # JSON-compatible YAML avoids adding a parser dependency to deployment tests.
    return json.loads((DEPLOYMENT / "azure.yaml").read_text())


def test_agent_service_is_isolated_and_has_no_hooks_or_model_provisioning() -> None:
    manifest = _manifest()
    assert set(manifest["services"]) == {"card-orchestrator"}
    assert manifest["infra"] == {"provider": "bicep", "path": "infra"}
    assert manifest["requiredVersions"]["extensions"] == {"azure.ai.agents": "=1.0.0-beta.13"}
    service = manifest["services"]["card-orchestrator"]
    assert service["host"] == "azure.ai.agent"
    assert service["kind"] == "hosted"
    assert service["name"] == "card-orchestrator"
    assert service["protocols"] == [{"protocol": "responses", "version": "2.0.0"}]
    assert service["container"] == {"resources": {"cpu": "0.5", "memory": "1Gi"}}
    for forbidden in ("hooks", "deployments", "uses", "codeConfiguration", "resources"):
        assert forbidden not in manifest
        assert forbidden not in service


def test_dedicated_azd_folder_resolves_root_context_and_agent_dockerfile() -> None:
    service = _manifest()["services"]["card-orchestrator"]
    service_root = (DEPLOYMENT / service["project"]).resolve()
    assert service["language"] == "docker"
    assert service_root == DEPLOYMENT
    assert (service_root / service["docker"]["context"]).resolve() == ROOT
    assert (service_root / service["docker"]["path"]).resolve() == AGENT / "Dockerfile"
    assert service["docker"]["platform"] == "linux/amd64"
    assert service["docker"]["remoteBuild"] is False
    assert service["docker"]["registry"] == "${AZURE_CONTAINER_REGISTRY_ENDPOINT}"
    assert service["docker"]["image"] == "card-orchestrator"
    assert service["docker"]["tag"] == "${CARD_ORCHESTRATOR_VERSION}"


def test_only_nonreserved_runtime_variables_are_supplied() -> None:
    service = _manifest()["services"]["card-orchestrator"]
    assert service["environmentVariables"] == [
        {"name": "AZURE_AI_MODEL_DEPLOYMENT_NAME", "value": "${AZURE_AI_MODEL_DEPLOYMENT_NAME}"},
        {"name": "CARD_ORCHESTRATOR_VERSION", "value": "${CARD_ORCHESTRATOR_VERSION}"},
        {"name": "TELEMETRY_ENABLED", "value": "true"},
        {"name": "TELEMETRY_ENVIRONMENT", "value": "${AZURE_ENV_NAME}"},
        {"name": "AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING", "value": "true"},
    ]
    assert "APPLICATIONINSIGHTS_CONNECTION_STRING" not in {
        item["name"] for item in service["environmentVariables"]
    }


def test_container_contract_is_nonroot_frozen_and_independent_of_web() -> None:
    dockerfile = (AGENT / "Dockerfile").read_text()
    assert "FROM python:3.12-slim" in dockerfile
    assert "uv sync --frozen --no-dev --extra hosted-agent" in dockerfile
    assert "USER agent" in dockerfile
    assert "EXPOSE 8088" in dockerfile
    assert 'CMD ["python", "-m", "hosted_agents.card_orchestrator"]' in dockerfile
    assert "/readiness" in dockerfile
    assert "COPY app ./app" in dockerfile
    assert "COPY hosted_agents ./hosted_agents" in dockerfile
    assert "COPY . " not in dockerfile
    assert "app.entrypoint" not in dockerfile
    assert ":latest" not in dockerfile
    assert "8088" not in (ROOT / "Dockerfile").read_text()


def _context_includes(path: str) -> bool:
    """Evaluate the limited *, **, ! rules used by this Docker-specific allowlist."""
    included = True
    parts = path.split("/")
    candidates = ["/".join(parts[:n]) for n in range(1, len(parts) + 1)]
    for line in (AGENT / "Dockerfile.dockerignore").read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        pattern = line.lstrip("!").rstrip("/")
        regex = re.escape(pattern)
        regex = regex.replace(r"\*\*/", "(?:.*/)?")
        regex = regex.replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
        if any(re.fullmatch(regex, candidate) for candidate in candidates):
            included = negate
    return included


@pytest.mark.parametrize(
    "path",
    [
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "app/__init__.py",
        "app/services/generation.py",
        "app/secrets.py",
        "hosted_agents/__init__.py",
        "hosted_agents/card_orchestrator/__main__.py",
        "hosted_agents/card_orchestrator/Dockerfile",
        "hosted_agents/card_orchestrator/Dockerfile.dockerignore",
    ],
)
def test_context_includes_only_required_package_inputs(path: str) -> None:
    assert _context_includes(path)


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.production",
        ".azure/dev/.env",
        ".git/config",
        ".squad/agents/gimli/history.md",
        "deployments/card-orchestrator/.azure/dev/.env",
        "tests/fixtures/private.json",
        "app/.env.local",
        "app/.azure/credentials.py",
        "app/.squad/history.py",
        "app/credentials/token.py",
        "app/__pycache__/settings.pyc",
        "hosted_agents/card_orchestrator/.env",
        "hosted_agents/card_orchestrator/.foundry/history.py",
        "hosted_agents/card_orchestrator/private.pem",
        "hosted_agents/card_orchestrator/.venv/config.py",
        "app/static/user-upload.png",
        "infra/main.bicep",
        "azure.yaml",
    ],
)
def test_context_excludes_credentials_state_and_unrelated_artifacts(path: str) -> None:
    assert not _context_includes(path)


def test_prerequisites_default_off_and_infra_supports_dev_and_prod() -> None:
    main = (DEPLOYMENT / "infra/main.bicep").read_text()
    parameters = json.loads((DEPLOYMENT / "infra/main.parameters.json").read_text())["parameters"]
    assert "'dev'\n  'prod'" in main
    assert "param enablePrerequisites bool = false" in main
    assert "= if (enablePrerequisites)" in main
    assert "param createRegistryConnection bool = false" in main
    assert parameters["enablePrerequisites"]["value"].endswith("=false}")
    assert parameters["createRegistryConnection"]["value"].endswith("=false}")
    assert "containerAppPrincipalId: containerApp.identity.principalId" in main
    assert "projectPrincipalId: project.identity.principalId" in main
    assert "output FOUNDRY_PROJECT_ENDPOINT string = projectEndpoint" in main
    assert "output AZURE_AI_PROJECT_ENDPOINT string = projectEndpoint" in main
    assert "output AZURE_AI_PROJECT_ID string = project.id" in main
    assert "resourceId(resourceGroupName," not in main


def test_iac_only_writes_scoped_assignments_and_optional_registry_connection() -> None:
    # Allowed non-existing resource types: role assignments, project connections, and
    # agent monitoring resources (action groups, workbooks, alert rules).
    _ALLOWED_CREATED_TYPES = {
        "Microsoft.Authorization/roleAssignments",
        "Microsoft.CognitiveServices/accounts/projects/connections",
        "Microsoft.Insights/actionGroups",
        "Microsoft.Insights/workbooks",
        "Microsoft.Insights/scheduledQueryRules",
    }
    for path in (DEPLOYMENT / "infra").rglob("*.bicep"):
        # Skip the monitoring module — it intentionally creates monitoring resources.
        if path.name == "agent-monitoring.bicep":
            continue
        source = path.read_text()
        for resource_type, existing in re.findall(
            r"\bresource\s+\w+\s+'([^']+)'\s+(existing\s+)?=", source
        ):
            if not existing:
                assert resource_type.split("@")[0] in _ALLOWED_CREATED_TYPES
        for forbidden in ("../..", "listKeys(", "listSecrets(", "publicNetworkAccess:", "sku:"):
            assert forbidden not in source


def test_repair_facade_reuses_main_canonical_foundry_role_ids_and_scopes() -> None:
    root = (ROOT / "infra/modules/ai-foundry.bicep").read_text()
    repair = (DEPLOYMENT / "infra/modules/prerequisites.bicep").read_text()
    for role in ("foundryUserRoleDefinitionId", "foundryAgentConsumerRoleDefinitionId"):
        pattern = rf"var {role} = '[^']+'"
        assert re.search(pattern, root).group() == re.search(pattern, repair).group()
    for symbol in (
        "projectManagedIdentityFoundryUserRoleAssignment",
        "containerAppFoundryAgentConsumerRoleAssignment",
    ):
        root_block = _bicep_block(root, f"resource {symbol}")
        repair_block = _bicep_block(repair, f"resource {symbol}")
        assert root_block[root_block.index("{") :] == repair_block[repair_block.index("{") :]


def test_registry_connection_uses_project_identity_and_classic_acr_pull_only() -> None:
    source = (DEPLOYMENT / "infra/modules/prerequisites.bicep").read_text()
    assert "'7f951dda-4ed3-4680-a7ca-43fe172d538d'" in source
    assert "guid(registry.id, projectPrincipalId, acrPullRoleDefinitionId)" in source
    assert "projects/connections@2025-04-01-preview" in source
    assert "= if (createRegistryConnection)" in source
    assert "category: 'ContainerRegistry'" in source
    assert "authType: 'ManagedIdentity'" in source
    assert "clientId: aiFoundryProject.identity.principalId" in source
    assert "resourceId: registry.id" in source
    assert "listCredentials" not in source
    assert "AcrPush" not in source


def _launcher():
    spec = importlib.util.spec_from_file_location("agent_deploy", DEPLOYMENT / "deploy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("action", ["preview", "provision", "deploy"])
def test_launcher_is_plan_only_by_default(action: str, monkeypatch) -> None:
    launcher = _launcher()

    def forbidden(*args, **kwargs):
        pytest.fail("Planning must never start azd")

    monkeypatch.setattr(launcher.subprocess, "run", forbidden)
    assert launcher.main([action]) == 0


@pytest.mark.parametrize(
    "arguments",
    [
        ["deploy", "--execute"],
        ["provision", "--execute"],
        ["deploy", "--environment", "prod", "--execute", "--approve-change"],
        ["preview", "--environment", "staging"],
    ],
)
def test_launcher_rejects_unapproved_mutations_and_other_environments(arguments) -> None:
    with pytest.raises(SystemExit) as error:
        _launcher().main(arguments)
    assert error.value.code == 2


@pytest.mark.parametrize("action", ["preview", "provision", "deploy"])
def test_launcher_execution_is_scoped_to_dedicated_manifest(action: str, monkeypatch) -> None:
    launcher = _launcher()
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return type("Result", (), {"returncode": 17})()

    monkeypatch.setattr(launcher.subprocess, "run", run)
    assert launcher.main([action, "--execute", "--approve-change"]) == 17
    command, options = calls[0]
    assert command[:2] == ["azd", "deploy" if action == "deploy" else "provision"]
    assert ("card-orchestrator" in command) == (action == "deploy")
    assert ("--preview" in command) == (action == "preview")
    assert command[-3:] == ["--environment", "dev", "--no-prompt"]
    assert options["cwd"] == DEPLOYMENT
    assert "env" not in options
    assert options["check"] is False


def test_launcher_has_separately_gated_prod_path(monkeypatch) -> None:
    launcher = _launcher()
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(launcher.subprocess, "run", run)
    assert (
        launcher.main(
            [
                "deploy",
                "--environment",
                "prod",
                "--execute",
                "--approve-change",
                "--approve-prod",
            ]
        )
        == 0
    )
    assert calls[0][0][-3:] == ["--environment", "prod", "--no-prompt"]


# ── Consolidated root entry point (issue #130) ──────────────────────


def test_root_manifest_card_orchestrator_matches_nested_contract() -> None:
    """Root and nested manifests declare equivalent card-orchestrator service contracts."""
    root_yaml = (ROOT / "azure.yaml").read_text()
    nested_service = _manifest()["services"]["card-orchestrator"]

    # Host type and kind match
    assert "host: azure.ai.agent" in root_yaml
    assert "kind: hosted" in root_yaml

    # Docker paths resolve to the same Dockerfile
    root_dockerfile = ROOT / "hosted_agents/card_orchestrator/Dockerfile"
    nested_dockerfile = (DEPLOYMENT / nested_service["docker"]["path"]).resolve()
    assert root_dockerfile.resolve() == nested_dockerfile

    # Environment variables match
    for env_var in nested_service["environmentVariables"]:
        assert env_var["name"] in root_yaml


def test_root_manifest_card_orchestrator_container_contract() -> None:
    """Root card-orchestrator matches the nested container and protocol contracts."""
    root_yaml = (ROOT / "azure.yaml").read_text()

    assert 'cpu: "0.5"' in root_yaml
    assert "memory: 1Gi" in root_yaml
    assert "protocol: responses" in root_yaml
    assert "image: card-orchestrator" in root_yaml
    assert "tag: ${CARD_ORCHESTRATOR_VERSION}" in root_yaml
    assert "platform: linux/amd64" in root_yaml
    assert "remoteBuild: false" in root_yaml


def test_nested_manifest_has_deprecation_notice() -> None:
    """The deprecated nested manifest must document its superseded status."""
    content = (DEPLOYMENT / "azure.yaml").read_text()
    assert "DEPRECATED" in content
    assert "root azure.yaml" in content.lower() or "root" in content.lower()


def test_nested_launcher_has_deprecation_notice() -> None:
    """The deprecated launcher must document its superseded status."""
    content = (DEPLOYMENT / "deploy.py").read_text()
    assert "DEPRECATED" in content
    assert "azd deploy card-orchestrator" in content


def test_root_bicep_card_orchestrator_prerequisites_conditional() -> None:
    """Root Bicep gates agent prerequisites and monitoring on explicit opt-in."""
    main = (ROOT / "infra/main.bicep").read_text()

    # Prerequisites module is conditional
    prereqs_module = _bicep_block(main, "module agentPrerequisites")
    assert "if (enableCardOrchestratorPrerequisites)" in prereqs_module

    # Agent monitoring module is conditional
    monitoring_module = _bicep_block(main, "module agentMonitoring")
    assert "if (enableCardOrchestratorPrerequisites)" in monitoring_module

    # Shares the same monitoring and registry resources
    assert "monitoring.outputs.appInsightsResourceId" in monitoring_module
    assert "monitoring.outputs.logAnalyticsWorkspaceResourceId" in monitoring_module
    assert "registryName: registryName" in prereqs_module


def test_root_agent_prerequisites_reuses_card_orchestrator_canonical_patterns() -> None:
    """Root and card-orchestrator prerequisites use identical ACR Pull role IDs
    and registry connection patterns."""
    root_prereqs = (ROOT / "infra/modules/agent-prerequisites.bicep").read_text()
    nested_prereqs = (DEPLOYMENT / "infra/modules/prerequisites.bicep").read_text()

    # Same canonical AcrPull role ID
    assert "'7f951dda-4ed3-4680-a7ca-43fe172d538d'" in root_prereqs
    assert "'7f951dda-4ed3-4680-a7ca-43fe172d538d'" in nested_prereqs

    # Same registry connection API version and patterns
    assert "projects/connections@2025-04-01-preview" in root_prereqs
    assert "projects/connections@2025-04-01-preview" in nested_prereqs
    assert "category: 'ContainerRegistry'" in root_prereqs
    assert "authType: 'ManagedIdentity'" in root_prereqs
    assert "clientId: aiFoundryProject.identity.principalId" in root_prereqs
    assert "resourceId: registry.id" in root_prereqs

    # No credential leaks in either
    assert "listCredentials" not in root_prereqs
    assert "AcrPush" not in root_prereqs


# ── Deploy guard and orchestration (reviewer findings) ───────────────


def test_root_workflow_uses_only_supported_up_override() -> None:
    """azd 1.32 supports ``workflows.up`` only; the root manifest must not
    claim an unsupported bare ``azd deploy`` override."""
    azure_yaml = (ROOT / "azure.yaml").read_text()
    workflows_start = azure_yaml.index("workflows:")
    hooks_start = azure_yaml.rindex("\nhooks:")
    workflows_section = azure_yaml[workflows_start:hooks_start]

    assert "up:" in workflows_section
    assert "deploy:" not in workflows_section
    assert "card-orchestrator" not in workflows_section
    assert "package web-nat" in workflows_section
    assert "deploy web-nat" in workflows_section


def test_card_orchestrator_service_lifecycle_guards_exist_without_fake_condition_gate() -> None:
    """The root manifest keeps real lifecycle hooks and drops the unsupported condition."""
    azure_yaml = (ROOT / "azure.yaml").read_text()

    co_start = azure_yaml.index("card-orchestrator:")
    service_section = azure_yaml[co_start : azure_yaml.index("\nresources:", co_start)]
    assert "condition:" not in service_section
    for hook in ("prebuild:", "prepackage:", "prepublish:", "predeploy:"):
        assert hook in service_section
    assert service_section.count("guard_agent_deploy") == 4

    guard = ROOT / "hooks/guard_agent_deploy.sh"
    assert guard.is_file()
    assert guard.stat().st_mode & 0o111


def test_predeploy_guard_rejects_unset_prerequisites() -> None:
    """The guard script exits non-zero when CARD_ORCHESTRATOR_ENABLE_PREREQUISITES
    is not 'true', preventing accidental agent deployment."""
    guard = ROOT / "hooks/guard_agent_deploy.sh"

    # Unset → blocked
    result = subprocess.run(
        ["bash", str(guard)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 1
    assert "blocked" in result.stderr.lower()


def test_predeploy_guard_allows_enabled_prerequisites() -> None:
    """The guard script exits zero when CARD_ORCHESTRATOR_ENABLE_PREREQUISITES=true."""
    guard = ROOT / "hooks/guard_agent_deploy.sh"

    result = subprocess.run(
        ["bash", str(guard)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "CARD_ORCHESTRATOR_ENABLE_PREREQUISITES": "true"},
    )
    assert result.returncode == 0


def test_root_orchestrator_preserves_approval_gates() -> None:
    """deploy.sh at the repository root is the production-safe entry point,
    preserving --approve-change / --approve-prod semantics from the deprecated
    launcher."""
    script = ROOT / "deploy.sh"
    assert script.is_file()
    assert script.stat().st_mode & 0o111
    content = script.read_text()
    assert "--approve-change" in content
    assert "--approve-prod" in content
    assert "PLAN ONLY" in content


def test_root_orchestrator_executes_azd_with_explicit_root_cwd(tmp_path: Path) -> None:
    """The root wrapper scopes azd with --cwd to its own repo root even when
    launched from a different current working directory."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "azd.log"
    fake_azd = fake_bin / "azd"
    fake_azd.write_text(
        f'#!/bin/sh\necho "pwd=$PWD" > "{log}"\necho "args=$*" >> "{log}"\nexit 17\n'
    )
    fake_azd.chmod(0o755)

    result = subprocess.run(
        ["bash", str(ROOT / "deploy.sh"), "agent", "--approve-change"],
        cwd=tmp_path,
        env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 17
    assert f"args=--cwd {ROOT} deploy card-orchestrator --environment dev --no-prompt" in (
        log.read_text()
    )


def test_root_orchestrator_plan_is_root_relative_from_other_cwd(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", str(ROOT / "deploy.sh"), "full"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert f"Project: {ROOT}" in result.stdout
    assert f"azd --cwd {ROOT} deploy web-nat" in result.stdout
    assert f"azd --cwd {ROOT} deploy card-orchestrator" in result.stdout
