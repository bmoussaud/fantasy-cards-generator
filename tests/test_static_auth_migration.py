"""Offline guards for the dev static-auth rollback migration."""

import argparse
import copy
import datetime
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "static_auth", ROOT / "deployments/dev-static-auth/migrate.py"
)
static_auth = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(static_auth)

SOURCE = "1" * 40
IMAGE = f"{static_auth.REGISTRY_SERVER}/{static_auth.IMAGE_REPOSITORY}" f"@sha256:{'a' * 64}"
TAGGED_IMAGE = f"{static_auth.REGISTRY_SERVER}/{static_auth.IMAGE_REPOSITORY}:azd-deploy-123456"


def test_published_tags_uses_repository_command_registry_name(monkeypatch):
    tag = {
        "name": "azd-deploy-123456",
        "digest": "sha256:" + "a" * 64,
        "createdTime": "2026-09-15T05:18:37Z",
        "lastUpdateTime": "2026-09-15T05:18:37Z",
    }

    def inventory(args):
        assert args == [
            "az",
            "acr",
            "repository",
            "show-tags",
            "--name",
            static_auth.REGISTRY_NAME,
            "--repository",
            static_auth.IMAGE_REPOSITORY,
            "--detail",
            "--output",
            "json",
        ]
        return [tag, {"name": "unrelated"}]

    monkeypatch.setattr(static_auth, "command", inventory)

    assert static_auth.published_tags() == {
        tag["name"]: {key: tag[key] for key in ("digest", "createdTime", "lastUpdateTime")}
    }


def test_resolve_published_digest_accepts_exact_tag_query_with_null_tags(monkeypatch):
    digest = "sha256:" + "a" * 64
    monkeypatch.setattr(
        static_auth,
        "command",
        lambda values: {"digest": digest, "tags": None},
    )
    assert static_auth.resolve_published_digest(TAGGED_IMAGE) == digest


def test_resolve_published_digest_rejects_conflicting_non_null_tags(monkeypatch):
    monkeypatch.setattr(
        static_auth,
        "command",
        lambda values: {"digest": "sha256:" + "a" * 64, "tags": ["another-tag"]},
    )
    with pytest.raises(static_auth.GateError, match="metadata is not exact"):
        static_auth.resolve_published_digest(TAGGED_IMAGE)


@pytest.fixture
def raw_app():
    environment = static_auth.GROUP + "/providers/Microsoft.App/managedEnvironments/fcag-dev-cae"
    return {
        "id": static_auth.APP,
        "name": static_auth.APP_NAME,
        "type": "Microsoft.App/containerApps",
        "location": "East US 2",
        "tags": {"azd-service-name": "web-nat"},
        "identity": {
            "type": "SystemAssigned, UserAssigned",
            "principalId": static_auth.PRINCIPAL_ID,
            "tenantId": "tenant",
            "userAssignedIdentities": {
                static_auth.GROUP
                + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/fcag-dev-acr-pull": {
                    "clientId": "client",
                    "principalId": "principal",
                }
            },
        },
        "properties": {
            "provisioningState": "Succeeded",
            "runningStatus": "Running",
            "latestRevisionName": "fcag-dev-app--static-rollback-approved",
            "latestReadyRevisionName": "fcag-dev-app--static-rollback-approved",
            "environmentId": environment,
            "managedEnvironmentId": environment,
            "configuration": {
                "activeRevisionsMode": "Single",
                "ingress": {
                    "fqdn": "fcag-dev-app.example.azurecontainerapps.io",
                    "traffic": [{"latestRevision": True, "weight": 100}],
                },
                "secrets": [
                    {"name": "applicationinsights-connection-string"},
                    {"name": "app-session-secret-key"},
                    {"name": "entra-client-secret"},
                ],
            },
            "template": {
                "revisionSuffix": "static-rollback-approved",
                "containers": [
                    {
                        "name": "web",
                        "image": IMAGE,
                        "resources": {
                            "cpu": 0.5,
                            "memory": "1Gi",
                            "ephemeralStorage": "2Gi",
                        },
                        "env": [
                            {
                                "name": "APPLICATIONINSIGHTS_CONNECTION_STRING",
                                "secretRef": "applicationinsights-connection-string",
                            },
                            {"name": "KEY_VAULT_URI", "value": "https://example.invalid/"},
                            {"name": "SECRET_PROVIDER_BACKEND", "value": "azure"},
                            {
                                "name": "APP_SESSION_SECRET_KEY",
                                "secretRef": "app-session-secret-key",
                            },
                            {
                                "name": "ENTRA_CLIENT_SECRET",
                                "secretRef": "entra-client-secret",
                            },
                        ],
                    }
                ],
            },
        },
    }


@pytest.fixture
def raw_vault():
    return {
        "id": static_auth.VAULT,
        "name": static_auth.VAULT_NAME,
        "location": "East US 2",
        "tags": {"environment": "dev"},
        "properties": {
            "tenantId": "31b6a5c6-8762-4d6b-bf6e-f37931c67a75",
            "sku": {"family": "A", "name": "standard"},
            "enablePurgeProtection": True,
            "enableRbacAuthorization": True,
            "publicNetworkAccess": "Disabled",
            "softDeleteRetentionInDays": 90,
            "enabledForTemplateDeployment": False,
            "networkAcls": None,
            "vaultUri": "https://example.invalid/",
            "provisioningState": "Succeeded",
            "privateEndpointConnections": [{"id": "private"}],
        },
    }


def make_receipt(raw_vault):
    receipt = successful_artifact(static_auth.baseline_receipt(raw_vault, SOURCE))
    receipt["artifact"]["pinnedRevision"] = "fcag-dev-app--static-rollback-approved"
    return receipt


def test_receipt_round_trip_accepts_full_commit_sha(raw_vault, tmp_path, monkeypatch):
    path = tmp_path / "receipt.json"
    monkeypatch.setattr(static_auth, "receipt_path", lambda: path)
    receipt = make_receipt(raw_vault)
    static_auth.save_receipt(receipt)
    assert static_auth.load_receipt() == receipt
    assert path.stat().st_mode & 0o777 == 0o600


def args(phase, **overrides):
    values = {
        "phase": phase,
        "apply": False,
        "approve_change": False,
        "reviewed": False,
        "expect_fingerprint": None,
        "rollback_source": SOURCE,
        "rollback_image": IMAGE,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def exact_assignment():
    return {
        "id": f"{static_auth.VAULT}/providers/Microsoft.Authorization/roleAssignments/exact",
        "principalId": static_auth.PRINCIPAL_ID,
        "roleDefinitionId": static_auth.ROLE_DEFINITION,
        "scope": static_auth.VAULT,
    }


def patch_source(monkeypatch):
    monkeypatch.setattr(static_auth, "validate_source", lambda source: "f" * 64)


def test_transfer_adds_exact_native_secrets_and_preserves_app_insights(raw_app):
    snapshot = static_auth.app_snapshot(raw_app)
    result = static_auth.desired(snapshot, "transfer", "static-transfer-test")
    assert result["properties"]["configuration"]["secrets"] == [
        {"name": "applicationinsights-connection-string"},
        {"name": "app-session-secret-key"},
        {"name": "entra-client-secret"},
    ]
    web = result["properties"]["template"]["containers"][0]
    assert web["image"] == IMAGE
    assert {"name": "APP_SESSION_SECRET_KEY", "secretRef": "app-session-secret-key"} in web["env"]
    assert {"name": "ENTRA_CLIENT_SECRET", "secretRef": "entra-client-secret"} in web["env"]


def test_cleanup_removes_only_rotation_env_and_keeps_key_vault_uri(raw_app):
    snapshot = static_auth.app_snapshot(raw_app)
    result = static_auth.desired(snapshot, "cleanup-app", "static-cleanup-test")
    names = {item["name"] for item in result["properties"]["template"]["containers"][0]["env"]}
    assert names.isdisjoint(static_auth.ROTATION_ONLY_ENV)
    assert "KEY_VAULT_URI" in names
    assert {"APP_SESSION_SECRET_KEY", "ENTRA_CLIENT_SECRET"} <= names


@pytest.mark.parametrize(
    ("revision", "image", "source", "message"),
    [
        ("fcag-dev-app--azd-reviewed", "registry/foreign@sha256:" + "a" * 64, SOURCE, "image"),
        ("fcag-dev-app--anything", IMAGE.replace("a" * 64, "b" * 64), SOURCE, "provenance"),
        ("fcag-dev-app--anything", IMAGE, "2" * 40, "provenance"),
    ],
)
def test_cleanup_refuses_arbitrary_revision_wrong_digest_or_wrong_source(
    raw_app, raw_vault, monkeypatch, revision, image, source, message
):
    patch_source(monkeypatch)
    raw_app["properties"]["latestRevisionName"] = revision
    raw_app["properties"]["latestReadyRevisionName"] = revision
    receipt = make_receipt(raw_vault)
    with pytest.raises(static_auth.GateError, match=message):
        static_auth.require_approved_rollback(raw_app, receipt, image, source)


def successful_artifact(receipt):
    commands = {
        "packageCommand": ["azd", "package", "web-nat", "--no-prompt"],
        "deployCommand": [
            "azd",
            "deploy",
            "web-nat",
            "--no-prompt",
            "--timeout",
            "1200",
        ],
    }
    receipt["buildAttempt"] = {
        "status": "succeeded",
        "source": SOURCE,
        "sourceFingerprint": "f" * 64,
        "publishedTag": TAGGED_IMAGE,
        "publishedRevision": "fcag-dev-app--azd-published",
        "image": IMAGE,
        **commands,
    }
    receipt["artifact"] = {
        "source": SOURCE,
        "sourceFingerprint": "f" * 64,
        "image": IMAGE,
        "publishedTag": TAGGED_IMAGE,
        "publishedRevision": "fcag-dev-app--azd-published",
        **commands,
    }
    return receipt


def transfer_vault(raw_vault, receipt):
    result = copy.deepcopy(raw_vault)
    result["properties"] = static_auth.expected_transfer_vault(receipt["vaultBaseline"])[
        "properties"
    ]
    return result


def deploy_fixture(raw_app, raw_vault):
    receipt = static_auth.baseline_receipt(raw_vault, SOURCE)
    vault = transfer_vault(raw_vault, receipt)
    before_tags = {
        "azd-deploy-100": {
            "digest": "sha256:" + "b" * 64,
            "createdTime": "2026-09-15T04:00:00+00:00",
            "lastUpdateTime": "2026-09-15T04:00:00+00:00",
        }
    }
    after = copy.deepcopy(raw_app)
    after["properties"]["template"]["containers"][0]["image"] = TAGGED_IMAGE
    after["properties"]["latestRevisionName"] = "fcag-dev-app--azd-123456"
    after["properties"]["latestReadyRevisionName"] = "fcag-dev-app--azd-123456"
    after_tags = {
        **before_tags,
        "azd-deploy-123456": {
            "digest": "sha256:" + "a" * 64,
            "createdTime": "2026-09-15T05:18:40+00:00",
            "lastUpdateTime": "2026-09-15T05:18:40+00:00",
        },
    }
    return receipt, vault, before_tags, after, after_tags


def prepare_deploy(
    monkeypatch,
    raw_app,
    receipt,
    vault,
    before_tags,
    after,
    after_tags,
    *,
    source_hashes=None,
    azd_failure=None,
):
    hashes = iter(source_hashes or ["f" * 64] * 8)
    apps = iter([raw_app, raw_app, after])
    tags = iter([before_tags, before_tags, after_tags])
    saved = []
    commands = []
    times = iter(
        [
            datetime.datetime(2026, 9, 15, 5, 18, 37, tzinfo=datetime.timezone.utc),
            datetime.datetime(2026, 9, 15, 5, 18, 41, tzinfo=datetime.timezone.utc),
        ]
    )
    monkeypatch.setattr(static_auth, "validate_source", lambda source: next(hashes))
    monkeypatch.setattr(static_auth, "load_receipt", lambda: receipt)
    monkeypatch.setattr(static_auth, "get_vault", lambda: vault)
    monkeypatch.setattr(static_auth, "get_app", lambda: next(apps))
    monkeypatch.setattr(static_auth, "published_tags", lambda: next(tags))
    monkeypatch.setattr(static_auth, "healthz", lambda raw: True)
    monkeypatch.setattr(static_auth, "resolve_published_digest", lambda image: "sha256:" + "a" * 64)
    monkeypatch.setattr(static_auth, "utc_now", lambda: next(times))
    monkeypatch.setattr(
        static_auth,
        "save_receipt",
        lambda value: saved.append(copy.deepcopy(value)),
    )

    def run_azd(values, failure_code):
        commands.append(values)
        if azd_failure == failure_code:
            raise static_auth.GateError(f"Owned azd command failed: {failure_code}")

    monkeypatch.setattr(static_auth, "azd_command", run_azd)
    state = static_auth.deploy_stage_state(receipt, raw_app, vault, "f" * 64, before_tags)
    return saved, commands, static_auth.fingerprint(state)


def test_owned_deploy_records_fresh_build_revision_and_digest(raw_app, raw_vault, monkeypatch):
    receipt, vault, before_tags, after, after_tags = deploy_fixture(raw_app, raw_vault)
    saved, commands, baseline = prepare_deploy(
        monkeypatch, raw_app, receipt, vault, before_tags, after, after_tags
    )
    static_auth.run_deploy_artifact(
        args(
            "deploy",
            apply=True,
            approve_change=True,
            reviewed=True,
            expect_fingerprint=baseline,
        )
    )
    assert commands == [
        ["azd", "package", "web-nat", "--no-prompt"],
        ["azd", "deploy", "web-nat", "--no-prompt", "--timeout", "1200"],
    ]
    assert saved[0]["buildAttempt"]["status"] == "started"
    assert "artifact" not in saved[0]
    assert saved[-1]["buildAttempt"]["status"] == "succeeded"
    assert saved[-1]["artifact"]["image"] == IMAGE
    assert saved[-1]["artifact"]["publishedRevision"] == "fcag-dev-app--azd-123456"


def test_owned_deploy_waits_for_delayed_ready_revision(raw_app, raw_vault, monkeypatch):
    receipt, vault, before_tags, after, after_tags = deploy_fixture(raw_app, raw_vault)
    transient = copy.deepcopy(after)
    transient["properties"]["runningStatus"] = "Progressing"
    transient["properties"]["latestReadyRevisionName"] = raw_app["properties"][
        "latestReadyRevisionName"
    ]
    saved, _, baseline = prepare_deploy(
        monkeypatch, raw_app, receipt, vault, before_tags, after, after_tags
    )
    apps = iter([raw_app, raw_app, transient, after])
    sleeps = []
    monkeypatch.setattr(static_auth, "get_app", lambda: next(apps))
    monkeypatch.setattr(static_auth.time, "sleep", lambda seconds: sleeps.append(seconds))
    static_auth.run_deploy_artifact(
        args(
            "deploy",
            apply=True,
            approve_change=True,
            reviewed=True,
            expect_fingerprint=baseline,
        )
    )
    assert sleeps == [5]
    assert saved[-1]["artifact"]["publishedRevision"] == "fcag-dev-app--azd-123456"


def test_owned_deploy_terminal_revision_failure_is_immediate(raw_app, raw_vault, monkeypatch):
    receipt, vault, before_tags, after, after_tags = deploy_fixture(raw_app, raw_vault)
    after["properties"]["runningStatus"] = "Failed"
    after["properties"]["latestReadyRevisionName"] = raw_app["properties"][
        "latestReadyRevisionName"
    ]
    saved, _, baseline = prepare_deploy(
        monkeypatch, raw_app, receipt, vault, before_tags, after, after_tags
    )
    apps = iter([raw_app, raw_app, after])
    monkeypatch.setattr(static_auth, "get_app", lambda: next(apps))
    monkeypatch.setattr(
        static_auth.time,
        "sleep",
        lambda seconds: pytest.fail("terminal failure must not be retried"),
    )
    with pytest.raises(static_auth.GateError, match="revision failed"):
        static_auth.run_deploy_artifact(
            args(
                "deploy",
                apply=True,
                approve_change=True,
                reviewed=True,
                expect_fingerprint=baseline,
            )
        )
    assert saved[-1]["buildAttempt"]["status"] == "started"
    assert "artifact" not in saved[-1]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda app: app["properties"].update({"latestRevisionName": "fcag-dev-app--foreign"}),
            "unexpected revision",
        ),
        (
            lambda app: app["properties"]["template"]["containers"][0].update(
                {"image": "registry.invalid/foreign:azd-deploy-123456"}
            ),
            "unexpected image",
        ),
        (
            lambda app: app["properties"]["configuration"].update(
                {"activeRevisionsMode": "Multiple"}
            ),
            "Single revision mode",
        ),
    ],
)
def test_owned_deploy_rejects_revision_image_or_config_drift(
    raw_app, raw_vault, monkeypatch, mutate, message
):
    receipt, vault, before_tags, after, after_tags = deploy_fixture(raw_app, raw_vault)
    mutate(after)
    saved, _, baseline = prepare_deploy(
        monkeypatch, raw_app, receipt, vault, before_tags, after, after_tags
    )
    with pytest.raises(static_auth.GateError, match=message):
        static_auth.run_deploy_artifact(
            args(
                "deploy",
                apply=True,
                approve_change=True,
                reviewed=True,
                expect_fingerprint=baseline,
            )
        )
    assert saved[-1]["buildAttempt"]["status"] == "started"
    assert "artifact" not in saved[-1]


def test_owned_deploy_readiness_timeout_cannot_create_artifact(raw_app, raw_vault, monkeypatch):
    receipt, vault, before_tags, after, after_tags = deploy_fixture(raw_app, raw_vault)
    after["properties"]["runningStatus"] = "Progressing"
    after["properties"]["latestReadyRevisionName"] = raw_app["properties"][
        "latestReadyRevisionName"
    ]
    saved, _, baseline = prepare_deploy(
        monkeypatch, raw_app, receipt, vault, before_tags, after, after_tags
    )
    apps = iter([raw_app, raw_app, after])
    monotonic = iter([0, 0, 301])
    monkeypatch.setattr(static_auth, "get_app", lambda: next(apps))
    monkeypatch.setattr(static_auth.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(static_auth.time, "sleep", lambda seconds: None)
    with pytest.raises(static_auth.GateError, match="readiness timed out"):
        static_auth.run_deploy_artifact(
            args(
                "deploy",
                apply=True,
                approve_change=True,
                reviewed=True,
                expect_fingerprint=baseline,
            )
        )
    assert saved[-1]["buildAttempt"]["status"] == "started"
    assert "artifact" not in saved[-1]


def test_owned_deploy_rejects_existing_azd_tag(raw_app, raw_vault, monkeypatch):
    receipt, vault, before_tags, after, after_tags = deploy_fixture(raw_app, raw_vault)
    before_tags[TAGGED_IMAGE.rsplit(":", 1)[1]] = after_tags[TAGGED_IMAGE.rsplit(":", 1)[1]]
    _, _, baseline = prepare_deploy(
        monkeypatch, raw_app, receipt, vault, before_tags, after, after_tags
    )
    with pytest.raises(static_auth.GateError, match="fresh tag"):
        static_auth.run_deploy_artifact(
            args(
                "deploy",
                apply=True,
                approve_change=True,
                reviewed=True,
                expect_fingerprint=baseline,
            )
        )


def test_owned_deploy_rejects_same_old_revision(raw_app, raw_vault, monkeypatch):
    receipt, vault, before_tags, after, after_tags = deploy_fixture(raw_app, raw_vault)
    after["properties"]["latestRevisionName"] = raw_app["properties"]["latestRevisionName"]
    after["properties"]["latestReadyRevisionName"] = raw_app["properties"]["latestRevisionName"]
    _, _, baseline = prepare_deploy(
        monkeypatch, raw_app, receipt, vault, before_tags, after, after_tags
    )
    with pytest.raises(static_auth.GateError, match="drifted while awaiting"):
        static_auth.run_deploy_artifact(
            args(
                "deploy",
                apply=True,
                approve_change=True,
                reviewed=True,
                expect_fingerprint=baseline,
            )
        )


def test_owned_deploy_rejects_tag_predating_attempt(raw_app, raw_vault, monkeypatch):
    receipt, vault, before_tags, after, after_tags = deploy_fixture(raw_app, raw_vault)
    after_tags["azd-deploy-123456"]["createdTime"] = "2026-09-15T05:18:36+00:00"
    _, _, baseline = prepare_deploy(
        monkeypatch, raw_app, receipt, vault, before_tags, after, after_tags
    )
    with pytest.raises(static_auth.GateError, match="predates"):
        static_auth.run_deploy_artifact(
            args(
                "deploy",
                apply=True,
                approve_change=True,
                reviewed=True,
                expect_fingerprint=baseline,
            )
        )


def test_owned_deploy_rejects_source_change_during_package(raw_app, raw_vault, monkeypatch):
    receipt, vault, before_tags, after, after_tags = deploy_fixture(raw_app, raw_vault)
    _, commands, baseline = prepare_deploy(
        monkeypatch,
        raw_app,
        receipt,
        vault,
        before_tags,
        after,
        after_tags,
        source_hashes=["f" * 64, "f" * 64, "0" * 64],
    )
    with pytest.raises(static_auth.GateError, match="during package"):
        static_auth.run_deploy_artifact(
            args(
                "deploy",
                apply=True,
                approve_change=True,
                reviewed=True,
                expect_fingerprint=baseline,
            )
        )
    assert commands == [["azd", "package", "web-nat", "--no-prompt"]]


def test_failed_owned_command_cannot_create_success_receipt(raw_app, raw_vault, monkeypatch):
    receipt, vault, before_tags, after, after_tags = deploy_fixture(raw_app, raw_vault)
    saved, _, baseline = prepare_deploy(
        monkeypatch,
        raw_app,
        receipt,
        vault,
        before_tags,
        after,
        after_tags,
        azd_failure="package",
    )
    with pytest.raises(static_auth.GateError, match="package"):
        static_auth.run_deploy_artifact(
            args(
                "deploy",
                apply=True,
                approve_change=True,
                reviewed=True,
                expect_fingerprint=baseline,
            )
        )
    assert saved[-1]["buildAttempt"]["status"] == "started"
    assert "artifact" not in saved[-1]


@pytest.mark.parametrize("status", [None, "started", "failed"])
def test_manual_or_unattempted_provenance_is_rejected(raw_vault, status, monkeypatch):
    patch_source(monkeypatch)
    receipt = static_auth.baseline_receipt(raw_vault, SOURCE)
    receipt["artifact"] = {
        "source": SOURCE,
        "sourceFingerprint": "f" * 64,
        "image": IMAGE,
        "publishedTag": TAGGED_IMAGE,
        "publishedRevision": "fcag-dev-app--manual",
        "packageCommand": ["azd", "package", "web-nat", "--no-prompt"],
        "deployCommand": [
            "azd",
            "deploy",
            "web-nat",
            "--no-prompt",
            "--timeout",
            "1200",
        ],
    }
    if status is not None:
        receipt["buildAttempt"] = {
            **receipt["artifact"],
            "status": status,
        }
    with pytest.raises(static_auth.GateError, match="owned build"):
        static_auth.require_artifact(receipt, SOURCE, IMAGE)


def test_successful_receipt_from_another_commit_is_rejected(raw_vault, monkeypatch):
    patch_source(monkeypatch)
    receipt = successful_artifact(static_auth.baseline_receipt(raw_vault, "2" * 40))
    receipt["buildAttempt"]["source"] = "2" * 40
    receipt["artifact"]["source"] = "2" * 40
    with pytest.raises(static_auth.GateError, match="owned build"):
        static_auth.require_artifact(receipt, SOURCE, IMAGE)


def test_access_refuses_enabled_vault_without_original_receipt(raw_vault, monkeypatch):
    patch_source(monkeypatch)
    raw_vault["properties"]["enabledForTemplateDeployment"] = True
    raw_vault["properties"]["networkAcls"] = {
        "bypass": "AzureServices",
        "defaultAction": "Deny",
        "ipRules": [],
        "virtualNetworkRules": [],
    }
    monkeypatch.setattr(static_auth, "get_vault", lambda: raw_vault)
    monkeypatch.setattr(static_auth, "load_receipt", lambda required=False: None)
    with pytest.raises(static_auth.GateError, match="trusted original-state receipt"):
        static_auth.run_access(args("access"))


@pytest.mark.parametrize(
    "network_acls",
    [
        None,
        {},
        {
            "bypass": "None",
            "defaultAction": "Deny",
            "ipRules": [{"value": "192.0.2.1"}],
            "virtualNetworkRules": [{"id": "/subscriptions/fixed/subnets/private"}],
        },
    ],
)
def test_receipt_preserves_null_empty_and_non_null_acl_baselines(raw_vault, network_acls):
    raw_vault["properties"]["networkAcls"] = copy.deepcopy(network_acls)
    receipt = static_auth.baseline_receipt(raw_vault, SOURCE)
    assert receipt["vaultBaseline"]["properties"]["networkAcls"] == network_acls
    assert receipt["vaultBaselineHash"] == static_auth.fingerprint(receipt["vaultBaseline"])
    transfer = static_auth.expected_transfer_vault(receipt["vaultBaseline"])
    assert transfer["properties"]["networkAcls"]["ipRules"] == (
        (network_acls or {}).get("ipRules", [])
    )
    assert transfer["properties"]["networkAcls"]["virtualNetworkRules"] == (
        (network_acls or {}).get("virtualNetworkRules", [])
    )


def test_missing_network_acl_property_remains_missing_in_original_snapshot(raw_vault):
    raw_vault["properties"].pop("networkAcls")
    receipt = static_auth.baseline_receipt(raw_vault, SOURCE)
    assert "networkAcls" not in receipt["vaultBaseline"]["properties"]


def test_cleanup_vault_uses_exact_null_baseline(raw_app, raw_vault, monkeypatch):
    patch_source(monkeypatch)
    receipt = make_receipt(raw_vault)
    transfer = copy.deepcopy(raw_vault)
    transfer["properties"] = static_auth.expected_transfer_vault(receipt["vaultBaseline"])[
        "properties"
    ]
    monkeypatch.setattr(static_auth, "load_receipt", lambda: receipt)
    monkeypatch.setattr(static_auth, "get_app", lambda: raw_app)
    monkeypatch.setattr(static_auth, "healthz", lambda raw: True)
    monkeypatch.setattr(static_auth, "get_vault", lambda: transfer)
    monkeypatch.setattr(static_auth, "build", lambda name: {"parameters": {}})
    captured = {}

    def preview(name, template, parameters, allowed):
        captured.update(parameters)
        return {"changes": [static_auth.VAULT]}

    monkeypatch.setattr(static_auth, "preview", preview)
    static_auth.run_cleanup_vault(args("cleanup-vault"))
    assert captured["snapshot"]["value"]["properties"]["networkAcls"] is None
    assert captured["enableTransferAccess"] == {"value": False}


def test_cleanup_app_does_not_require_vault_access_or_secret_reads(raw_app, raw_vault, monkeypatch):
    patch_source(monkeypatch)
    receipt = make_receipt(raw_vault)
    monkeypatch.setattr(static_auth, "load_receipt", lambda: receipt)
    monkeypatch.setattr(static_auth, "get_app", lambda: raw_app)
    monkeypatch.setattr(static_auth, "healthz", lambda raw: True)
    monkeypatch.setattr(static_auth, "get_vault", lambda: pytest.fail("vault not required"))
    monkeypatch.setattr(
        static_auth, "secret_versions", lambda: pytest.fail("Key Vault values not required")
    )
    monkeypatch.setattr(static_auth, "build", lambda name: {"parameters": {}})
    monkeypatch.setattr(static_auth, "preview", lambda *values: {"changes": [static_auth.APP]})
    static_auth.run_cleanup_app(args("cleanup-app"))


def test_stale_cleanup_vault_fingerprint_refuses_before_apply(raw_app, raw_vault, monkeypatch):
    patch_source(monkeypatch)
    receipt = make_receipt(raw_vault)
    monkeypatch.setattr(static_auth, "load_receipt", lambda: receipt)
    monkeypatch.setattr(static_auth, "get_app", lambda: raw_app)
    monkeypatch.setattr(static_auth, "healthz", lambda raw: True)
    monkeypatch.setattr(static_auth, "get_vault", lambda: raw_vault)
    with pytest.raises(static_auth.GateError, match="fingerprint drifted"):
        static_auth.run_cleanup_vault(args("cleanup-vault", expect_fingerprint="0" * 64))


def test_exact_role_inventory_rejects_any_additional_assignment(monkeypatch):
    target = exact_assignment()
    foreign = {
        **target,
        "id": target["id"] + "-foreign",
        "roleDefinitionId": target["roleDefinitionId"] + "-foreign",
    }
    monkeypatch.setattr(static_auth, "command", lambda values: [target, foreign])
    with pytest.raises(static_auth.GateError, match="inventory is not exact"):
        static_auth.role_assignments()


def test_stale_role_fingerprint_refuses_delete(raw_app, raw_vault, monkeypatch):
    patch_source(monkeypatch)
    raw_app["properties"]["template"]["containers"][0]["env"] = [
        item
        for item in raw_app["properties"]["template"]["containers"][0]["env"]
        if item["name"] not in static_auth.ROTATION_ONLY_ENV
    ]
    receipt = make_receipt(raw_vault)
    monkeypatch.setattr(static_auth, "load_receipt", lambda: receipt)
    monkeypatch.setattr(static_auth, "get_app", lambda: raw_app)
    monkeypatch.setattr(static_auth, "get_vault", lambda: raw_vault)
    monkeypatch.setattr(static_auth, "healthz", lambda raw: True)
    monkeypatch.setattr(static_auth, "role_assignments", lambda: [exact_assignment()])
    monkeypatch.setattr(static_auth, "command", lambda values: pytest.fail("no delete"))
    with pytest.raises(static_auth.GateError, match="role fingerprint drifted"):
        static_auth.run_cleanup_role(args("cleanup-role", expect_fingerprint="0" * 64))


def test_interrupted_cleanup_continues_with_role_only(raw_app, raw_vault, monkeypatch):
    patch_source(monkeypatch)
    raw_app["properties"]["template"]["containers"][0]["env"] = [
        item
        for item in raw_app["properties"]["template"]["containers"][0]["env"]
        if item["name"] not in static_auth.ROTATION_ONLY_ENV
    ]
    receipt = make_receipt(raw_vault)
    assignment = exact_assignment()
    assignments = iter([[assignment], [assignment], []])
    deleted = []
    monkeypatch.setattr(static_auth, "load_receipt", lambda: receipt)
    monkeypatch.setattr(static_auth, "get_app", lambda: raw_app)
    monkeypatch.setattr(static_auth, "get_vault", lambda: raw_vault)
    monkeypatch.setattr(static_auth, "healthz", lambda raw: True)
    monkeypatch.setattr(static_auth, "role_assignments", lambda: next(assignments))
    monkeypatch.setattr(static_auth, "command", lambda values: deleted.append(values))
    state = static_auth.role_stage_state(receipt, raw_app, raw_vault, [assignment])
    static_auth.run_cleanup_role(
        args(
            "cleanup-role",
            apply=True,
            approve_change=True,
            reviewed=True,
            expect_fingerprint=static_auth.fingerprint(state),
        )
    )
    assert deleted[0][-2:] == ["--ids", assignment["id"]]


def test_role_delete_error_is_not_treated_as_absence(raw_app, raw_vault, monkeypatch):
    patch_source(monkeypatch)
    raw_app["properties"]["template"]["containers"][0]["env"] = [
        item
        for item in raw_app["properties"]["template"]["containers"][0]["env"]
        if item["name"] not in static_auth.ROTATION_ONLY_ENV
    ]
    receipt = make_receipt(raw_vault)
    assignment = exact_assignment()
    monkeypatch.setattr(static_auth, "load_receipt", lambda: receipt)
    monkeypatch.setattr(static_auth, "get_app", lambda: raw_app)
    monkeypatch.setattr(static_auth, "get_vault", lambda: raw_vault)
    monkeypatch.setattr(static_auth, "healthz", lambda raw: True)
    monkeypatch.setattr(static_auth, "role_assignments", lambda: [assignment])
    monkeypatch.setattr(
        static_auth,
        "command",
        lambda values: (_ for _ in ()).throw(
            static_auth.GateError("Azure/tool command failed: RoleAssignmentDoesNotExist")
        ),
    )
    state = static_auth.role_stage_state(receipt, raw_app, raw_vault, [assignment])
    with pytest.raises(static_auth.GateError, match="RoleAssignmentDoesNotExist"):
        static_auth.run_cleanup_role(
            args(
                "cleanup-role",
                apply=True,
                approve_change=True,
                reviewed=True,
                expect_fingerprint=static_auth.fingerprint(state),
            )
        )


def test_version_references_are_explicit_and_value_free():
    reference = static_auth.parameter_reference("app-session-secret-key", "a" * 32)
    assert reference == {
        "reference": {
            "keyVault": {"id": static_auth.VAULT},
            "secretName": "app-session-secret-key",
            "secretVersion": "a" * 32,
        }
    }
    assert "value" not in json.dumps(reference).lower()


def test_cleanup_parameters_have_no_key_vault_value_references(raw_app):
    snapshot = static_auth.app_snapshot(raw_app)
    parameters = static_auth.app_phase_parameters(snapshot, "cleanup-app")
    assert set(parameters) == {"snapshot", "phase", "rollbackImage"}
    assert "reference" not in json.dumps(parameters)


def test_whatif_rejects_delete_unknown_and_foreign(monkeypatch):
    template = {"parameters": {"phase": {"type": "string"}}}
    for change in (
        {"resourceId": static_auth.APP, "changeType": "Delete"},
        {"resourceId": static_auth.APP, "changeType": "Modify", "delta": {}},
        {
            "resourceId": static_auth.GROUP
            + "/providers/Microsoft.Storage/storageAccounts/foreign",
            "changeType": "Modify",
        },
    ):
        monkeypatch.setattr(
            static_auth,
            "command",
            lambda *values, change=change, **kwargs: {
                "status": "Succeeded",
                "changes": [change],
            },
        )
        with pytest.raises(static_auth.GateError):
            static_auth.preview(
                "test", template, {"phase": {"value": "transfer"}}, {static_auth.APP}
            )


def test_secret_version_drift_stops_immediately_before_transfer_apply(
    raw_app, raw_vault, monkeypatch
):
    patch_source(monkeypatch)
    receipt = make_receipt(raw_vault)
    transfer = transfer_vault(raw_vault, receipt)
    initial_versions = {"app-session-secret-key": "a", "entra-client-secret": "b"}
    versions = iter(
        [
            initial_versions,
            {"app-session-secret-key": "a", "entra-client-secret": "changed"},
        ]
    )
    monkeypatch.setattr(static_auth, "load_receipt", lambda: receipt)
    monkeypatch.setattr(static_auth, "get_vault", lambda: transfer)
    monkeypatch.setattr(static_auth, "get_app", lambda: raw_app)
    monkeypatch.setattr(static_auth, "secret_versions", lambda: next(versions))
    monkeypatch.setattr(static_auth, "build", lambda name: {"parameters": {}})
    monkeypatch.setattr(static_auth, "preview", lambda *values: {"changes": [static_auth.APP]})
    monkeypatch.setattr(static_auth, "deploy", lambda *values: pytest.fail("no apply"))
    baseline = static_auth.fingerprint(
        static_auth.transfer_stage_state(receipt, raw_app, transfer, initial_versions)
    )
    with pytest.raises(static_auth.GateError, match="Secret versions changed"):
        static_auth.run_transfer(
            args(
                "transfer",
                apply=True,
                approve_change=True,
                reviewed=True,
                expect_fingerprint=baseline,
            )
        )


def test_transfer_plan_fingerprint_changes_with_access_snapshot(raw_app, raw_vault):
    receipt = make_receipt(raw_vault)
    versions = {"app-session-secret-key": "a", "entra-client-secret": "b"}
    transfer = transfer_vault(raw_vault, receipt)
    original = static_auth.fingerprint(
        static_auth.transfer_stage_state(receipt, raw_app, transfer, versions)
    )
    transfer["properties"]["networkAcls"]["ipRules"] = [{"value": "192.0.2.20"}]
    changed = static_auth.fingerprint(
        static_auth.transfer_stage_state(receipt, raw_app, transfer, versions)
    )
    assert changed != original


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda vault: vault["properties"]["networkAcls"].update(
                {"ipRules": [{"value": "192.0.2.20"}]}
            ),
            "exact receipt-bound transfer posture",
        ),
        (
            lambda vault: vault["properties"].update({"enabledForTemplateDeployment": False}),
            "exact receipt-bound transfer posture",
        ),
        (
            lambda vault: vault["properties"].update({"publicNetworkAccess": "Enabled"}),
            "immutable vault posture",
        ),
    ],
)
def test_transfer_rejects_vault_access_drift_immediately_before_apply(
    raw_app, raw_vault, monkeypatch, mutate, message
):
    patch_source(monkeypatch)
    receipt = make_receipt(raw_vault)
    transfer = transfer_vault(raw_vault, receipt)
    drifted = copy.deepcopy(transfer)
    mutate(drifted)
    vaults = iter([transfer, drifted])
    versions = {"app-session-secret-key": "a", "entra-client-secret": "b"}
    monkeypatch.setattr(static_auth, "load_receipt", lambda: receipt)
    monkeypatch.setattr(static_auth, "get_vault", lambda: next(vaults))
    monkeypatch.setattr(static_auth, "get_app", lambda: raw_app)
    monkeypatch.setattr(static_auth, "secret_versions", lambda: versions)
    monkeypatch.setattr(static_auth, "build", lambda name: {"parameters": {}})
    monkeypatch.setattr(static_auth, "preview", lambda *values: {"changes": [static_auth.APP]})
    monkeypatch.setattr(static_auth, "deploy", lambda *values: pytest.fail("no apply"))
    baseline = static_auth.fingerprint(
        static_auth.transfer_stage_state(receipt, raw_app, transfer, versions)
    )
    with pytest.raises(static_auth.GateError, match=message):
        static_auth.run_transfer(
            args(
                "transfer",
                apply=True,
                approve_change=True,
                reviewed=True,
                expect_fingerprint=baseline,
            )
        )


def test_provider_failure_redacts_raw_response(monkeypatch):
    monkeypatch.setattr(
        static_auth.subprocess,
        "run",
        lambda *values, **kwargs: argparse.Namespace(
            returncode=1,
            stdout="PRIVATE-SENTINEL",
            stderr="ForbiddenByRbac PRIVATE-SENTINEL",
        ),
    )
    with pytest.raises(static_auth.GateError) as error:
        static_auth.command(["az", "rest"])
    assert str(error.value) == "Azure/tool command failed: ForbiddenByRbac"


def test_templates_use_secure_parent_child_and_exact_vault_restore():
    parent = (ROOT / "deployments/dev-static-auth/infra/main.bicep").read_text()
    child = (ROOT / "deployments/dev-static-auth/infra/app.bicep").read_text()
    vault = (ROOT / "deployments/dev-static-auth/infra/vault-transfer-access.bicep").read_text()
    assert parent.count("@secure()") == 3
    assert "listSecrets(resourceId('Microsoft.App/containerApps', 'fcag-dev-app')" in parent
    assert "output " not in parent and "output " not in child and "output " not in vault
    assert "name: 'fcag-dev-app'" in child
    assert "phase == 'cleanup-app' ? cleanupEnvironment : staticEnvironment" in child
    assert "phase == 'deploy-image' ? rollbackImage : web.image" in child
    assert "name: 'kvfcagdevqhg3qc4rlbt4g'" in vault
    assert "publicNetworkAccess" not in vault
    assert "properties: enableTransferAccess ?" in vault
    assert "}) : snapshot.properties" in vault
    assert "priorNetworkAcls ?? {}" in vault
