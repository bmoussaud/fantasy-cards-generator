"""Offline contracts for additive, metadata-only private execution."""

import importlib.util
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts/private_runner"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def probe():
    return load("preflight")


@pytest.fixture
def control():
    return load("control")


class MetadataOnlyClient:
    def __init__(self, count=1):
        self.names = []
        self.count = count

    def list_properties_of_secret_versions(self, name):
        self.names.append(name)
        for number in range(self.count):
            yield SimpleNamespace(version=f"{number:032x}")

    def get_secret(self, *_args, **_kwargs):
        pytest.fail("Secret value reads are prohibited")

    def list_properties_of_secrets(self, *_args, **_kwargs):
        pytest.fail("Vault-wide metadata listing is prohibited")


def test_metadata_is_named_read_only_bounded_and_hashed(probe):
    client = MetadataOnlyClient()
    digest = probe.metadata(client)
    assert client.names == ["app-session-secret-key", "entra-client-secret"]
    assert len(digest) == 64
    assert digest == probe.metadata(MetadataOnlyClient())
    with pytest.raises(ValueError, match="version_limit"):
        probe.metadata(MetadataOnlyClient(101))
    with pytest.raises(ValueError, match="missing_metadata"):
        probe.metadata(MetadataOnlyClient(0))


@pytest.mark.parametrize("addresses", [["10.42.2.7"], ["1.1.1.1"], ["10.42.2.8"], []])
def test_dns_requires_exact_private_endpoint(probe, monkeypatch, addresses):
    monkeypatch.setattr(
        probe.socket,
        "getaddrinfo",
        lambda *_a, **_kw: [(socket.AF_INET, 1, 6, "", (ip, 443)) for ip in addresses],
    )
    if addresses == ["10.42.2.7"]:
        probe.private_dns("not-logged.example", "10.42.2.7")
    else:
        with pytest.raises(ValueError, match="private_dns_mismatch"):
            probe.private_dns("not-logged.example", "10.42.2.7")
    with pytest.raises(ValueError, match="invalid_private_address"):
        probe.private_dns("not-logged.example", "1.1.1.1")


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (None, "ok"),
        (403, "metadata_forbidden"),
        (429, "metadata_throttled"),
        (500, "metadata_http_failed"),
    ],
)
def test_fake_sdk_main_is_redacted_and_managed_identity_only(
    probe, monkeypatch, capsys, failure, status
):
    import azure.identity
    import azure.keyvault.secrets
    from azure.core.exceptions import HttpResponseError

    calls = []

    class Credential:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Client(MetadataOnlyClient, Credential):
        def __init__(self, **kwargs):
            assert kwargs["retry_total"] == 0
            assert kwargs["logging_enable"] is False
            MetadataOnlyClient.__init__(self)

        def list_properties_of_secret_versions(self, name):
            if failure:
                error = HttpResponseError(
                    message="sensitive-sdk-message https://internal.example/secrets/name/value"
                )
                error.status_code = failure
                raise error
            return super().list_properties_of_secret_versions(name)

    monkeypatch.setattr(azure.identity, "ManagedIdentityCredential", Credential)
    monkeypatch.setattr(azure.keyvault.secrets, "SecretClient", Client)
    monkeypatch.setattr(probe, "private_dns", lambda *_args: None)
    monkeypatch.setattr(probe.signal, "alarm", lambda _seconds: None)
    monkeypatch.setattr(probe.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(probe.sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(probe.logging, "disable", lambda *_args: None)
    monkeypatch.setenv("RUNNER_CLIENT_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("RUNNER_PRIVATE_IP", "10.42.2.7")
    assert probe.main() == (0 if failure is None else 1)
    output = capsys.readouterr()
    record = json.loads(output.out)
    assert record["status"] == status
    assert set(record) <= {"schema", "status", "version_hash"}
    assert not output.err
    assert "sensitive" not in output.out and "https://" not in output.out
    assert calls[0]["client_id"].endswith("0001")
    assert calls[0]["retry_total"] == 0


def test_unexpected_errors_are_not_tracebacks(probe, capsys):
    probe.unexpected(ValueError, ValueError("private-host/secret-id"), None)
    assert json.loads(capsys.readouterr().out)["status"] == "unexpected_error"


def preview(control):
    prefix = f"/subscriptions/{control.SUBSCRIPTION}/resourceGroups/{control.GROUP}/providers/"
    return {
        "status": "Succeeded",
        "changes": [
            {"resourceId": prefix + kind + "/" + str(i), "changeType": "Create"}
            for kind, count in (
                ("Microsoft.App/jobs", 1),
                ("Microsoft.ManagedIdentity/userAssignedIdentities", 1),
                ("Microsoft.Authorization/roleDefinitions", 1),
                ("Microsoft.Authorization/roleAssignments", 3),
            )
            for i in range(count)
        ],
    }


def test_preview_requires_all_six_resources_and_no_diagnostics(control):
    result = preview(control)
    assert sum(item["count"] for item in control.summarize_preview(result)) == 6
    result["changes"].pop(0)
    with pytest.raises(control.RunnerError, match="incomplete"):
        control.summarize_preview(result)
    result["diagnostics"] = [{"message": "nested module was skipped"}]
    with pytest.raises(control.RunnerError, match="diagnostics"):
        control.summarize_preview(result)


@pytest.mark.parametrize("change", ["Modify", "Delete", "Deploy", "Unsupported"])
def test_preview_refuses_every_non_additive_action(control, change):
    result = preview(control)
    result["changes"][0]["changeType"] = change
    with pytest.raises(control.RunnerError, match="additive"):
        control.summarize_preview(result)


def test_preview_refuses_scope_or_resource_expansion(control):
    result = preview(control)
    result["changes"][0]["resourceId"] = result["changes"][0]["resourceId"].replace(
        "Microsoft.App/jobs", "Microsoft.App/containerApps"
    )
    with pytest.raises(control.RunnerError, match="additive"):
        control.summarize_preview(result)
    result = preview(control)
    result["changes"][0]["resourceId"] = result["changes"][0]["resourceId"].replace(
        "rg-fcag-dev", "rg-fcag-prod"
    )
    with pytest.raises(control.RunnerError, match="additive"):
        control.summarize_preview(result)


def test_unapproved_commands_do_not_call_azure(control, monkeypatch):
    monkeypatch.setattr(control, "azure", lambda _args: pytest.fail("Azure called"))
    for action in ("runner-provision", "runner-start"):
        assert control.main([action, "--subscription", control.SUBSCRIPTION]) == 0


def test_provision_previews_before_create_and_does_not_start(control, monkeypatch):
    calls = []

    def azure(args):
        calls.append(args)
        return preview(control) if args[:3] == ["deployment", "group", "what-if"] else "Succeeded"

    monkeypatch.setattr(control, "azure", azure)
    assert (
        control.main(
            ["runner-provision", "--subscription", control.SUBSCRIPTION, "--approve-change"]
        )
        == 0
    )
    assert len(calls) == 2
    assert calls[1][:3] == ["deployment", "group", "create"]
    assert "--mode" in calls[1] and "Incremental" in calls[1]
    assert "infra/private-runner.bicep" in " ".join(calls[1])
    assert not any("azd" in arg or "start" == arg for call in calls for arg in call)


def test_azure_cli_error_redacts_raw_response(control, monkeypatch, capsys):
    monkeypatch.setattr(
        control.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(
            returncode=1, stdout="", stderr="AuthorizationFailed private-host credential-value"
        ),
    )
    assert control.main(["runner-preview", "--subscription", control.SUBSCRIPTION]) == 1
    output = capsys.readouterr().out
    assert "authorization_failed" in output
    assert "private-host" not in output and "credential-value" not in output


@pytest.mark.parametrize(
    "options",
    [[], ["--environment", "prod"], ["--environment", "dev"], ["--subscription", "wrong"]],
)
def test_root_runner_requires_explicit_dev_and_subscription(options):
    result = subprocess.run(
        ["bash", str(ROOT / "deploy.sh"), "runner-start", *options],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2


def test_runner_plan_does_not_invoke_azd_or_credential_hooks(control, tmp_path):
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "deploy.sh"),
            "runner-provision",
            "--environment",
            "dev",
            "--subscription",
            control.SUBSCRIPTION,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "AZURE_ENV_NAME": "prod"},
    )
    assert result.returncode == 0
    assert "PLAN ONLY" in result.stdout


@pytest.fixture(scope="module")
def compiled_module():
    result = subprocess.run(
        [
            "az",
            "bicep",
            "build",
            "--file",
            str(ROOT / "infra/modules/private-metadata-runner.bicep"),
            "--stdout",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_compiled_infrastructure_is_exactly_scoped(compiled_module):
    resources = compiled_module["resources"]
    # The two Key Vault assignments compile as a two-iteration copy.
    assert len(resources) == 5
    assert {r["type"] for r in resources} == {
        "Microsoft.App/jobs",
        "Microsoft.ManagedIdentity/userAssignedIdentities",
        "Microsoft.Authorization/roleDefinitions",
        "Microsoft.Authorization/roleAssignments",
    }
    role = next(r for r in resources if r["type"].endswith("/roleDefinitions"))
    assert role["properties"]["permissions"] == [
        {
            "actions": [],
            "notActions": [],
            "dataActions": ["Microsoft.KeyVault/vaults/secrets/readMetadata/action"],
            "notDataActions": [],
        }
    ]
    assignments = [r for r in resources if r["type"].endswith("/roleAssignments")]
    assert len(assignments) == 2
    named = next(r for r in assignments if "copy" in r)
    assert named["copy"]["count"] == (
        "[length(createArray('app-session-secret-key', 'entra-client-secret'))]"
    )
    assert "Microsoft.KeyVault/vaults/secrets" in named["scope"]
    assert "app-session-secret-key" in json.dumps(compiled_module)
    assert "entra-client-secret" in json.dumps(compiled_module)


def test_compiled_job_is_bounded_fixed_command_and_dedicated_identity(compiled_module, control):
    job = next(r for r in compiled_module["resources"] if r["type"] == "Microsoft.App/jobs")
    assert job["identity"]["type"] == "UserAssigned"
    assert len(job["identity"]["userAssignedIdentities"]) == 1
    properties = job["properties"]
    config = properties["configuration"]
    assert config["triggerType"] == "Manual"
    assert config["replicaTimeout"] == 120
    assert config["replicaRetryLimit"] == 0
    assert config["manualTriggerConfig"] == {"parallelism": 1, "replicaCompletionCount": 1}
    assert "secrets" not in config
    assert "ingress" not in config
    assert "scheduleTriggerConfig" not in config
    assert properties["workloadProfileName"] == "Consumption"
    container = properties["template"]["containers"][0]
    assert container["command"][:3] == ["/app/.venv/bin/python", "-I", "-c"]
    # Bicep loadTextContent is compiled to a generated template variable.
    assert (SCRIPTS / "preflight.py").read_text() in compiled_module["variables"].values()
    assert control.IMAGE in compiled_module["variables"].values()
    assert len(container["env"]) == 2
    assert compiled_module["parameters"]["devScopeGuard"]["minValue"] == 1


def test_root_and_leaf_use_same_default_off_dev_module():
    for filename in ("infra/main.bicep", "infra/private-runner.bicep"):
        source = (ROOT / filename).read_text()
        assert "param enablePrivateMetadataRunner bool = false" in source
        assert "'./modules/private-metadata-runner.bicep'" in source
    root = (ROOT / "infra/main.bicep").read_text()
    assert "enablePrivateMetadataRunner && environmentName == 'dev'" in root
    script = (SCRIPTS / "preflight.py").read_text()
    for forbidden in ("get_secret(", "set_secret(", "DefaultAzureCredential", "subprocess"):
        assert forbidden not in script


def deployed_job(control):
    prefix = f"/subscriptions/{control.SUBSCRIPTION}/resourceGroups/{control.GROUP}/providers/"
    identity_id = prefix + f"Microsoft.ManagedIdentity/userAssignedIdentities/{control.JOB}-id"
    return {
        "identity": {"type": "UserAssigned", "userAssignedIdentities": {identity_id: {}}},
        "environment": prefix + "Microsoft.App/managedEnvironments/fcag-dev-cae",
        "profile": "Consumption",
        "trigger": "Manual",
        "manual": {"parallelism": 1, "replicaCompletionCount": 1},
        "retries": 0,
        "timeout": 120,
        "secretCount": 0,
        "initCount": 0,
        "volumeCount": 0,
        "containers": [
            {
                "image": control.IMAGE,
                "command": [
                    "/app/.venv/bin/python",
                    "-I",
                    "-c",
                    (SCRIPTS / "preflight.py").read_text(),
                ],
                "env": [
                    {"name": "RUNNER_CLIENT_ID", "value": "dedicated-client"},
                    {"name": "RUNNER_PRIVATE_IP", "value": "10.42.2.7"},
                ],
                "resources": {"cpu": 0.25, "memory": "0.5Gi"},
            }
        ],
    }


@pytest.mark.parametrize("drift", [None, "command", "identity", "secret", "parallelism"])
def test_start_checks_fixed_job_and_refuses_drift(control, monkeypatch, drift):
    job = deployed_job(control)
    if drift == "command":
        job["containers"][0]["command"] = ["bash"]
    elif drift == "identity":
        job["identity"]["type"] = "SystemAssigned"
    elif drift == "secret":
        job["secretCount"] = 1
    elif drift == "parallelism":
        job["manual"]["parallelism"] = 2
    calls = []

    def azure(args):
        calls.append(args)
        if args[:3] == ["containerapp", "job", "show"]:
            return job
        if args[:2] == ["identity", "show"]:
            return "dedicated-client"
        if args[:4] == ["containerapp", "job", "execution", "list"]:
            return []
        if args[:3] == ["containerapp", "job", "start"]:
            return "execution-name-not-printed"
        pytest.fail("Unexpected Azure operation")

    monkeypatch.setattr(control, "azure", azure)
    result = control.main(
        ["runner-start", "--subscription", control.SUBSCRIPTION, "--approve-change"]
    )
    assert result == (0 if drift is None else 1)
    starts = [call for call in calls if call[:3] == ["containerapp", "job", "start"]]
    assert len(starts) == (1 if drift is None else 0)
    assert all("--command" not in call and "--args" not in call for call in starts)


def test_start_refuses_overlapping_execution(control, monkeypatch):
    monkeypatch.setattr(control, "validate_job", lambda: None)
    monkeypatch.setattr(control, "azure", lambda _args: ["already-running"])
    assert (
        control.main(["runner-start", "--subscription", control.SUBSCRIPTION, "--approve-change"])
        == 1
    )
