"""Offline packaging contracts; no Azure lookup, image build, or model invocation."""

import importlib.util
import json
import re
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


def test_only_nonreserved_model_and_build_variables_are_supplied() -> None:
    service = _manifest()["services"]["card-orchestrator"]
    assert service["environmentVariables"] == [
        {"name": "AZURE_AI_MODEL_DEPLOYMENT_NAME", "value": "${AZURE_AI_MODEL_DEPLOYMENT_NAME}"},
        {"name": "CARD_ORCHESTRATOR_VERSION", "value": "${CARD_ORCHESTRATOR_VERSION}"},
    ]


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


def test_prerequisites_default_off_and_dev_only() -> None:
    main = (DEPLOYMENT / "infra/main.bicep").read_text()
    parameters = json.loads((DEPLOYMENT / "infra/main.parameters.json").read_text())["parameters"]
    assert "@allowed(['dev'])" in main
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
    for path in (DEPLOYMENT / "infra").rglob("*.bicep"):
        source = path.read_text()
        for resource_type, existing in re.findall(
            r"\bresource\s+\w+\s+'([^']+)'\s+(existing\s+)?=", source
        ):
            if not existing:
                assert resource_type.split("@")[0] in {
                    "Microsoft.Authorization/roleAssignments",
                    "Microsoft.CognitiveServices/accounts/projects/connections",
                }
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
