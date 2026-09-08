"""Read-only operator checks; never evidence of target managed-identity invocation."""

from __future__ import annotations

import argparse
import json
import os
import subprocess

FOUNDRY_USER = "53ca6127-db72-4b80-b1b0-d745d6d5456d"
AGENT_CONSUMER = "eed3b665-ab3a-47b6-8f48-c9382fb1dad6"


class PreflightError(Exception):
    pass


def az_json(*args: str) -> object:
    try:
        result = subprocess.run(
            ["az", *args, "--output", "json", "--only-show-errors"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={**os.environ, "AZURE_EXTENSION_USE_DYNAMIC_INSTALL": "no"},
        )
    except FileNotFoundError:
        raise PreflightError("azure_cli_missing") from None
    except subprocess.TimeoutExpired:
        raise PreflightError("azure_query_timeout") from None
    if result.returncode:
        error = result.stderr.lower()
        for code, markers in (
            ("authentication_unavailable", ("az login", "aadsts", "expired")),
            (
                "network_access_blocked",
                ("publicnetworkaccessdisabled", "public access is disabled"),
            ),
            ("authorization_denied", ("authorizationfailed", "forbidden", "permissiondenied")),
            ("resource_missing", ("resourcenotfound", "resourcegroupnotfound")),
        ):
            if any(marker in error for marker in markers):
                raise PreflightError(code)
        raise PreflightError("azure_query_failed")
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise PreflightError("invalid_azure_json") from None


def has_role(assignments: list, principal: str | None, role: str, scope: str) -> bool:
    return bool(principal) and any(
        assignment.get("principalId", "").lower() == principal.lower()
        and assignment.get("roleDefinitionId", "").lower().endswith("/" + role)
        and assignment.get("scope", "").lower() == scope.lower()
        for assignment in assignments
    )


def preflight(args: argparse.Namespace) -> dict:
    subscription = args.subscription
    group = az_json(
        "group",
        "show",
        "--subscription",
        subscription,
        "--name",
        args.resource_group,
        "--query",
        '{id:id,environment:tags."azd-env-name"}',
    )
    if group.get("environment") != "dev":
        raise PreflightError("resource_group_not_tagged_dev")
    account_id = f"{group['id']}/providers/Microsoft.CognitiveServices/accounts/{args.account}"
    project_id = f"{account_id}/projects/{args.project}"
    project = az_json(
        "rest",
        "--method",
        "get",
        "--url",
        f"https://management.azure.com{project_id}?api-version=2025-06-01",
        "--query",
        "{principal:identity.principalId,endpoints:properties.endpoints}",
    )
    endpoint = (project.get("endpoints") or {}).get("AI Foundry API", "")
    # Do not forward a data-plane token to an arbitrary endpoint returned by configuration.
    expected_endpoint = f"https://{args.account}.services.ai.azure.com/api/projects/{args.project}"
    if not isinstance(endpoint, str) or endpoint != expected_endpoint:
        raise PreflightError("unexpected_project_endpoint")
    app = az_json(
        "containerapp",
        "show",
        "--subscription",
        subscription,
        "--resource-group",
        args.resource_group,
        "--name",
        args.container_app,
        "--query",
        "{principal:identity.principalId,revision:properties.latestReadyRevisionName,"
        "containers:properties.template.containers[].{name:name,image:image,"
        "config:env[?name==`APP_ENV` || name==`FOUNDRY_PROJECT_ENDPOINT`]},"
        "traffic:properties.configuration.ingress.traffic}",
    )
    containers = app.get("containers", [])
    if len(containers) != 1:
        raise PreflightError("expected_single_application_container")
    config = {item["name"]: item.get("value") for item in containers[0].get("config", [])}
    if config.get("APP_ENV") not in ("dev", "development"):
        raise PreflightError("application_not_dev")
    assignments = az_json(
        "role",
        "assignment",
        "list",
        "--subscription",
        subscription,
        "--scope",
        project_id,
        "--include-inherited",
        "--fill-principal-name",
        "false",
        "--query",
        "[].{principalId:principalId,roleDefinitionId:roleDefinitionId,scope:scope}",
    )
    agents = az_json(
        "rest",
        "--method",
        "get",
        "--resource",
        "https://ai.azure.com",
        "--url",
        f"{endpoint}/agents?api-version=2025-11-15-preview",
        "--query",
        "{names:data[].name,hasMore:has_more}",
    )
    if not isinstance(agents.get("names"), list) or not isinstance(agents.get("hasMore"), bool):
        raise PreflightError("unexpected_agent_list")
    checks = {
        "projectIdentityFoundryUserAtAccount": has_role(
            assignments, project.get("principal"), FOUNDRY_USER, account_id
        ),
        "appIdentityAgentConsumerAtProject": has_role(
            assignments, app.get("principal"), AGENT_CONSUMER, project_id
        ),
        "appProjectEndpointConfigured": config.get("FOUNDRY_PROJECT_ENDPOINT") == endpoint,
        "agentNamePresent": args.agent_name in agents["names"],
        "agentInventoryComplete": not agents["hasMore"],
    }
    return {
        "environment": "dev",
        "checks": checks,
        "prerequisitesReady": all(checks.values()),
        "liveAcceptance": False,
        "identityUsed": "azure_cli_operator_not_target_managed_identity",
        "agentName": args.agent_name,
        "projectEndpoint": endpoint,
        "projectPrincipalId": project.get("principal"),
        "appPrincipalId": app.get("principal"),
        "latestReadyRevision": app.get("revision"),
        "configuredImage": containers[0].get("image"),
        "traffic": app.get("traffic"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("subscription", "resource-group", "account", "project", "container-app"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--agent-name", default="card-orchestrator")
    args = parser.parse_args(argv)
    try:
        report = preflight(args)
    except PreflightError as error:
        print(json.dumps({"liveAcceptance": False, "errorCode": str(error)}, sort_keys=True))
        return 2
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0 if report["prerequisitesReady"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
