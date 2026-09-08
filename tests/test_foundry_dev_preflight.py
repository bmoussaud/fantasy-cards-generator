import argparse
import json
import subprocess

import pytest

from scripts import foundry_dev_preflight as preflight


@pytest.fixture
def azure(monkeypatch):
    group_id = "/subscriptions/sub/resourceGroups/rg-dev"
    account_id = f"{group_id}/providers/Microsoft.CognitiveServices/accounts/account"
    project_id = f"{account_id}/projects/project"
    endpoint = "https://account.services.ai.azure.com/api/projects/project"
    responses = [
        {"id": group_id, "environment": "dev"},
        {"principal": "project-mi", "endpoints": {"AI Foundry API": endpoint}},
        {
            "principal": "app-mi",
            "revision": "app--ready",
            "containers": [
                {
                    "name": "web",
                    "image": "registry/app:existing",
                    "config": [
                        {"name": "APP_ENV", "value": "dev"},
                        {"name": "FOUNDRY_PROJECT_ENDPOINT", "value": endpoint},
                    ],
                }
            ],
            "traffic": [{"latestRevision": True, "weight": 100}],
        },
        [
            {
                "principalId": "project-mi",
                "roleDefinitionId": f"/roles/{preflight.FOUNDRY_USER}",
                "scope": account_id,
            },
            {
                "principalId": "app-mi",
                "roleDefinitionId": f"/roles/{preflight.AGENT_CONSUMER}",
                "scope": project_id,
            },
        ],
        {"names": ["card-orchestrator"], "hasMore": False},
    ]
    calls = []

    def run(*args):
        calls.append(args)
        return responses[len(calls) - 1]

    monkeypatch.setattr(preflight, "az_json", run)
    args = argparse.Namespace(
        subscription="sub",
        resource_group="rg-dev",
        account="account",
        project="project",
        container_app="app",
        agent_name="card-orchestrator",
    )
    return args, responses, calls


def test_ready_prerequisites_are_not_live_acceptance(azure):
    args, _, calls = azure
    report = preflight.preflight(args)
    assert report["prerequisitesReady"]
    assert report["liveAcceptance"] is False
    assert report["identityUsed"] == "azure_cli_operator_not_target_managed_identity"
    assert report["configuredImage"] == "registry/app:existing"
    assert len(calls) == 5
    assert all(call[:3] == ("rest", "--method", "get") for call in calls if call[0] == "rest")
    assert "https://ai.azure.com" in calls[-1]
    assert "--fill-principal-name" in calls[-2]


def test_missing_runtime_and_roles_are_explicit(azure):
    args, responses, _ = azure
    responses[2]["containers"][0]["config"].pop()
    responses[3] = []
    responses[4]["names"] = []
    report = preflight.preflight(args)
    assert not report["prerequisitesReady"]
    assert report["checks"] == {
        "projectIdentityFoundryUserAtAccount": False,
        "appIdentityAgentConsumerAtProject": False,
        "appProjectEndpointConfigured": False,
        "agentNamePresent": False,
        "agentInventoryComplete": True,
    }


@pytest.mark.parametrize("environment", ["prod", None, "development"])
def test_non_dev_resource_group_stops_before_resource_queries(azure, environment):
    args, responses, calls = azure
    responses[0]["environment"] = environment
    with pytest.raises(preflight.PreflightError, match="resource_group_not_tagged_dev"):
        preflight.preflight(args)
    assert len(calls) == 1


def test_non_dev_application_stops_before_roles_or_data_plane(azure):
    args, responses, calls = azure
    responses[2]["containers"][0]["config"][0]["value"] = "production"
    with pytest.raises(preflight.PreflightError, match="application_not_dev"):
        preflight.preflight(args)
    assert len(calls) == 3


def test_unexpected_endpoint_stops_before_data_plane(azure):
    args, responses, calls = azure
    responses[1]["endpoints"]["AI Foundry API"] = "https://untrusted.example"
    with pytest.raises(preflight.PreflightError, match="unexpected_project_endpoint"):
        preflight.preflight(args)
    assert len(calls) == 2


def test_paginated_inventory_is_not_ready(azure):
    args, responses, _ = azure
    responses[4]["hasMore"] = True
    report = preflight.preflight(args)
    assert not report["prerequisitesReady"]
    assert not report["checks"]["agentInventoryComplete"]


@pytest.mark.parametrize("inventory", [{"names": [], "hasMore": None}, {"names": None}])
def test_malformed_inventory_is_inconclusive(azure, inventory):
    args, responses, _ = azure
    responses[4] = inventory
    with pytest.raises(preflight.PreflightError, match="unexpected_agent_list"):
        preflight.preflight(args)


def test_missing_project_endpoint_is_sanitized(azure):
    args, responses, _ = azure
    responses[1]["endpoints"] = None
    with pytest.raises(preflight.PreflightError, match="unexpected_project_endpoint"):
        preflight.preflight(args)


def test_multiple_containers_require_explicit_operator_review(azure):
    args, responses, _ = azure
    responses[2]["containers"].append({"name": "sidecar"})
    with pytest.raises(preflight.PreflightError, match="expected_single_application_container"):
        preflight.preflight(args)


def test_wrong_identity_or_broader_scope_does_not_satisfy_exact_role(azure):
    args, responses, _ = azure
    responses[3][0]["principalId"] = "operator"
    responses[3][1]["scope"] = "/subscriptions/sub"
    report = preflight.preflight(args)
    assert not report["checks"]["projectIdentityFoundryUserAtAccount"]
    assert not report["checks"]["appIdentityAgentConsumerAtProject"]


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        ("Please run az login. sensitive detail", "authentication_unavailable"),
        ("AuthorizationFailed sensitive detail", "authorization_denied"),
        ("ResourceNotFound sensitive detail", "resource_missing"),
        ("PublicNetworkAccessDisabled sensitive detail", "network_access_blocked"),
        ("unrecognized sensitive detail", "azure_query_failed"),
    ],
)
def test_cli_failure_is_sanitized_and_bounded(monkeypatch, stderr, code):
    def run(command, **kwargs):
        assert kwargs["timeout"] == 30
        assert kwargs["capture_output"]
        assert kwargs["env"]["AZURE_EXTENSION_USE_DYNAMIC_INSTALL"] == "no"
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=stderr)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(preflight.PreflightError) as error:
        preflight.az_json("group", "show")
    assert str(error.value) == code


@pytest.mark.parametrize(
    ("exception", "code"),
    [
        (FileNotFoundError(), "azure_cli_missing"),
        (subprocess.TimeoutExpired("az", 30), "azure_query_timeout"),
    ],
)
def test_missing_cli_and_timeout(monkeypatch, exception, code):
    def run(*args, **kwargs):
        raise exception

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(preflight.PreflightError, match=code):
        preflight.az_json("group", "show")


def test_cli_returns_nonzero_without_claiming_acceptance(monkeypatch, capsys):
    def fail(args):
        raise preflight.PreflightError("authorization_denied")

    monkeypatch.setattr(preflight, "preflight", fail)
    assert (
        preflight.main(
            [
                "--subscription",
                "sub",
                "--resource-group",
                "rg-dev",
                "--account",
                "account",
                "--project",
                "project",
                "--container-app",
                "app",
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out) == {
        "liveAcceptance": False,
        "errorCode": "authorization_denied",
    }


@pytest.mark.parametrize("ready", [True, False])
def test_cli_exit_code_reflects_prerequisites_only(monkeypatch, capsys, ready):
    monkeypatch.setattr(
        preflight, "preflight", lambda args: {"prerequisitesReady": ready, "liveAcceptance": False}
    )
    assert preflight.main(
        [
            "--subscription",
            "sub",
            "--resource-group",
            "rg-dev",
            "--account",
            "account",
            "--project",
            "project",
            "--container-app",
            "app",
        ]
    ) == (0 if ready else 2)
    assert json.loads(capsys.readouterr().out)["liveAcceptance"] is False
