"""Root-only additive runner orchestration; never reads azd environment secrets."""

import argparse
import collections
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUBSCRIPTION = "b8ff3e15-7e2d-4fac-a773-992fb59ccedd"
GROUP = "rg-fcag-dev"
JOB = "fcag-dev-metadata-runner"
ALLOWED_TYPES = {
    "Microsoft.App/jobs",
    "Microsoft.ManagedIdentity/userAssignedIdentities",
    "Microsoft.Authorization/roleDefinitions",
    "Microsoft.Authorization/roleAssignments",
}
IMAGE = (
    "fcagdevqhg3qc4rlbt4gacr.azurecr.io/fantasy-cards-generator/web-nat-dev"
    "@sha256:53c95a2d0457516d715df8e2e78d996afde9124016d2f5b6381bbf0f07f7dfeb"
)


class RunnerError(Exception):
    pass


def azure(args):
    try:
        result = subprocess.run(
            [
                "az",
                *args,
                "--subscription",
                SUBSCRIPTION,
                "--only-show-errors",
                "--output",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RunnerError("azure_cli_unavailable_or_timeout") from error
    if result.returncode:
        # Never echo SDK/CLI errors, resource identifiers, or request payloads.
        status = (
            "authorization_failed"
            if "AuthorizationFailed" in result.stderr
            else "azure_command_failed"
        )
        raise RunnerError(status)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RunnerError("invalid_azure_response") from error


def summarize_preview(result):
    if result.get("status") != "Succeeded":
        raise RunnerError("preview_not_succeeded")
    if result.get("diagnostics"):
        raise RunnerError("preview_has_diagnostics")
    counts = collections.Counter()
    changes = result.get("changes")
    if not isinstance(changes, list):
        raise RunnerError("preview_missing_changes")
    for change in changes:
        action = change.get("changeType")
        if action in ("Ignore", "NoChange"):
            continue
        resource_id = change.get("resourceId", "")
        # Last provider segment handles extension RBAC at individual-secret scope.
        parts = resource_id.rsplit("/providers/", 1)[-1].split("/")
        kind = "/".join([parts[0], *parts[1::2]])
        prefix = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}/providers/"
        if (
            not resource_id.lower().startswith(prefix.lower())
            or kind not in ALLOWED_TYPES
            or action != "Create"
        ):
            raise RunnerError("preview_not_additive_create_only")
        counts[(action, kind)] += 1
    expected = {
        ("Create", "Microsoft.App/jobs"): 1,
        ("Create", "Microsoft.ManagedIdentity/userAssignedIdentities"): 1,
        ("Create", "Microsoft.Authorization/roleDefinitions"): 1,
        ("Create", "Microsoft.Authorization/roleAssignments"): 3,
    }
    if counts != expected:
        raise RunnerError("preview_incomplete_or_already_provisioned")
    return [
        {"action": action, "kind": kind, "count": count}
        for (action, kind), count in sorted(counts.items())
    ]


def validate_job():
    job = azure(
        [
            "containerapp",
            "job",
            "show",
            "-g",
            GROUP,
            "-n",
            JOB,
            "--query",
            "{identity:identity,environment:properties.environmentId,"
            "profile:properties.workloadProfileName,"
            "trigger:properties.configuration.triggerType,"
            "manual:properties.configuration.manualTriggerConfig,"
            "retries:properties.configuration.replicaRetryLimit,"
            "timeout:properties.configuration.replicaTimeout,"
            "secretCount:length(properties.configuration.secrets || `[]`),"
            "containers:properties.template.containers,"
            "initCount:length(properties.template.initContainers || `[]`),"
            "volumeCount:length(properties.template.volumes || `[]`)}",
        ]
    )
    prefix = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}/providers/"
    identity_id = prefix + f"Microsoft.ManagedIdentity/userAssignedIdentities/{JOB}-id"
    identity = job.get("identity", {})
    if identity.get("type") != "UserAssigned" or set(
        identity.get("userAssignedIdentities", {})
    ) != {identity_id}:
        raise RunnerError("job_identity_drift")
    client_id = azure(["identity", "show", "-g", GROUP, "-n", f"{JOB}-id", "--query", "clientId"])
    containers = job.get("containers", [])
    if len(containers) != 1:
        raise RunnerError("job_template_drift")
    container = containers[0]
    expected_command = [
        "/app/.venv/bin/python",
        "-I",
        "-c",
        (ROOT / "scripts/private_runner/preflight.py").read_text(),
    ]
    if (
        container.get("image") != IMAGE
        or container.get("command") != expected_command
        or container.get("args")
        or container.get("env")
        != [
            {"name": "RUNNER_CLIENT_ID", "value": client_id},
            {"name": "RUNNER_PRIVATE_IP", "value": "10.42.2.7"},
        ]
        or container.get("resources", {}).get("cpu") != 0.25
        or container.get("resources", {}).get("memory") != "0.5Gi"
        or job.get("environment") != prefix + "Microsoft.App/managedEnvironments/fcag-dev-cae"
        or job.get("profile") != "Consumption"
        or job.get("trigger") != "Manual"
        or job.get("manual") != {"parallelism": 1, "replicaCompletionCount": 1}
        or job.get("retries") != 0
        or job.get("timeout") != 120
        or any(job.get(key) != 0 for key in ("secretCount", "initCount", "volumeCount"))
    ):
        raise RunnerError("job_template_drift")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("runner-preview", "runner-provision", "runner-start"))
    parser.add_argument("--subscription", required=True, choices=(SUBSCRIPTION,))
    parser.add_argument("--approve-change", action="store_true")
    args = parser.parse_args(argv)
    if args.action != "runner-preview" and not args.approve_change:
        print(
            "PLAN ONLY: no Azure process started. Independent review and --approve-change required."
        )
        return 0
    deployment = [
        "--resource-group",
        GROUP,
        "--name",
        "private-metadata-runner",
        "--template-file",
        str(ROOT / "infra/private-runner.bicep"),
        "--mode",
        "Incremental",
        "--parameters",
        "environmentName=dev",
        "enablePrivateMetadataRunner=true",
    ]
    try:
        if args.action == "runner-start":
            # No --command/--args/--yaml execution overrides are accepted.
            validate_job()
            running = azure(
                [
                    "containerapp",
                    "job",
                    "execution",
                    "list",
                    "-g",
                    GROUP,
                    "-n",
                    JOB,
                    "--query",
                    "[?properties.status=='Running'].name",
                ]
            )
            if running:
                raise RunnerError("execution_already_running")
            execution = azure(
                ["containerapp", "job", "start", "-g", GROUP, "-n", JOB, "--query", "name"]
            )
            if not execution:
                raise RunnerError("execution_not_confirmed")
            print('{"schema":"private-runner-control/v1","status":"execution_requested"}')
            return 0
        preview = azure(
            [
                "deployment",
                "group",
                "what-if",
                *deployment,
                "--no-pretty-print",
                "--result-format",
                "FullResourcePayloads",
            ]
        )
        summary = summarize_preview(preview)
        print(json.dumps({"schema": "private-runner-preview/v1", "changes": summary}))
        if args.action == "runner-provision":
            state = azure(
                [
                    "deployment",
                    "group",
                    "create",
                    *deployment,
                    "--query",
                    "properties.provisioningState",
                ]
            )
            if state != "Succeeded":
                raise RunnerError("provision_not_succeeded")
            print('{"schema":"private-runner-control/v1","status":"provisioned_not_started"}')
        return 0
    except RunnerError as error:
        print(json.dumps({"schema": "private-runner-control/v1", "status": str(error)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
