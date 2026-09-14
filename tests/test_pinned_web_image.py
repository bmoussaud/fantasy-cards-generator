import copy
import importlib.util
import io
import json
import os
import subprocess
import urllib.error
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def helper():
    path = REPO_ROOT / "scripts" / "pinned_web_image.py"
    spec = importlib.util.spec_from_file_location("pinned_web_image", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def registry(helper):
    return {
        "id": helper.ACR_ID.swapcase(),
        "name": helper.REGISTRY,
        "type": "mIcRoSoFt.CoNtAiNeRrEgIsTrY/ReGiStRiEs",
        "location": "eastus2",
        "loginServer": helper.LOGIN_SERVER.upper(),
    }


@pytest.fixture
def snapshot(helper):
    return {
        "id": helper.APP_ID.swapcase(),
        "name": helper.APP,
        "type": "mIcRoSoFt.ApP/CoNtAiNeRaPpS",
        "location": "eastus2",
        "tagFields": ["owner", "azd-service-name"],
        "azdServiceName": "web-nat",
        "identity": {
            "type": "SystemAssigned, UserAssigned",
            "principalId": "principal",
            "tenantId": "tenant",
            "userAssignedIdentities": {helper.ACR_PULL_ID.swapcase(): {}},
        },
        "properties": {
            "provisioningState": "Succeeded",
            "runningStatus": "Running",
            "latestRevisionName": "fcag-dev-app--before",
            "latestReadyRevisionName": "fcag-dev-app--before",
            "latestRevisionFqdn": "fcag-dev-app--before.example",
            "environmentId": "/subscriptions/example/environment",
            "managedEnvironmentId": "/subscriptions/example/environment",
            "workloadProfileName": "Consumption",
            "configuration": {
                "activeRevisionsMode": "Single",
                "agentSettings": None,
                "dapr": {
                    "enabled": False,
                    "appProtocol": "http",
                    "enableApiLogging": False,
                },
                "identitySettings": [{"identity": "system", "lifecycle": "All"}],
                "ingress": {
                    "additionalPortMappings": [
                        {"external": False, "targetPort": 9000, "exposedPort": 9000}
                    ],
                    "allowInsecure": False,
                    "clientCertificateMode": "ignore",
                    "corsPolicy": {
                        "allowCredentials": False,
                        "allowedHeaders": ["x-request-id"],
                        "allowedMethods": ["GET"],
                        "allowedOrigins": ["https://example.test"],
                        "exposeHeaders": ["x-request-id"],
                        "maxAge": 60,
                    },
                    "customDomains": [
                        {
                            "bindingType": "SniEnabled",
                            "certificateId": "/subscriptions/example/certificate",
                            "name": "cards.example.test",
                        }
                    ],
                    "exposedPort": 0,
                    "external": True,
                    "fqdn": "fcag-dev-app.example.azurecontainerapps.io",
                    "ipSecurityRestrictions": [
                        {
                            "action": "Allow",
                            "description": "test",
                            "ipAddressRange": "203.0.113.0/24",
                            "name": "example",
                        }
                    ],
                    "stickySessions": {"affinity": "none"},
                    "targetPort": 8000,
                    "targetPortHttpScheme": None,
                    "traffic": [{"latestRevision": True, "weight": 100}],
                    "transport": "auto",
                },
                "maxInactiveRevisions": 20,
                "registries": [
                    {
                        "server": helper.LOGIN_SERVER.upper(),
                        "identity": helper.ACR_PULL_ID.swapcase(),
                        "passwordSecretRef": "",
                        "username": "",
                    }
                ],
                "revisionTransitionThreshold": None,
                "runtime": {"java": {"enableMetrics": False}},
                "secrets": [
                    {
                        "name": "applicationinsights-connection-string",
                        "identity": "system",
                        "keyVaultUrl": "https://vault.example/secrets/appinsights",
                    }
                ],
                "service": {"type": "app"},
                "targetLabel": None,
            },
            "template": {
                "revisionSuffix": None,
                "terminationGracePeriodSeconds": 30,
                "containers": [
                    {
                        "name": "web",
                        "image": f"{helper.LOGIN_SERVER}/{helper.REPOSITORY}:current",
                        "command": ["uvicorn"],
                        "args": ["app.entrypoint:app"],
                        "env": [
                            {"name": "ENTRA_CLIENT_ID", "value": "public-client-id"},
                            {
                                "name": "APPLICATIONINSIGHTS_CONNECTION_STRING",
                                "secretRef": "applicationinsights-connection-string",
                            },
                        ],
                        "resources": {
                            "cpu": 0.5,
                            "memory": "1Gi",
                            "ephemeralStorage": "2Gi",
                        },
                        "probes": [
                            {
                                "type": "Readiness",
                                "httpGet": {"path": "/healthz", "port": 8000},
                            }
                        ],
                        "volumeMounts": [{"volumeName": "assets", "mountPath": "/assets"}],
                    }
                ],
                "initContainers": [
                    {
                        "name": "init",
                        "image": "example/init@sha256:abc",
                        "resources": {
                            "cpu": 0.25,
                            "memory": "0.5Gi",
                            "ephemeralStorage": "1Gi",
                        },
                    }
                ],
                "scale": {
                    "minReplicas": 1,
                    "maxReplicas": 2,
                    "rules": [
                        {
                            "name": "http",
                            "http": {"metadata": {"concurrentRequests": "10"}},
                        }
                    ],
                },
                "serviceBinds": [{"name": "cache", "serviceId": "cache-id"}],
                "volumes": [{"name": "assets", "storageType": "AzureFile"}],
            },
            "revisionState": {
                "name": "fcag-dev-app--before",
                "active": True,
                "healthState": "Healthy",
                "provisioningState": "Provisioned",
                "replicas": 1,
                "runningState": "Running",
            },
        },
    }


def app_records(snapshot):
    properties = snapshot["properties"]
    configuration = properties["configuration"]
    app_data = {
        "id": snapshot["id"],
        "name": snapshot["name"],
        "type": snapshot["type"],
        "location": snapshot["location"],
        "tagFields": snapshot["tagFields"],
        "azdServiceName": snapshot["azdServiceName"],
        "identity": snapshot["identity"],
        "provisioningState": properties["provisioningState"],
        "runningStatus": properties["runningStatus"],
        "latestRevisionName": properties["latestRevisionName"],
        "latestReadyRevisionName": properties["latestReadyRevisionName"],
        "latestRevisionFqdn": properties["latestRevisionFqdn"],
        "environmentId": properties["environmentId"],
        "managedEnvironmentId": properties["managedEnvironmentId"],
        "workloadProfileName": properties["workloadProfileName"],
        "configurationFields": list(configuration),
        "secretCount": len(configuration["secrets"]),
        "activeRevisionsMode": configuration["activeRevisionsMode"],
        "agentSettings": configuration.get("agentSettings"),
        "dapr": configuration["dapr"],
        "identitySettings": configuration["identitySettings"],
        "ingress": configuration["ingress"],
        "maxInactiveRevisions": configuration["maxInactiveRevisions"],
        "registries": configuration["registries"],
        "revisionTransitionThreshold": configuration.get("revisionTransitionThreshold"),
        "runtime": configuration["runtime"],
        "service": configuration["service"],
        "targetLabel": configuration.get("targetLabel"),
    }
    records = [{"recordType": "app", "data": app_data}]
    for secret in configuration["secrets"]:
        records.append(
            {
                "recordType": "secret",
                "data": {
                    "latestRevisionName": properties["latestRevisionName"],
                    "fields": [*secret, "value"],
                    "name": secret["name"],
                    "identity": secret.get("identity", ""),
                    "keyVaultUrl": secret.get("keyVaultUrl", ""),
                },
            }
        )
    return records


def app_query_result(helper, snapshot, query):
    records = app_records(snapshot)
    if query == helper.APP_CONFIGURATION_QUERY:
        return records[:1]
    if query == helper.APP_SECRET_METADATA_QUERY:
        return records[1:]
    pytest.fail("unexpected Resource Graph query")


def template_records(snapshot, group):
    revision = snapshot["properties"]["latestRevisionName"]
    records = []
    for container_index, container in enumerate(snapshot["properties"]["template"].get(group, [])):
        environment = container.get("env", [])
        records.append(
            {
                "recordType": "container",
                "data": {
                    "latestRevisionName": revision,
                    "containerGroup": group,
                    "containerIndex": container_index,
                    "name": container["name"],
                    "fields": list(container),
                    "environmentCount": len(environment),
                },
            }
        )
        for environment_index, variable in enumerate(environment):
            records.append(
                {
                    "recordType": "environment",
                    "data": {
                        "latestRevisionName": revision,
                        "containerGroup": group,
                        "containerIndex": container_index,
                        "environmentIndex": environment_index,
                        "name": variable["name"],
                        "secretRef": variable.get("secretRef", ""),
                        "fields": list(variable),
                    },
                }
            )
    return records


def revision_response(helper, snapshot, *, created_time="2026-09-14T08:00:00Z"):
    revision = snapshot["properties"]["latestRevisionName"]
    state = snapshot["properties"]["revisionState"]
    return {
        "id": f"{helper.APP_ID}/revisions/{revision}",
        "name": revision,
        "type": "Microsoft.App/containerApps/revisions",
        "properties": {
            "active": state["active"],
            "createdTime": created_time,
            "fqdn": f"{revision}.example",
            "healthState": state["healthState"],
            "provisioningState": state["provisioningState"],
            "replicas": state["replicas"],
            "runningState": state["runningState"],
            "template": copy.deepcopy(snapshot["properties"]["template"]),
        },
    }


def revision_list(*revisions, next_link=None):
    result = {"value": [copy.deepcopy(revision) for revision in revisions]}
    if next_link is not None:
        result["nextLink"] = next_link
    return result


def rollout_snapshot(helper, snapshot, *, state):
    result = copy.deepcopy(snapshot)
    properties = result["properties"]
    properties["latestRevisionName"] = "fcag-dev-app--after"
    properties["template"]["containers"][0]["image"] = helper.TARGET_IMAGE
    properties["revisionState"]["name"] = "fcag-dev-app--after"
    if state == "progressing":
        properties["provisioningState"] = "InProgress"
        properties["runningStatus"] = "Progressing"
        properties["revisionState"].update(
            active=False,
            healthState="None",
            provisioningState="Provisioning",
            replicas=0,
            runningState="Processing",
        )
    elif state == "ready":
        properties["latestReadyRevisionName"] = "fcag-dev-app--after"
    elif state == "failed":
        properties["provisioningState"] = "Failed"
        properties["runningStatus"] = "Progressing"
        properties["revisionState"].update(
            active=False,
            healthState="Unhealthy",
            provisioningState="Failed",
            replicas=0,
            runningState="Failed",
        )
    return result


def test_patch_payload_changes_only_web_image_and_omits_read_only_fields(helper, snapshot):
    before = copy.deepcopy(snapshot)
    payload = helper.patch_payload(snapshot)

    assert set(payload) == {"location", "properties"}
    assert payload["location"] == before["location"]
    assert set(payload["properties"]) == {"template"}
    assert set(payload["properties"]["template"]) == {"containers"}
    assert "configuration" not in json.dumps(payload)

    expected = copy.deepcopy(before["properties"]["template"]["containers"])
    expected[0]["image"] = helper.TARGET_IMAGE
    expected[0]["resources"].pop("ephemeralStorage")
    assert payload["properties"]["template"]["containers"] == expected
    assert snapshot == before


def test_direct_patch_uses_exact_arm_request_without_reading_response(
    helper, monkeypatch, snapshot
):
    recorded = {}

    class Response:
        status = 202

        def getcode(self):
            return self.status

        def geturl(self):
            return helper.APP_URL

        def read(self, size=-1):
            pytest.fail("PATCH response body must not be read")

        def close(self):
            recorded["closed"] = True

    def fake_open(request, timeout):
        recorded.update(request=request, timeout=timeout)
        return Response()

    monkeypatch.setattr(helper, "open_url", fake_open)
    payload = helper.patch_payload(snapshot)
    helper.rest_patch("opaque-test-token", payload)

    request = recorded["request"]
    headers = {name.casefold(): value for name, value in request.header_items()}
    assert request.full_url == helper.APP_URL
    assert request.get_method() == "PATCH"
    assert set(headers) == {"accept", "authorization", "content-type"}
    assert headers["authorization"].startswith("Bearer ")
    assert headers["content-type"] == "application/json"
    assert json.loads(request.data) == payload
    assert recorded["closed"] is True


def test_direct_revision_list_follows_only_expected_arm_pagination(helper, snapshot, monkeypatch):
    first = revision_response(helper, snapshot)
    after = rollout_snapshot(helper, snapshot, state="ready")
    second = revision_response(helper, after, created_time="2026-09-14T08:01:00+00:00")
    next_link = f"{helper.REVISIONS_URL}&pageSize=50&skipToken=opaque"
    responses = iter(
        [
            revision_list(first, next_link=next_link),
            revision_list(second),
        ]
    )
    urls = []

    def fake_request(_token, *, method, url, **_kwargs):
        assert method == "GET"
        urls.append(url)
        return next(responses)

    monkeypatch.setattr(helper, "arm_request", fake_request)
    assert [item["name"] for item in helper.list_revisions("opaque-test-token")] == [
        "fcag-dev-app--before",
        "fcag-dev-app--after",
    ]
    assert urls == [helper.REVISIONS_URL, next_link]

    with pytest.raises(helper.PreflightError, match="revision_pagination_invalid"):
        helper.validate_revisions_url(
            "https://example.test/subscriptions/example/revisions"
            f"?api-version={helper.API_VERSION}"
        )
    with pytest.raises(helper.PreflightError, match="revision_pagination_invalid"):
        helper.validate_revisions_url(f"{helper.REVISIONS_URL}&pageSize=50&$skiptoken=opaque")


def test_server_side_queries_never_project_secret_or_environment_values(helper):
    queries = (
        helper.APP_CONFIGURATION_QUERY,
        helper.APP_SECRET_METADATA_QUERY,
        helper.MAIN_CONTAINER_METADATA_QUERY,
        helper.MAIN_ENVIRONMENT_METADATA_QUERY,
        helper.INIT_CONTAINER_METADATA_QUERY,
        helper.INIT_ENVIRONMENT_METADATA_QUERY,
    )
    assert all("\nunion\n" not in f"\n{query.casefold()}\n" for query in queries)
    assert "secret.value" not in helper.APP_SECRET_METADATA_QUERY
    assert "properties.template" not in helper.APP_CONFIGURATION_QUERY
    assert "environment.value" not in helper.MAIN_ENVIRONMENT_METADATA_QUERY
    assert "environment.value" not in helper.INIT_ENVIRONMENT_METADATA_QUERY
    assert "bag_keys(secret)" in helper.APP_SECRET_METADATA_QUERY
    assert "bag_keys(environment)" in helper.MAIN_ENVIRONMENT_METADATA_QUERY


def test_resource_graph_compile_error_reports_only_bounded_structured_detail(helper, monkeypatch):
    body = json.dumps(
        {
            "error": {
                "code": "BadRequest",
                "message": "support metadata must not be surfaced",
                "details": [
                    {"code": "InvalidQuery", "message": "Query is invalid."},
                    {
                        "code": "Operator_FailedToResolveEntity",
                        "message": "'where' operator: Failed to resolve table named 'resources'",
                    },
                ],
                "authorization": "must-not-leak",
            }
        }
    ).encode()

    def fake_open(request, _timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            400,
            "Bad Request",
            {},
            io.BytesIO(body),
        )

    monkeypatch.setattr(helper, "open_url", fake_open)
    with pytest.raises(helper.PreflightError) as excinfo:
        helper.resource_graph_records("opaque-test-token", helper.ACR_METADATA_QUERY)

    detail = str(excinfo.value)
    assert detail == (
        "resource_graph_query_invalid:Operator_FailedToResolveEntity:"
        "'where' operator: Failed to resolve table named 'resources'"
    )
    assert "support metadata" not in detail
    assert "authorization" not in detail
    assert "opaque-test-token" not in detail


def test_arm_token_is_captured_in_memory_without_output(helper, monkeypatch, capsys):
    recorded = {}

    def fake_run(args):
        recorded["args"] = args
        return {"accessToken": "opaque-test-token"}

    monkeypatch.setattr(helper, "run_azure_json", fake_run)
    assert helper.get_arm_token() == "opaque-test-token"
    assert recorded["args"] == [
        "az",
        "account",
        "get-access-token",
        "--subscription",
        helper.SUBSCRIPTION,
        "--resource",
        helper.ARM_RESOURCE,
        "--only-show-errors",
        "--output",
        "json",
    ]
    assert capsys.readouterr().out == ""


def test_configuration_metadata_is_complete_and_sanitized(helper, snapshot):
    result = helper.app_metadata_from_records(app_records(snapshot))
    configuration = result["properties"]["configuration"]

    assert set(configuration) == helper.CONFIGURATION_FIELDS
    assert configuration["maxInactiveRevisions"] == 20
    assert configuration["secrets"] == snapshot["properties"]["configuration"]["secrets"]
    assert all("value" not in secret for secret in configuration["secrets"])


def test_configuration_accepts_current_nullable_aca_metadata_fields(helper, snapshot):
    records = app_records(snapshot)
    app_data = records[0]["data"]
    app_data["agentSettings"] = {"discoveryMode": "Disabled", "isAgent": False}
    app_data["maxInactiveRevisions"] = None
    app_data["ingress"] = {
        **app_data["ingress"],
        "additionalPortMappings": None,
        "clientCertificateMode": None,
        "corsPolicy": None,
        "customDomains": None,
        "ipSecurityRestrictions": None,
        "stickySessions": None,
        "targetPortHttpScheme": None,
    }
    app_data["dapr"] = None
    app_data["runtime"] = None
    app_data["service"] = None

    configuration = helper.app_metadata_from_records(records)["properties"]["configuration"]

    assert configuration["agentSettings"] == {
        "discoveryMode": "Disabled",
        "isAgent": False,
    }
    assert configuration["maxInactiveRevisions"] is None
    assert configuration["revisionTransitionThreshold"] is None
    assert configuration["targetLabel"] is None
    assert configuration["ingress"]["targetPortHttpScheme"] is None


def test_configuration_unknown_fields_fail_closed(helper, snapshot):
    records = app_records(snapshot)
    records[0]["data"]["configurationFields"].append("futureMutableField")
    with pytest.raises(helper.PreflightError, match="configuration_schema_invalid"):
        helper.app_metadata_from_records(records)

    records = app_records(snapshot)
    records[0]["data"]["ingress"]["futureIngressField"] = True
    with pytest.raises(helper.PreflightError, match="configuration_schema_invalid"):
        helper.app_metadata_from_records(records)

    records = app_records(snapshot)
    records[1]["data"]["fields"].append("futureSecretField")
    with pytest.raises(helper.PreflightError, match="secret_metadata_invalid"):
        helper.app_metadata_from_records(records)


def test_environment_guard_blocks_provider_secrets_and_unreviewed_direct_values(helper):
    with pytest.raises(helper.PreflightError, match="provider_secret_env_present"):
        helper.validate_environment_metadata(
            name="APP_SESSION_SECRET_KEY",
            fields=["name", "secretRef"],
            secret_ref="app-session-secret-key",
        )
    with pytest.raises(helper.PreflightError, match="direct_environment_not_allowlisted"):
        helper.validate_environment_metadata(
            name="UNREVIEWED_SETTING",
            fields=["name", "value"],
            secret_ref="",
        )
    with pytest.raises(helper.PreflightError, match="secret_reference_invalid"):
        helper.validate_environment_metadata(
            name="APPLICATIONINSIGHTS_CONNECTION_STRING",
            fields=["name", "secretRef"],
            secret_ref="wrong-secret",
        )


def test_safe_snapshot_binds_server_side_guard_to_exact_immutable_revision(
    helper, snapshot, monkeypatch
):
    revision = revision_response(helper, snapshot)

    def fake_resource_graph(_token, query):
        return copy.deepcopy(app_query_result(helper, snapshot, query))

    monkeypatch.setattr(helper, "resource_graph_records", fake_resource_graph)
    monkeypatch.setattr(helper, "list_revisions", lambda _token: [copy.deepcopy(revision)])
    monkeypatch.setattr(helper, "read_revision", lambda _token, _name: copy.deepcopy(revision))

    result = helper.safe_snapshot("opaque-test-token")
    assert result == snapshot


def test_safe_snapshot_uses_direct_active_revision_when_resource_graph_is_stale(
    helper, snapshot, monkeypatch
):
    current = rollout_snapshot(helper, snapshot, state="ready")
    direct = revision_response(
        helper,
        current,
        created_time="2026-09-14T08:01:00+00:00",
    )
    monkeypatch.setattr(
        helper,
        "resource_graph_records",
        lambda _token, query: copy.deepcopy(app_query_result(helper, snapshot, query)),
    )
    monkeypatch.setattr(helper, "list_revisions", lambda _token: [copy.deepcopy(direct)])
    monkeypatch.setattr(helper, "read_revision", lambda _token, _name: copy.deepcopy(direct))

    result = helper.safe_snapshot("opaque-test-token")
    assert result["properties"]["latestRevisionName"] == "fcag-dev-app--after"
    assert result["properties"]["template"] == direct["properties"]["template"]
    assert result["properties"]["configuration"] == snapshot["properties"]["configuration"]


def test_registry_and_pull_identity_are_fixed_case_insensitive_arm_ids(helper, snapshot, registry):
    helper.validate_registry_contract(snapshot, registry)

    wrong = copy.deepcopy(snapshot)
    wrong["properties"]["configuration"]["registries"][0]["identity"] = (
        "/subscriptions/example/resourceGroups/example/providers/"
        "Microsoft.ManagedIdentity/userAssignedIdentities/wrong"
    )
    with pytest.raises(helper.PreflightError, match="registry_contract_invalid"):
        helper.validate_registry_contract(wrong, registry)

    wrong = copy.deepcopy(snapshot)
    wrong["identity"]["userAssignedIdentities"] = {}
    with pytest.raises(helper.PreflightError, match="pull_identity_invalid"):
        helper.validate_registry_contract(wrong, registry)


def test_preservation_contract_ignores_only_revision_state_image_and_read_only_storage(
    helper, snapshot
):
    after = rollout_snapshot(helper, snapshot, state="ready")
    after["properties"]["template"]["containers"][0]["resources"]["ephemeralStorage"] = "3Gi"

    assert helper.preserved_contract(after) == helper.preserved_contract(snapshot)

    after["properties"]["template"]["containers"][0]["resources"]["cpu"] = 1
    assert helper.preserved_contract(after) != helper.preserved_contract(snapshot)


def test_nullable_init_containers_are_treated_as_empty_metadata(helper, snapshot):
    snapshot["properties"]["template"]["initContainers"] = None

    assert helper.revision_environment_inventory(snapshot["properties"]["template"])
    assert helper.preserved_contract(snapshot)["properties"]["template"]["initContainers"] is None


def test_wait_for_rollout_polls_recognized_transient_states(helper, snapshot, monkeypatch):
    progressing = rollout_snapshot(helper, snapshot, state="progressing")
    ready = rollout_snapshot(helper, snapshot, state="ready")
    before_revision = revision_response(helper, snapshot)
    progressing_revision = revision_response(
        helper,
        progressing,
        created_time="2026-09-14T08:01:00Z",
    )
    ready_revision = revision_response(
        helper,
        ready,
        created_time="2026-09-14T08:01:00Z",
    )
    inactive_before = copy.deepcopy(before_revision)
    inactive_before["properties"].update(
        active=False,
        healthState="None",
        replicas=0,
        runningState="Stopped",
    )
    lists = iter(
        [
            [copy.deepcopy(before_revision)],
            [copy.deepcopy(before_revision), copy.deepcopy(progressing_revision)],
            [copy.deepcopy(inactive_before), copy.deepcopy(ready_revision)],
        ]
    )
    sleeps = []
    clock = {"value": 0}

    monkeypatch.setattr(helper, "list_revisions", lambda _token: next(lists))
    monkeypatch.setattr(
        helper,
        "read_revision",
        lambda _token, name: (
            copy.deepcopy(before_revision)
            if name == "fcag-dev-app--before"
            else copy.deepcopy(progressing_revision if clock["value"] <= 5 else ready_revision)
        ),
    )
    monkeypatch.setattr(helper, "safe_snapshot", lambda _token: copy.deepcopy(ready))
    monkeypatch.setattr(helper, "healthz", lambda _snapshot: True)
    monkeypatch.setattr(helper.time, "monotonic", lambda: clock["value"])

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["value"] += seconds

    monkeypatch.setattr(helper.time, "sleep", fake_sleep)

    result = helper.wait_for_rollout(
        "opaque-test-token",
        snapshot,
        timeout_seconds=30,
        poll_seconds=5,
    )
    assert result == ready
    assert sleeps == [5, 5]


def test_wait_for_rollout_fails_immediately_on_terminal_state(helper, snapshot, monkeypatch):
    failed = rollout_snapshot(helper, snapshot, state="failed")
    before_revision = revision_response(helper, snapshot)
    failed_revision = revision_response(
        helper,
        failed,
        created_time="2026-09-14T08:01:00Z",
    )
    monkeypatch.setattr(
        helper,
        "list_revisions",
        lambda _token: [copy.deepcopy(before_revision), copy.deepcopy(failed_revision)],
    )
    monkeypatch.setattr(
        helper,
        "read_revision",
        lambda _token, name: copy.deepcopy(
            before_revision if name == "fcag-dev-app--before" else failed_revision
        ),
    )
    monkeypatch.setattr(
        helper.time,
        "sleep",
        lambda _seconds: pytest.fail("terminal failure must not be polled"),
    )
    with pytest.raises(helper.PreflightError, match="rollout_terminal_failure"):
        helper.wait_for_rollout("opaque-test-token", snapshot)


def test_wait_for_rollout_fails_immediately_on_preservation_drift(helper, snapshot, monkeypatch):
    drifted = rollout_snapshot(helper, snapshot, state="progressing")
    drifted["properties"]["template"]["containers"][0]["resources"]["cpu"] = 1
    before_revision = revision_response(helper, snapshot)
    drifted_revision = revision_response(
        helper,
        drifted,
        created_time="2026-09-14T08:01:00Z",
    )
    monkeypatch.setattr(
        helper,
        "list_revisions",
        lambda _token: [copy.deepcopy(before_revision), copy.deepcopy(drifted_revision)],
    )
    monkeypatch.setattr(
        helper,
        "read_revision",
        lambda _token, name: copy.deepcopy(
            before_revision if name == "fcag-dev-app--before" else drifted_revision
        ),
    )
    monkeypatch.setattr(
        helper.time,
        "sleep",
        lambda _seconds: pytest.fail("preservation drift must not be polled"),
    )
    with pytest.raises(helper.PreflightError, match="preservation_drift"):
        helper.wait_for_rollout("opaque-test-token", snapshot)


def test_wait_for_rollout_uses_direct_revisions_while_resource_graph_stays_stale(
    helper, snapshot, monkeypatch
):
    progressing = rollout_snapshot(helper, snapshot, state="progressing")
    ready = rollout_snapshot(helper, snapshot, state="ready")
    before_revision = revision_response(helper, snapshot)
    progressing_revision = revision_response(
        helper,
        progressing,
        created_time="2026-09-14T08:01:00Z",
    )
    ready_revision = revision_response(
        helper,
        ready,
        created_time="2026-09-14T08:01:00Z",
    )
    inactive_before = copy.deepcopy(before_revision)
    inactive_before["properties"].update(
        active=False,
        healthState="None",
        replicas=0,
        runningState="Stopped",
    )
    lists = iter(
        [
            [copy.deepcopy(before_revision), copy.deepcopy(progressing_revision)],
            [copy.deepcopy(inactive_before), copy.deepcopy(ready_revision)],
            [copy.deepcopy(inactive_before), copy.deepcopy(ready_revision)],
            [copy.deepcopy(inactive_before), copy.deepcopy(ready_revision)],
        ]
    )
    clock = {"value": 0}

    monkeypatch.setattr(
        helper,
        "resource_graph_records",
        lambda _token, query: copy.deepcopy(app_query_result(helper, snapshot, query)),
    )
    monkeypatch.setattr(helper, "list_revisions", lambda _token: next(lists))
    monkeypatch.setattr(
        helper,
        "read_revision",
        lambda _token, name: copy.deepcopy(
            before_revision
            if name == "fcag-dev-app--before"
            else (progressing_revision if clock["value"] == 0 else ready_revision)
        ),
    )
    monkeypatch.setattr(helper, "healthz", lambda _snapshot: True)
    monkeypatch.setattr(helper.time, "monotonic", lambda: clock["value"])

    def fake_sleep(seconds):
        clock["value"] += seconds

    monkeypatch.setattr(helper.time, "sleep", fake_sleep)

    result = helper.wait_for_rollout(
        "opaque-test-token",
        snapshot,
        timeout_seconds=30,
        poll_seconds=5,
    )
    assert result["properties"]["latestRevisionName"] == "fcag-dev-app--after"
    assert result["properties"]["template"] == ready_revision["properties"]["template"]
    assert result["properties"]["configuration"] == snapshot["properties"]["configuration"]


def test_deploy_gates_stop_before_patch_without_reviewed_fingerprint(
    helper, snapshot, registry, monkeypatch, capsys
):
    evidence = helper.report(snapshot, registry)
    monkeypatch.setattr(helper, "get_arm_token", lambda: "opaque-test-token")
    monkeypatch.setattr(
        helper,
        "preflight",
        lambda _token: (snapshot, registry, evidence),
    )
    monkeypatch.setattr(
        helper,
        "rest_patch",
        lambda _token, _payload: pytest.fail("patch must not run"),
    )

    assert (
        helper.main(
            [
                "deploy",
                "--subscription",
                helper.SUBSCRIPTION,
                "--approve-change",
                "--reviewed",
            ]
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "reviewed_fingerprint_required" in output
    assert "opaque-test-token" not in output


def test_concurrent_drift_stops_before_patch(helper, snapshot, registry, monkeypatch, capsys):
    evidence = helper.report(snapshot, registry)
    drifted = copy.deepcopy(snapshot)
    drifted["properties"]["template"]["scale"]["maxReplicas"] = 3
    monkeypatch.setattr(helper, "get_arm_token", lambda: "opaque-test-token")
    monkeypatch.setattr(
        helper,
        "preflight",
        lambda _token: (snapshot, registry, evidence),
    )
    monkeypatch.setattr(helper, "registry_metadata", lambda _token: registry)
    monkeypatch.setattr(helper, "safe_snapshot", lambda _token: drifted)
    monkeypatch.setattr(
        helper,
        "rest_patch",
        lambda _token, _payload: pytest.fail("patch must not run"),
    )

    assert (
        helper.main(
            [
                "deploy",
                "--subscription",
                helper.SUBSCRIPTION,
                "--expect-fingerprint",
                evidence["baseline_fingerprint"],
                "--approve-change",
                "--reviewed",
            ]
        )
        == 1
    )
    assert "concurrent_drift_before_patch" in capsys.readouterr().out


def test_stale_resource_graph_with_newer_active_direct_revision_stops_before_patch(
    helper, snapshot, registry, monkeypatch, capsys
):
    evidence = helper.report(snapshot, registry)
    current = rollout_snapshot(helper, snapshot, state="ready")
    direct = revision_response(
        helper,
        current,
        created_time="2026-09-14T08:01:00Z",
    )
    monkeypatch.setattr(helper, "get_arm_token", lambda: "opaque-test-token")
    monkeypatch.setattr(
        helper,
        "preflight",
        lambda _token: (snapshot, registry, evidence),
    )
    monkeypatch.setattr(helper, "registry_metadata", lambda _token: registry)
    monkeypatch.setattr(
        helper,
        "resource_graph_records",
        lambda _token, query: copy.deepcopy(app_query_result(helper, snapshot, query)),
    )
    monkeypatch.setattr(helper, "list_revisions", lambda _token: [copy.deepcopy(direct)])
    monkeypatch.setattr(helper, "read_revision", lambda _token, _name: copy.deepcopy(direct))
    monkeypatch.setattr(
        helper,
        "rest_patch",
        lambda _token, _payload: pytest.fail("patch must not run"),
    )

    assert (
        helper.main(
            [
                "deploy",
                "--subscription",
                helper.SUBSCRIPTION,
                "--expect-fingerprint",
                evidence["baseline_fingerprint"],
                "--approve-change",
                "--reviewed",
            ]
        )
        == 1
    )
    assert "concurrent_drift_before_patch" in capsys.readouterr().out


def test_stale_resource_graph_with_in_progress_direct_revision_stops_before_patch(
    helper, snapshot, registry, monkeypatch, capsys
):
    evidence = helper.report(snapshot, registry)
    before_revision = revision_response(helper, snapshot)
    progressing = rollout_snapshot(helper, snapshot, state="progressing")
    progressing_revision = revision_response(
        helper,
        progressing,
        created_time="2026-09-14T08:01:00Z",
    )
    monkeypatch.setattr(helper, "get_arm_token", lambda: "opaque-test-token")
    monkeypatch.setattr(
        helper,
        "preflight",
        lambda _token: (snapshot, registry, evidence),
    )
    monkeypatch.setattr(helper, "registry_metadata", lambda _token: registry)
    monkeypatch.setattr(
        helper,
        "resource_graph_records",
        lambda _token, query: copy.deepcopy(app_query_result(helper, snapshot, query)),
    )
    monkeypatch.setattr(
        helper,
        "list_revisions",
        lambda _token: [
            copy.deepcopy(before_revision),
            copy.deepcopy(progressing_revision),
        ],
    )
    monkeypatch.setattr(
        helper,
        "rest_patch",
        lambda _token, _payload: pytest.fail("patch must not run"),
    )

    assert (
        helper.main(
            [
                "deploy",
                "--subscription",
                helper.SUBSCRIPTION,
                "--expect-fingerprint",
                evidence["baseline_fingerprint"],
                "--approve-change",
                "--reviewed",
            ]
        )
        == 1
    )
    assert "revision_in_progress" in capsys.readouterr().out


def test_success_requires_exact_image_health_and_preservation(
    helper, snapshot, registry, monkeypatch, capsys
):
    evidence = helper.report(snapshot, registry)
    after = rollout_snapshot(helper, snapshot, state="ready")
    patches = []
    monkeypatch.setattr(helper, "get_arm_token", lambda: "opaque-test-token")
    monkeypatch.setattr(
        helper,
        "preflight",
        lambda _token: (snapshot, registry, evidence),
    )
    monkeypatch.setattr(helper, "registry_metadata", lambda _token: registry)
    monkeypatch.setattr(helper, "safe_snapshot", lambda _token: copy.deepcopy(snapshot))
    monkeypatch.setattr(
        helper,
        "rest_patch",
        lambda token, payload: patches.append((token, payload)),
    )
    monkeypatch.setattr(helper, "wait_for_rollout", lambda _token, _before: after)

    assert (
        helper.main(
            [
                "deploy",
                "--subscription",
                helper.SUBSCRIPTION,
                "--expect-fingerprint",
                evidence["baseline_fingerprint"],
                "--approve-change",
                "--reviewed",
            ]
        )
        == 0
    )
    assert patches == [("opaque-test-token", helper.patch_payload(snapshot))]
    output = capsys.readouterr().out
    assert '"status": "succeeded"' in output
    assert f'"image": "{helper.TARGET_IMAGE}"' in output
    assert '"healthz": 200' in output
    assert '"configuration_transmitted": false' in output
    assert "opaque-test-token" not in output


def test_root_deploy_script_forwards_exact_pinned_apply_arguments(tmp_path):
    record = tmp_path / "argv"
    python = tmp_path / "python3"
    python.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$RECORD"\n')
    python.chmod(0o755)
    fingerprint = "a" * 64
    result = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "deploy.sh"),
            "web-pinned",
            "--environment",
            "dev",
            "--subscription",
            "b8ff3e15-7e2d-4fac-a773-992fb59ccedd",
            "--expect-fingerprint",
            fingerprint,
            "--approve-change",
            "--reviewed",
        ],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "RECORD": str(record),
        },
    )
    assert result.returncode == 0
    assert record.read_text().splitlines() == [
        str(REPO_ROOT / "scripts" / "pinned_web_image.py"),
        "deploy",
        "--subscription",
        "b8ff3e15-7e2d-4fac-a773-992fb59ccedd",
        "--approve-change",
        "--reviewed",
        "--expect-fingerprint",
        fingerprint,
    ]
