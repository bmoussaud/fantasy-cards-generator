"""Controller/infra unit tests for scripts/session_rotation/control.py.

These tests never touch live Azure. Most tests stub `azure()`/`rest()` directly. A
dedicated set of tests (see "rest(): real not_found_code parsing" below) instead
fakes only the lower-level `_run_az()` subprocess boundary with realistic az CLI
2.74.0 stdout/stderr shapes, so the real `rest()` implementation — including its
not_found_code error-body parsing — is genuinely exercised rather than bypassed.
`state_path()` is redirected into a temporary directory so the local per-run state
file and lock never collide with a real checkout's `.git` directory.
"""

from __future__ import annotations

import fcntl
import importlib.util
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTROL_PATH = ROOT / "scripts/session_rotation/control.py"


def _load_control():
    spec = importlib.util.spec_from_file_location(
        "session_rotation_control_under_test", CONTROL_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def control(tmp_path, monkeypatch):
    module = _load_control()
    monkeypatch.setattr(
        module, "state_path", lambda run_id: tmp_path / f"session-rotation-{run_id}.json"
    )
    return module


@pytest.fixture(autouse=True)
def _restore_global_logging_disable_state():
    """`control.main()` calls `logging.disable(logging.CRITICAL)` as a real-process
    entry point (safe, since a real invocation always exits afterwards). Under pytest
    that call mutates process-global logging state for the rest of the test session,
    which would silently break any later test file that asserts on log records/emission
    (e.g. tests/test_rotation_observability.py) if it runs after this file. Restore it.
    """
    yield
    logging.disable(logging.NOTSET)


@pytest.fixture()
def run_id():
    return str(uuid4())


def _whatif(changes, diagnostics=None, status="Succeeded"):
    return {"status": status, "diagnostics": diagnostics or [], "changes": changes}


def _change(resource_id, change_type="Create"):
    return {"changeType": change_type, "resourceId": resource_id}


def _expected_ids(control):
    return [
        control.JOB_ID,
        control.IDENTITY,
        control.ROLE_RESOURCE,
        control.ASSIGNMENT,
        control.ACR_ASSIGNMENT,
    ]


# ---------------------------------------------------------------------------
# preview(): exact what-if resource ID / scope checks
# ---------------------------------------------------------------------------


def test_preview_accepts_exactly_five_expected_creates(control, run_id, monkeypatch):
    calls = []

    def fake_azure(args):
        calls.append(args)
        return _whatif([_change(rid) for rid in _expected_ids(control)])

    monkeypatch.setattr(control, "azure", fake_azure)
    result = control.preview(run_id, "0" * 12)

    assert result == {"creates": 5, "modifies": 0, "deletes": 0, "diagnostics": 0}
    assert len(calls) == 1
    assert "what-if" in calls[0]
    assert calls[0][calls[0].index("--exclude-change-types") + 1] == "Ignore"
    assert "create" not in calls[0] and "delete" not in calls[0]


def test_preview_rejects_unexpected_extra_resource(control, run_id, monkeypatch):
    extra = control.RG_ID + "/providers/Microsoft.Storage/storageAccounts/unexpected"
    monkeypatch.setattr(
        control,
        "azure",
        lambda args: _whatif([_change(rid) for rid in _expected_ids(control)] + [_change(extra)]),
    )
    with pytest.raises(control.ControlError) as excinfo:
        control.preview(run_id, "0" * 12)
    assert excinfo.value.args[0] == "preview_extra_scope"


def test_preview_rejects_non_create_change_type(control, run_id, monkeypatch):
    ids = _expected_ids(control)
    changes = [_change(rid) for rid in ids[:-1]] + [_change(ids[-1], change_type="Modify")]
    monkeypatch.setattr(control, "azure", lambda args: _whatif(changes))
    with pytest.raises(control.ControlError) as excinfo:
        control.preview(run_id, "0" * 12)
    assert excinfo.value.args[0] == "preview_not_create_or_nochange"


@pytest.mark.parametrize("change_type", ["Delete", "Ignore", "Modify"])
def test_preview_rejects_every_unapproved_change_type(control, run_id, monkeypatch, change_type):
    ids = _expected_ids(control)
    changes = [_change(rid) for rid in ids]
    changes[-1] = _change(ids[-1], change_type)
    monkeypatch.setattr(control, "azure", lambda args: _whatif(changes))
    with pytest.raises(control.ControlError) as excinfo:
        control.preview(run_id, "0" * 12)
    assert excinfo.value.args[0] == "preview_not_create_or_nochange"


def test_preview_rejects_diagnostics(control, run_id, monkeypatch):
    ids = _expected_ids(control)
    monkeypatch.setattr(
        control,
        "azure",
        lambda args: _whatif([_change(rid) for rid in ids], diagnostics=[{"code": "Warn"}]),
    )
    with pytest.raises(control.ControlError) as excinfo:
        control.preview(run_id, "0" * 12)
    assert excinfo.value.args[0] == "preview_failed_or_diagnostics"


def test_preview_rejects_incomplete_resource_set(control, run_id, monkeypatch):
    ids = _expected_ids(control)[:-1]  # missing ACR_ASSIGNMENT
    monkeypatch.setattr(control, "azure", lambda args: _whatif([_change(rid) for rid in ids]))
    with pytest.raises(control.ControlError) as excinfo:
        control.preview(run_id, "0" * 12)
    assert excinfo.value.args[0] == "preview_not_exact_five"


def test_preview_rejects_unknown_nochange_resource(control, run_id, monkeypatch):
    ids = _expected_ids(control)
    changes = [_change(rid) for rid in ids] + [_change(control.RG_ID, change_type="NoChange")]
    monkeypatch.setattr(control, "azure", lambda args: _whatif(changes))
    with pytest.raises(control.ControlError) as excinfo:
        control.preview(run_id, "0" * 12)
    assert excinfo.value.args[0] == "preview_extra_scope"


def test_preview_accepts_reused_identity_as_only_nochange(control, run_id, monkeypatch):
    changes = [
        _change(rid, "NoChange" if rid == control.IDENTITY else "Create")
        for rid in _expected_ids(control)
    ]
    monkeypatch.setattr(control, "azure", lambda args: _whatif(changes))
    result = control.preview(
        run_id,
        "0" * 12,
        present={control.IDENTITY},
        identity_principal=str(uuid4()),
    )
    assert result == {"creates": 4, "modifies": 0, "deletes": 0, "diagnostics": 0}


def test_preview_accepts_all_five_nochange_after_ambiguous_success(control, run_id, monkeypatch):
    monkeypatch.setattr(
        control,
        "azure",
        lambda args: _whatif([_change(rid, "NoChange") for rid in _expected_ids(control)]),
    )
    result = control.preview(run_id, "0" * 12, present=set(_expected_ids(control)))
    assert result == {"creates": 0, "modifies": 0, "deletes": 0, "diagnostics": 0}


# ---------------------------------------------------------------------------
# main(): approval-gating boundaries
# ---------------------------------------------------------------------------


def _forbid_mutation(monkeypatch, control):
    def fail(*_args, **_kwargs):
        raise AssertionError("azure()/rest() must not be called without approval")

    monkeypatch.setattr(control, "azure", fail)


@pytest.mark.parametrize(
    "action",
    ["session-provision", "session-run", "session-observe", "session-recover", "session-cleanup"],
)
def test_mutating_actions_require_approval_and_review(control, run_id, monkeypatch, capsys, action):
    _forbid_mutation(monkeypatch, control)
    code = control.main(["--subscription", control.SUBSCRIPTION, "--run-id", run_id, action])
    out = capsys.readouterr().out
    assert code == 0
    assert "PLAN ONLY" in out


@pytest.mark.parametrize("flags", [[], ["--approve-change"], ["--reviewed"]])
def test_partial_approval_flags_still_block(control, run_id, monkeypatch, capsys, flags):
    _forbid_mutation(monkeypatch, control)
    code = control.main(
        ["--subscription", control.SUBSCRIPTION, "--run-id", run_id, "session-run", *flags]
    )
    assert code == 0
    assert "PLAN ONLY" in capsys.readouterr().out


def test_preview_action_never_requires_approval_and_only_previews(
    control, run_id, monkeypatch, capsys
):
    calls = []
    monkeypatch.setattr(control, "fresh_preview_context", lambda: (set(), ""))

    def fake_azure(args):
        calls.append(args)
        return _whatif([_change(rid) for rid in _expected_ids(control)])

    monkeypatch.setattr(control, "azure", fake_azure)
    code = control.main(
        ["--subscription", control.SUBSCRIPTION, "--run-id", run_id, "session-preview"]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"creates": 5, "modifies": 0, "deletes": 0, "diagnostics": 0}
    assert len(calls) == 1
    assert all(verb not in calls[0] for verb in ("create", "delete", "start", "stop"))


def test_main_rejects_subscription_not_in_allowlist(control, run_id):
    with pytest.raises(SystemExit):
        control.main(
            [
                "--subscription",
                "00000000-0000-0000-0000-000000000000",
                "--run-id",
                run_id,
                "session-preview",
            ]
        )


def test_main_rejects_malformed_run_id(control):
    with pytest.raises(SystemExit):
        control.main(
            ["--subscription", control.SUBSCRIPTION, "--run-id", "not-a-uuid", "session-preview"]
        )


# ---------------------------------------------------------------------------
# Concurrency: overlapping/concurrent executions must be rejected
# ---------------------------------------------------------------------------


def test_concurrent_invocation_blocks_second_controller(
    control, run_id, tmp_path, monkeypatch, capsys
):
    lock_path = tmp_path / "session-rotation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    held = open(lock_path, "w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        _forbid_mutation(monkeypatch, control)
        code = control.main(
            ["--subscription", control.SUBSCRIPTION, "--run-id", run_id, "session-preview"]
        )
        out = json.loads(capsys.readouterr().out)
        assert code == 1
        assert out["status"] == "controller_failed"
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()


# ---------------------------------------------------------------------------
# Failure paths / output redaction: never leak raw exception content
# ---------------------------------------------------------------------------


def test_unexpected_exception_is_redacted(control, run_id, monkeypatch, capsys):
    secret_value = "super-secret-key-value-should-never-print"  # pragma: allowlist secret
    calls = []

    def leaky_azure(args):
        calls.append(args)
        raise RuntimeError(f"boom, leaked value: {secret_value}")

    monkeypatch.setattr(control, "fresh_preview_context", lambda: (set(), ""))
    monkeypatch.setattr(control, "azure", leaky_azure)
    code = control.main(
        ["--subscription", control.SUBSCRIPTION, "--run-id", run_id, "session-preview"]
    )
    captured = capsys.readouterr()

    assert code == 1
    assert len(calls) == 1
    assert "what-if" in calls[0]
    assert secret_value not in captured.out + captured.err
    payload = json.loads(captured.out)
    assert payload["status"] == "controller_failed"


def test_control_error_reports_only_fixed_code(control, run_id, monkeypatch, capsys):
    monkeypatch.setattr(control, "fresh_preview_context", lambda: (set(), ""))
    monkeypatch.setattr(control, "azure", lambda args: _whatif([]))
    code = control.main(
        ["--subscription", control.SUBSCRIPTION, "--run-id", run_id, "session-preview"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["status"] == "preview_not_exact_five"


# ---------------------------------------------------------------------------
# State persistence: symlink rejection + reviewed-source binding
# ---------------------------------------------------------------------------


def test_state_load_rejects_source_hash_drift(control, run_id):
    state = {"schema": 1, "run_id": run_id, "source_hash": "0" * 64, "phase": "provisioned"}
    control.save(state)
    with pytest.raises(control.ControlError) as excinfo:
        control.load(run_id)
    assert excinfo.value.args[0] == "reviewed_source_changed"


def test_state_load_accepts_matching_source_hash(control, run_id):
    state = {
        "schema": 1,
        "run_id": run_id,
        "source_hash": control.source_hash(),
        "phase": "provisioned",
        "provision_attempted": True,
    }
    control.save(state)
    loaded = control.load(run_id)
    assert loaded == state


def test_state_load_rejects_missing_provision_attempt_marker(control, run_id):
    state = {
        "schema": 1,
        "run_id": run_id,
        "source_hash": control.source_hash(),
        "phase": "provision_intent",
    }
    control.save(state)
    with pytest.raises(control.ControlError) as excinfo:
        control.load(run_id)
    assert excinfo.value.args[0] == "state_invalid"


def test_state_load_rejects_conflicting_run_id(control, run_id):
    other_run_id = str(uuid4())
    path = control.state_path(run_id)
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "run_id": other_run_id,
                "source_hash": control.source_hash(),
                "phase": "provision_intent",
                "provision_attempted": False,
            }
        )
    )
    with pytest.raises(control.ControlError) as excinfo:
        control.load(run_id)
    assert excinfo.value.args[0] == "state_invalid"


def test_state_load_rejects_symlinked_state_file(control, run_id, tmp_path):
    real = tmp_path / "elsewhere.json"
    real.write_text(
        json.dumps(
            {
                "schema": 1,
                "run_id": run_id,
                "source_hash": control.source_hash(),
                "phase": "provisioned",
                "provision_attempted": True,
            },
        )
    )
    link_path = control.state_path(run_id)
    link_path.parent.mkdir(parents=True, exist_ok=True)
    link_path.symlink_to(real)
    with pytest.raises(control.ControlError) as excinfo:
        control.load(run_id)
    assert excinfo.value.args[0] == "state_symlink"


def test_state_save_is_private_and_not_world_readable(control, run_id):
    state = {
        "schema": 1,
        "run_id": run_id,
        "source_hash": control.source_hash(),
        "phase": "provisioned",
        "provision_attempted": True,
    }
    control.save(state)
    mode = control.state_path(run_id).stat().st_mode & 0o777
    assert mode == 0o600


def test_source_hash_covers_both_bicep_contract_files(control, tmp_path, monkeypatch):
    files = (
        "scripts/session_rotation/harness.py",
        "scripts/session_rotation/control.py",
        "scripts/session_rotation/process_inventory.py",
        "infra/session-rotation.bicep",
        "infra/modules/session-rotation-runner.bicep",
    )
    for relative in files:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative)
    monkeypatch.setattr(control, "ROOT", tmp_path)
    before = control.source_hash()
    (tmp_path / "infra/modules/session-rotation-runner.bicep").write_text("changed")
    assert control.source_hash() != before


# ---------------------------------------------------------------------------
# app_config(): exact deployed revision and image-identity validation
# ---------------------------------------------------------------------------


def _app_show(control, image=None, agent_flag="True"):
    return {
        "revision": "rev0",
        "latest": "rev0",
        "mode": "Single",
        "fqdn": "fcag-dev-app.example.azurecontainerapps.io",
        "traffic": [{"latestRevision": True, "weight": 100}],
        "provision": "Succeeded",
        "running": "Running",
        "containers": [
            {
                "name": "web",
                "image": image or control.IMAGE,
                "command": None,
                "args": None,
                "env": [],
                "settings": [
                    {"name": "SECRET_PROVIDER_BACKEND", "value": "azure"},
                    {"name": "AGENT_GENERATION_ENABLED", "value": agent_flag},
                ],
            }
        ],
        "scale": {"minReplicas": 1, "maxReplicas": 2},
    }


def _revision_show(control, image=None):
    return {
        "name": "rev0",
        "created": "2026-09-11T12:41:36Z",
        "active": True,
        "health": "Healthy",
        "provision": "Provisioned",
        "containers": [
            {
                "name": "web",
                "image": image or control.IMAGE,
                "command": None,
                "args": None,
            }
        ],
    }


def _locked_tag(control, tag="azd-deploy-1789130475", **overrides):
    item = {
        "name": tag,
        "digest": control.IMAGE_DIGEST,
        "createdTime": "2026-09-11T12:40:00Z",
        "lastUpdateTime": "2026-09-11T12:40:30Z",
        "changeableAttributes": {
            "deleteEnabled": False,
            "listEnabled": True,
            "readEnabled": True,
            "writeEnabled": False,
        },
    }
    item.update(overrides)
    return [item]


def _wire_app_config(control, monkeypatch, image, metadata=None):
    app = _app_show(control, image)
    revision = _revision_show(control, image)
    calls = []

    def fake_azure(args):
        calls.append(args)
        if args[:2] == ["containerapp", "show"]:
            return app
        if args[:3] == ["containerapp", "revision", "show"]:
            return revision
        if args[:3] == ["acr", "repository", "show-tags"]:
            return metadata
        raise AssertionError(args)

    monkeypatch.setattr(control, "azure", fake_azure)
    return calls


def test_app_config_accepts_exact_digest_and_azure_boolean_casing(control, monkeypatch):
    calls = _wire_app_config(control, monkeypatch, control.IMAGE)
    config = control.app_config()
    assert config["revision"] == "rev0"
    assert config["container"] == "web"
    assert config["image"] == {
        "reference": control.IMAGE,
        "digest": control.IMAGE_DIGEST,
        "kind": "digest",
    }
    assert not any(args[:3] == ["acr", "repository", "show-tags"] for args in calls)


def test_app_config_accepts_locked_azd_tag_backed_by_expected_digest(control, monkeypatch):
    image = control.IMAGE_REGISTRY + "/" + control.IMAGE_REPOSITORY + ":azd-deploy-1789130475"
    calls = _wire_app_config(control, monkeypatch, image, _locked_tag(control))
    config = control.app_config()
    assert config["image"]["kind"] == "locked_azd_tag"
    assert config["image"]["digest"] == control.IMAGE_DIGEST
    acr_call = next(args for args in calls if args[:3] == ["acr", "repository", "show-tags"])
    query = acr_call[acr_call.index("--query") + 1]
    assert query.endswith("changeableAttributes:changeableAttributes}")


@pytest.mark.parametrize(
    "image",
    [
        "other.azurecr.io/fantasy-cards-generator/web-nat-dev:azd-deploy-1789130475",
        "fcagdevqhg3qc4rlbt4gacr.azurecr.io/other/web:azd-deploy-1789130475",
        "fcagdevqhg3qc4rlbt4gacr.azurecr.io/fantasy-cards-generator/web-nat-dev:latest",
    ],
)
def test_app_config_rejects_wrong_registry_repository_or_tag(control, monkeypatch, image):
    calls = _wire_app_config(control, monkeypatch, image)
    with pytest.raises(control.ControlError) as excinfo:
        control.app_config()
    assert excinfo.value.args[0] == "worker_image_or_command_drift"
    assert not any(args[:3] == ["acr", "repository", "show-tags"] for args in calls)


@pytest.mark.parametrize(
    "digest",
    [
        "sha256:" + "0" * 64,
        "sha256:53c95a2d0457516d715df8e2e78d996afde9124016d2f5b6381bbf0f07f7dfeb",
    ],
)
def test_app_config_rejects_azd_tag_with_wrong_digest(control, monkeypatch, digest):
    image = control.IMAGE_REGISTRY + "/" + control.IMAGE_REPOSITORY + ":azd-deploy-1789130475"
    metadata = _locked_tag(control, digest=digest)
    _wire_app_config(control, monkeypatch, image, metadata)
    with pytest.raises(control.ControlError) as excinfo:
        control.app_config()
    assert excinfo.value.args[0] == "worker_image_or_command_drift"


@pytest.mark.parametrize("metadata", [None, {}, [], [{}, {}], [{"name": "wrong"}]])
def test_app_config_rejects_malformed_acr_tag_response(control, monkeypatch, metadata):
    image = control.IMAGE_REGISTRY + "/" + control.IMAGE_REPOSITORY + ":azd-deploy-1789130475"
    _wire_app_config(control, monkeypatch, image, metadata)
    with pytest.raises(control.ControlError) as excinfo:
        control.app_config()
    assert excinfo.value.args[0] in {
        "worker_image_metadata_invalid",
        "worker_image_or_command_drift",
    }


def test_app_config_rejects_mutable_tag_even_with_expected_digest(control, monkeypatch):
    image = control.IMAGE_REGISTRY + "/" + control.IMAGE_REPOSITORY + ":azd-deploy-1789130475"
    metadata = _locked_tag(
        control,
        changeableAttributes={
            "deleteEnabled": True,
            "listEnabled": True,
            "readEnabled": True,
            "writeEnabled": True,
        },
    )
    _wire_app_config(control, monkeypatch, image, metadata)
    with pytest.raises(control.ControlError) as excinfo:
        control.app_config()
    assert excinfo.value.args[0] == "worker_image_or_command_drift"


def test_app_config_rejects_tag_locked_after_revision_creation(control, monkeypatch):
    image = control.IMAGE_REGISTRY + "/" + control.IMAGE_REPOSITORY + ":azd-deploy-1789130475"
    metadata = _locked_tag(control, lastUpdateTime="2026-09-11T12:42:00Z")
    _wire_app_config(control, monkeypatch, image, metadata)
    with pytest.raises(control.ControlError) as excinfo:
        control.app_config()
    assert excinfo.value.args[0] == "worker_image_or_command_drift"


# ---------------------------------------------------------------------------
# validate_role / validate_assignments: drift and unknown-permission detection
# ---------------------------------------------------------------------------


def _role_properties(control):
    return {
        "type": "CustomRole",
        "roleName": f"{control.GROUP} temporary session rotation",
        "description": "Temporary session-only drill: get/set values and list version metadata.",
        "assignableScopes": [control.RG_ID],
        "permissions": [
            {
                "actions": [],
                "notActions": [],
                "dataActions": control.DATA_ACTIONS,
                "notDataActions": [],
            }
        ],
    }


def test_validate_role_accepts_expected_shape(control, monkeypatch):
    monkeypatch.setattr(
        control,
        "rest",
        lambda *a, **k: {"id": control.ROLE, "properties": _role_properties(control)},
    )
    control.validate_role()  # must not raise


@pytest.mark.parametrize("value", ["canonical", "alias", "mixed-case-alias"])
def test_custom_role_id_accepts_only_exact_canonical_or_rg_alias(control, value):
    role_id = {
        "canonical": control.ROLE,
        "alias": control.ROLE_RESOURCE,
        "mixed-case-alias": control.ROLE_RESOURCE.upper(),
    }[value]
    assert control.canonical_role_definition_id(role_id, control.ROLE) == control.ROLE


@pytest.mark.parametrize(
    "role_id",
    [
        "/subscriptions/00000000-0000-0000-0000-000000000000/providers/"
        "Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000000",
        "/subscriptions/b8ff3e15-7e2d-4fac-a773-992fb59ccedd/providers/"
        "Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000000",
        "/subscriptions/b8ff3e15-7e2d-4fac-a773-992fb59ccedd/resourceGroups/other/providers/"
        "Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000000",
        "roleDefinitions/" + "0" * 36,
    ],
)
def test_custom_role_id_rejects_foreign_or_suffix_only_values(control, role_id):
    with pytest.raises(control.ControlError) as excinfo:
        control.canonical_role_definition_id(role_id, control.ROLE)
    assert excinfo.value.args[0] == "role_definition_id_drift"


def test_custom_role_id_rejects_known_guid_at_foreign_subscription_or_group(control):
    role_guid = control.ROLE.rsplit("/", 1)[1]
    invalid = (
        f"/subscriptions/00000000-0000-0000-0000-000000000000/providers/"
        f"Microsoft.Authorization/roleDefinitions/{role_guid}",
        f"/subscriptions/{control.SUBSCRIPTION}/resourceGroups/other/providers/"
        f"Microsoft.Authorization/roleDefinitions/{role_guid}",
        f"Microsoft.Authorization/roleDefinitions/{role_guid}",
    )
    for role_id in invalid:
        with pytest.raises(control.ControlError) as excinfo:
            control.canonical_role_definition_id(role_id, control.ROLE)
        assert excinfo.value.args[0] == "role_definition_id_drift"


def test_deterministic_resource_and_assignment_guids_are_unchanged(control):
    assert control.ROLE_RESOURCE.endswith("/c75c5223-8a74-566b-8ce8-3d1ff71baa59")
    assert control.ROLE.endswith("/c75c5223-8a74-566b-8ce8-3d1ff71baa59")
    assert control.ASSIGNMENT.endswith("/4b479add-c456-545f-b0cc-5f94841aae4b")
    assert control.ACR_ASSIGNMENT.endswith("/2d923f2b-95a3-5f3d-b747-3e81a288390e")


def test_validate_role_detects_extra_data_action_drift(control, monkeypatch):
    props = _role_properties(control)
    props["permissions"][0]["dataActions"] = [
        *control.DATA_ACTIONS,
        "Microsoft.KeyVault/vaults/purge/action",
    ]
    monkeypatch.setattr(
        control,
        "rest",
        lambda *a, **k: {"id": control.ROLE, "properties": props},
    )
    with pytest.raises(control.ControlError) as excinfo:
        control.validate_role()
    assert excinfo.value.args[0] == "role_drift"


def test_validate_role_detects_assignable_scope_drift(control, monkeypatch):
    props = _role_properties(control)
    props["assignableScopes"] = [f"/subscriptions/{control.SUBSCRIPTION}"]
    monkeypatch.setattr(
        control,
        "rest",
        lambda *a, **k: {"id": control.ROLE, "properties": props},
    )
    with pytest.raises(control.ControlError) as excinfo:
        control.validate_role()
    assert excinfo.value.args[0] == "role_drift"


def test_validate_assignments_detects_identity_extra_permissions(control, monkeypatch):
    principal = str(uuid4())

    def fake_rest(method, resource_id, api, body=None):
        if resource_id == control.ROLE:
            return {"id": control.ROLE, "properties": _role_properties(control)}
        return {
            "id": resource_id,
            "properties": {
                "principalId": principal,
                "roleDefinitionId": (
                    control.ROLE if resource_id == control.ASSIGNMENT else control.ACR_ROLE
                ),
                "scope": (
                    control.SECRET_ID if resource_id == control.ASSIGNMENT else control.REGISTRY
                ),
                "principalType": "ServicePrincipal",
            },
        }

    def fake_azure(args):
        # role assignment list --all reveals an extra, unaccounted-for assignment.
        return [
            control.ASSIGNMENT,
            control.ACR_ASSIGNMENT,
            control.RG_ID + "/providers/x/roleAssignments/extra",
        ]

    monkeypatch.setattr(control, "rest", fake_rest)
    monkeypatch.setattr(control, "azure", fake_azure)
    with pytest.raises(control.ControlError) as excinfo:
        control.validate_assignments(principal)
    assert excinfo.value.args[0] == "identity_extra_permissions"


def test_validate_assignments_accepts_exact_two(control, monkeypatch):
    principal = str(uuid4())

    def fake_rest(method, resource_id, api, body=None):
        if resource_id == control.ROLE:
            return {"id": control.ROLE, "properties": _role_properties(control)}
        return {
            "id": resource_id,
            "properties": {
                "principalId": principal,
                "roleDefinitionId": (
                    control.ROLE if resource_id == control.ASSIGNMENT else control.ACR_ROLE
                ),
                "scope": (
                    control.SECRET_ID if resource_id == control.ASSIGNMENT else control.REGISTRY
                ),
                "principalType": "ServicePrincipal",
            },
        }

    monkeypatch.setattr(control, "rest", fake_rest)
    monkeypatch.setattr(control, "azure", lambda args: [control.ASSIGNMENT, control.ACR_ASSIGNMENT])
    control.validate_assignments(principal)  # must not raise


def test_identity_assignment_ids_rejects_ambiguous_duplicates(control, monkeypatch):
    monkeypatch.setattr(
        control,
        "azure",
        lambda args: [control.ASSIGNMENT, control.ASSIGNMENT.upper()],
    )

    with pytest.raises(control.ControlError) as excinfo:
        control.identity_assignment_ids(str(uuid4()))

    assert excinfo.value.args[0] == "identity_permissions_invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("principalId", "00000000-0000-0000-0000-000000000000"),
        ("scope", "/subscriptions/b8ff3e15-7e2d-4fac-a773-992fb59ccedd"),
        (
            "roleDefinitionId",
            "/subscriptions/00000000-0000-0000-0000-000000000000/providers/"
            "Microsoft.Authorization/roleDefinitions/7f951dda-4ed3-4680-a7ca-43fe172d538d",
        ),
    ],
)
def test_assignment_validation_rejects_principal_scope_or_role_drift(control, field, value):
    principal = str(uuid4())
    item = {
        "id": control.ACR_ASSIGNMENT,
        "properties": {
            "principalId": principal,
            "roleDefinitionId": control.ACR_ROLE,
            "scope": control.REGISTRY,
            "principalType": "ServicePrincipal",
        },
    }
    item["properties"][field] = value
    with pytest.raises(control.ControlError):
        control.validate_assignment_item(
            item,
            control.ACR_ASSIGNMENT,
            principal,
            control.ACR_ROLE,
            control.REGISTRY,
        )


# ---------------------------------------------------------------------------
# provision(): resumable intent and exact resource reconciliation
# ---------------------------------------------------------------------------


def _snapshot(control, present, job_provisioning_state="Succeeded"):
    identity = None
    if control.IDENTITY in present:
        identity = {
            "id": control.IDENTITY,
            "clientId": str(uuid4()),
            "principalId": str(uuid4()),
        }
    return {
        "present": set(present),
        "identity": identity,
        "job": {} if control.JOB_ID in present else None,
        "job_provisioning_state": (job_provisioning_state if control.JOB_ID in present else None),
    }


@pytest.mark.parametrize(
    "present",
    [
        set(),
        {"identity"},
        {"identity", "role"},
        {"identity", "role", "secret-assignment"},
        {"identity", "role", "secret-assignment", "acr-assignment"},
    ],
)
def test_provision_resume_creates_only_missing_expected_resources(
    control, run_id, monkeypatch, present
):
    mapping = {
        "identity": control.IDENTITY,
        "role": control.ROLE_RESOURCE,
        "secret-assignment": control.ASSIGNMENT,
        "acr-assignment": control.ACR_ASSIGNMENT,
    }
    existing = {mapping[name] for name in present}
    state = _prepared_state(control, run_id)
    state["phase"] = "provision_intent"
    state["provision_attempted"] = True
    initial = _snapshot(control, existing)
    final = _snapshot(control, set(_expected_ids(control)))
    snapshots = iter((initial, final))
    plans = []
    deployments = []
    monkeypatch.setattr(control, "validate_current_baseline", lambda state: ({}, {}))
    monkeypatch.setattr(control, "resource_snapshot", lambda state, **kwargs: next(snapshots))

    def fake_preview(run, expected, resources, principal):
        plans.append((set(resources), principal))
        return {"creates": 5 - len(resources), "modifies": 0, "deletes": 0, "diagnostics": 0}

    monkeypatch.setattr(control, "preview", fake_preview)
    monkeypatch.setattr(control, "azure", lambda args: deployments.append(args) or "Succeeded")
    monkeypatch.setattr(control, "save", lambda state: None)
    control.provision(state, resume=True)
    assert plans[0][0] == existing
    assert len(deployments) == 1
    assert state["phase"] == "provisioned"


def test_provision_all_resources_exist_reconciles_without_redeploy(control, run_id, monkeypatch):
    state = _prepared_state(control, run_id)
    state["phase"] = "provision_intent"
    state["provision_attempted"] = True
    complete = _snapshot(control, set(_expected_ids(control)))
    monkeypatch.setattr(control, "validate_current_baseline", lambda state: ({}, {}))
    monkeypatch.setattr(control, "resource_snapshot", lambda state, **kwargs: complete)
    monkeypatch.setattr(
        control,
        "preview",
        lambda *args: {"creates": 0, "modifies": 0, "deletes": 0, "diagnostics": 0},
    )
    monkeypatch.setattr(
        control, "azure", lambda args: pytest.fail("deployment must not be repeated")
    )
    monkeypatch.setattr(control, "save", lambda state: None)
    control.provision(state, resume=True)
    assert state["phase"] == "provisioned"


def test_provision_failed_deployment_keeps_intent_phase(control, run_id, monkeypatch):
    state = _prepared_state(control, run_id)
    state["phase"] = "provision_intent"
    state["provision_attempted"] = True
    monkeypatch.setattr(control, "validate_current_baseline", lambda state: ({}, {}))
    monkeypatch.setattr(
        control, "resource_snapshot", lambda state, **kwargs: _snapshot(control, set())
    )
    monkeypatch.setattr(
        control,
        "preview",
        lambda *args: {"creates": 5, "modifies": 0, "deletes": 0, "diagnostics": 0},
    )
    monkeypatch.setattr(control, "azure", lambda args: "Failed")
    with pytest.raises(control.ControlError) as excinfo:
        control.provision(state, resume=True)
    assert excinfo.value.args[0] == "provision_unconfirmed"
    assert state["phase"] == "provision_intent"


def test_provision_timeout_after_write_can_reconcile_same_run(control, run_id, monkeypatch):
    state = _prepared_state(control, run_id)
    state["phase"] = "provision_intent"
    state["provision_attempted"] = True
    empty = _snapshot(control, set())
    complete = _snapshot(control, set(_expected_ids(control)))
    monkeypatch.setattr(control, "validate_current_baseline", lambda state: ({}, {}))
    monkeypatch.setattr(
        control,
        "preview",
        lambda *args: {
            "creates": 0 if args[2] else 5,
            "modifies": 0,
            "deletes": 0,
            "diagnostics": 0,
        },
    )
    monkeypatch.setattr(control, "save", lambda state: None)
    monkeypatch.setattr(control, "resource_snapshot", lambda state, **kwargs: empty)
    monkeypatch.setattr(
        control,
        "azure",
        lambda args: (_ for _ in ()).throw(control.ControlError("azure_transport_failed")),
    )
    with pytest.raises(control.ControlError):
        control.provision(state, resume=True)
    assert state["phase"] == "provision_intent"

    monkeypatch.setattr(control, "resource_snapshot", lambda state, **kwargs: complete)
    monkeypatch.setattr(
        control, "azure", lambda args: pytest.fail("reconciled success must not redeploy")
    )
    control.provision(state, resume=True)
    assert state["phase"] == "provisioned"


def test_fresh_provision_reuses_only_idle_identity_with_zero_grants(control, run_id, monkeypatch):
    state = _prepared_state(control, run_id)
    state["phase"] = "provision_intent"
    state["provision_attempted"] = False
    identity_only = _snapshot(control, {control.IDENTITY})
    complete = _snapshot(control, set(_expected_ids(control)))
    snapshots = iter((identity_only, complete))
    captured = {}
    monkeypatch.setattr(control, "validate_current_baseline", lambda state: ({}, {}))
    monkeypatch.setattr(control, "resource_snapshot", lambda state, **kwargs: next(snapshots))
    monkeypatch.setattr(control, "identity_assignment_ids", lambda principal: set())

    def fake_preview(run, expected, present, principal):
        captured["present"] = present
        captured["principal"] = principal
        return {"creates": 4, "modifies": 0, "deletes": 0, "diagnostics": 0}

    monkeypatch.setattr(control, "preview", fake_preview)
    monkeypatch.setattr(control, "azure", lambda args: "Succeeded")
    monkeypatch.setattr(control, "save", lambda state: None)
    control.provision(state, resume=False)
    assert captured["present"] == {control.IDENTITY}
    assert captured["principal"] == identity_only["identity"]["principalId"]


def test_fresh_provision_rejects_retained_role_or_job(control, run_id, monkeypatch):
    state = _prepared_state(control, run_id)
    state["phase"] = "provision_intent"
    state["provision_attempted"] = False
    snapshot = _snapshot(control, {control.IDENTITY, control.ROLE_RESOURCE})
    monkeypatch.setattr(control, "validate_current_baseline", lambda state: ({}, {}))
    monkeypatch.setattr(control, "resource_snapshot", lambda state, **kwargs: snapshot)
    monkeypatch.setattr(control, "identity_assignment_ids", lambda principal: set())
    with pytest.raises(control.ControlError) as excinfo:
        control.provision(state, resume=False)
    assert excinfo.value.args[0] == "fresh_run_resources_exist"


def test_provision_rejects_stopped_or_active_preexisting_execution(control, run_id, monkeypatch):
    state = _prepared_state(control, run_id)
    identity = {
        "id": control.IDENTITY,
        "name": control.JOB + "-id",
        "type": "Microsoft.ManagedIdentity/userAssignedIdentities",
        "location": "eastus2",
        "tags": None,
        "properties": {"clientId": str(uuid4()), "principalId": str(uuid4())},
    }
    resources = {
        control.IDENTITY: identity,
        control.ROLE: {"id": control.ROLE, "properties": _role_properties(control)},
        control.ASSIGNMENT: None,
        control.ACR_ASSIGNMENT: None,
        control.JOB_ID: _job_resource(
            control,
            state,
            {"id": control.IDENTITY, **identity["properties"]},
        ),
    }
    monkeypatch.setattr(
        control,
        "_probe",
        lambda resource_id, api, code: resources.get(resource_id),
    )
    monkeypatch.setattr(control, "identity_assignment_ids", lambda principal: set())
    monkeypatch.setattr(
        control,
        "executions",
        lambda: [{"name": "old-execution", "status": "Stopped"}],
    )
    with pytest.raises(control.ControlError) as excinfo:
        control.resource_snapshot(state, require_no_executions=True)
    assert excinfo.value.args[0] == "preexisting_execution"


def test_job_validation_rejects_conflicting_run_id(control, run_id):
    state = _prepared_state(control, run_id)
    identity = {"id": control.IDENTITY, "clientId": str(uuid4()), "principalId": str(uuid4())}
    item = _job_resource(control, state, identity)
    item["properties"]["template"]["containers"][0]["env"][1]["value"] = str(uuid4())
    with pytest.raises(control.ControlError) as excinfo:
        control.validate_job_item(state, item, identity)
    assert excinfo.value.args[0] == "job_template_drift"


def test_job_validation_rejects_failed_server_provisioning(control, run_id):
    state = _prepared_state(control, run_id)
    identity = {"id": control.IDENTITY, "clientId": str(uuid4()), "principalId": str(uuid4())}
    item = _job_resource(control, state, identity)
    item["properties"]["provisioningState"] = "Failed"
    with pytest.raises(control.ControlError) as excinfo:
        control.validate_job_item(state, item, identity)
    assert excinfo.value.args[0] == "job_configuration_drift"


@pytest.mark.parametrize("value", ["eastus2", "EASTUS2", "East US 2", "east us 2", " eastus2 "])
def test_same_location_accepts_compact_and_display_forms(control, value):
    assert control.same_location(value, control.LOCATION) is True


@pytest.mark.parametrize(
    "value",
    [
        "eastus",  # different (shorter) region
        "westus2",  # different region entirely
        "East US",  # display form of a different region
        "east-us-2",  # hyphenated, not a real ARM form
        "eastus2x",  # trailing garbage
        "",  # missing/empty
        None,  # non-string
        123,  # non-string
    ],
)
def test_same_location_rejects_other_regions_and_malformed_values(control, value):
    assert control.same_location(value, control.LOCATION) is False


@pytest.mark.parametrize("location", ["eastus2", "East US 2"])
def test_job_validation_accepts_compact_and_display_location(control, run_id, location):
    state = _prepared_state(control, run_id)
    identity = {"id": control.IDENTITY, "clientId": str(uuid4()), "principalId": str(uuid4())}
    item = _job_resource(control, state, identity)
    item["location"] = location
    # Should not raise: both real ARM location representations are accepted.
    control.validate_job_item(state, item, identity)


def test_job_validation_rejects_other_region(control, run_id):
    state = _prepared_state(control, run_id)
    identity = {"id": control.IDENTITY, "clientId": str(uuid4()), "principalId": str(uuid4())}
    item = _job_resource(control, state, identity)
    item["location"] = "westus2"
    with pytest.raises(control.ControlError) as excinfo:
        control.validate_job_item(state, item, identity)
    assert excinfo.value.args[0] == "job_identity_drift"


def test_job_validation_rejects_malformed_location(control, run_id):
    state = _prepared_state(control, run_id)
    identity = {"id": control.IDENTITY, "clientId": str(uuid4()), "principalId": str(uuid4())}
    item = _job_resource(control, state, identity)
    item["location"] = "east-us-2"
    with pytest.raises(control.ControlError) as excinfo:
        control.validate_job_item(state, item, identity)
    assert excinfo.value.args[0] == "job_identity_drift"


@pytest.mark.parametrize("location", ["eastus2", "East US 2"])
def test_identity_validation_accepts_compact_and_display_location(control, location):
    item = {
        "id": control.IDENTITY,
        "name": control.JOB + "-id",
        "type": "Microsoft.ManagedIdentity/userAssignedIdentities",
        "location": location,
        "tags": None,
        "properties": {"clientId": str(uuid4()), "principalId": str(uuid4())},
    }
    # Should not raise: both real ARM location representations are accepted.
    control.validate_identity_item(item)


def test_identity_validation_rejects_other_region(control):
    item = {
        "id": control.IDENTITY,
        "name": control.JOB + "-id",
        "type": "Microsoft.ManagedIdentity/userAssignedIdentities",
        "location": "West US 2",
        "tags": None,
        "properties": {"clientId": str(uuid4()), "principalId": str(uuid4())},
    }
    with pytest.raises(control.ControlError) as excinfo:
        control.validate_identity_item(item)
    assert excinfo.value.args[0] == "identity_drift"


@pytest.mark.parametrize("provisioning_state", ["InProgress", "Updating", "Unknown"])
def test_failed_job_snapshot_path_rejects_nonterminal_server_states(
    control, run_id, monkeypatch, provisioning_state
):
    state = _prepared_state(control, run_id)
    identity = {
        "id": control.IDENTITY,
        "name": control.JOB + "-id",
        "type": "Microsoft.ManagedIdentity/userAssignedIdentities",
        "location": "eastus2",
        "tags": None,
        "properties": {"clientId": str(uuid4()), "principalId": str(uuid4())},
    }
    job = _job_resource(
        control,
        state,
        {"id": control.IDENTITY, **identity["properties"]},
    )
    job["properties"]["provisioningState"] = provisioning_state
    resources = {
        control.IDENTITY: identity,
        control.ROLE: None,
        control.ASSIGNMENT: None,
        control.ACR_ASSIGNMENT: None,
        control.JOB_ID: job,
    }
    monkeypatch.setattr(
        control,
        "_probe",
        lambda resource_id, api, code: resources.get(resource_id),
    )
    monkeypatch.setattr(control, "identity_assignment_ids", lambda principal: set())

    with pytest.raises(control.ControlError) as excinfo:
        control.resource_snapshot(state, allow_failed_job=True)

    assert excinfo.value.args[0] == "job_configuration_drift"


def test_failed_job_snapshot_path_rejects_foreign_run(control, run_id, monkeypatch):
    state = _prepared_state(control, run_id)
    identity = {
        "id": control.IDENTITY,
        "name": control.JOB + "-id",
        "type": "Microsoft.ManagedIdentity/userAssignedIdentities",
        "location": "eastus2",
        "tags": None,
        "properties": {"clientId": str(uuid4()), "principalId": str(uuid4())},
    }
    job = _job_resource(
        control,
        state,
        {"id": control.IDENTITY, **identity["properties"]},
    )
    job["properties"]["provisioningState"] = "Failed"
    job["properties"]["template"]["containers"][0]["env"][1]["value"] = str(uuid4())
    resources = {
        control.IDENTITY: identity,
        control.ROLE: None,
        control.ASSIGNMENT: None,
        control.ACR_ASSIGNMENT: None,
        control.JOB_ID: job,
    }
    monkeypatch.setattr(
        control,
        "_probe",
        lambda resource_id, api, code: resources.get(resource_id),
    )
    monkeypatch.setattr(control, "identity_assignment_ids", lambda principal: set())

    with pytest.raises(control.ControlError) as excinfo:
        control.resource_snapshot(state, allow_failed_job=True)

    assert excinfo.value.args[0] == "job_template_drift"


def test_provision_retries_contract_valid_failed_job_and_requires_server_success(
    control, run_id, monkeypatch
):
    state = _prepared_state(control, run_id)
    state["phase"] = "provision_intent"
    state["provision_attempted"] = True
    failed = _snapshot(control, set(_expected_ids(control)), "Failed")
    succeeded = _snapshot(control, set(_expected_ids(control)))
    snapshots = iter((failed, succeeded))
    deployments = []
    monkeypatch.setattr(control, "validate_current_baseline", lambda state: ({}, {}))
    monkeypatch.setattr(control, "resource_snapshot", lambda state, **kwargs: next(snapshots))
    monkeypatch.setattr(
        control,
        "preview",
        lambda *args: {"creates": 0, "modifies": 0, "deletes": 0, "diagnostics": 0},
    )
    monkeypatch.setattr(control, "azure", lambda args: deployments.append(args) or "Succeeded")
    monkeypatch.setattr(control, "save", lambda state: None)

    control.provision(state, resume=True)

    assert len(deployments) == 1
    assert "create" in deployments[0]
    assert state["phase"] == "provisioned"


def test_provision_rejects_failed_job_still_failed_after_successful_deployment(
    control, run_id, monkeypatch
):
    state = _prepared_state(control, run_id)
    state["phase"] = "provision_intent"
    state["provision_attempted"] = True
    failed = _snapshot(control, set(_expected_ids(control)), "Failed")
    monkeypatch.setattr(control, "validate_current_baseline", lambda state: ({}, {}))

    def failed_snapshot(state, allow_failed_job=False, **kwargs):
        if not allow_failed_job:
            raise control.ControlError("job_configuration_drift")
        return failed

    monkeypatch.setattr(control, "resource_snapshot", failed_snapshot)
    monkeypatch.setattr(
        control,
        "preview",
        lambda *args: {"creates": 0, "modifies": 0, "deletes": 0, "diagnostics": 0},
    )
    monkeypatch.setattr(control, "azure", lambda args: "Succeeded")

    with pytest.raises(control.ControlError) as excinfo:
        control.provision(state, resume=True)

    assert excinfo.value.args[0] == "job_configuration_drift"
    assert state["phase"] == "provision_intent"


def test_main_saves_intent_before_provision_and_reuses_same_state(
    control, run_id, monkeypatch, capsys
):
    config = _prepared_state(control, run_id)["config"]
    evidence = _prepared_state(control, run_id)["baseline"]
    monkeypatch.setattr(control, "app_config", lambda: config)
    monkeypatch.setattr(control, "inventory", lambda current: {})
    monkeypatch.setattr(control, "observations", lambda revision: [])
    monkeypatch.setattr(control, "baseline", lambda *args: evidence)
    calls = []

    def fake_provision(state, resume):
        assert control.state_path(run_id).exists()
        assert control.load(run_id)["phase"] == "provision_intent"
        assert control.load(run_id)["provision_attempted"] is False
        calls.append(resume)

    monkeypatch.setattr(control, "provision", fake_provision)
    args = [
        "--subscription",
        control.SUBSCRIPTION,
        "--run-id",
        run_id,
        "session-provision",
        "--approve-change",
        "--reviewed",
    ]
    assert control.main(args) == 0
    capsys.readouterr()
    assert control.main(args) == 0
    assert calls == [False, True]


def test_main_unattempted_intent_rejects_stale_resources_on_every_retry(
    control, run_id, monkeypatch, capsys
):
    prepared = _prepared_state(control, run_id)
    monkeypatch.setattr(control, "app_config", lambda: prepared["config"])
    monkeypatch.setattr(control, "inventory", lambda current: {})
    monkeypatch.setattr(control, "observations", lambda revision: [])
    monkeypatch.setattr(control, "baseline", lambda *args: prepared["baseline"])
    monkeypatch.setattr(control, "validate_current_baseline", lambda state: ({}, {}))
    stale = _snapshot(control, {control.IDENTITY, control.ROLE_RESOURCE})
    monkeypatch.setattr(control, "resource_snapshot", lambda state, **kwargs: stale)
    monkeypatch.setattr(control, "identity_assignment_ids", lambda principal: set())
    monkeypatch.setattr(
        control,
        "azure",
        lambda args: pytest.fail("stale resources must block before Azure deployment"),
    )
    args = [
        "--subscription",
        control.SUBSCRIPTION,
        "--run-id",
        run_id,
        "session-provision",
        "--approve-change",
        "--reviewed",
    ]

    assert control.main(args) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "fresh_run_resources_exist"
    assert control.load(run_id)["provision_attempted"] is False
    assert control.main(args) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "fresh_run_resources_exist"
    assert control.load(run_id)["provision_attempted"] is False


def test_invalid_preview_cannot_turn_foreign_resources_into_owned_resume(
    control, run_id, monkeypatch, capsys
):
    prepared = _prepared_state(control, run_id)
    monkeypatch.setattr(control, "app_config", lambda: prepared["config"])
    monkeypatch.setattr(control, "inventory", lambda current: {})
    monkeypatch.setattr(control, "observations", lambda revision: [])
    monkeypatch.setattr(control, "baseline", lambda *args: prepared["baseline"])
    monkeypatch.setattr(control, "validate_current_baseline", lambda state: ({}, {}))
    current = {"snapshot": _snapshot(control, set())}
    monkeypatch.setattr(control, "resource_snapshot", lambda state, **kwargs: current["snapshot"])
    monkeypatch.setattr(
        control,
        "preview",
        lambda *args: (_ for _ in ()).throw(control.ControlError("preview_not_exact_five")),
    )
    monkeypatch.setattr(
        control,
        "azure",
        lambda args: pytest.fail("invalid preview must never reach Azure deployment"),
    )
    args = [
        "--subscription",
        control.SUBSCRIPTION,
        "--run-id",
        run_id,
        "session-provision",
        "--approve-change",
        "--reviewed",
    ]

    assert control.main(args) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "preview_not_exact_five"
    assert control.load(run_id)["provision_attempted"] is False

    current["snapshot"] = _snapshot(control, {control.IDENTITY, control.ROLE_RESOURCE})
    assert control.main(args) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "fresh_run_resources_exist"
    assert control.load(run_id)["provision_attempted"] is False


def test_partial_deployment_failure_persists_attempt_and_resumes_owned_resources(
    control, run_id, monkeypatch, capsys
):
    prepared = _prepared_state(control, run_id)
    monkeypatch.setattr(control, "app_config", lambda: prepared["config"])
    monkeypatch.setattr(control, "inventory", lambda current: {})
    monkeypatch.setattr(control, "observations", lambda revision: [])
    monkeypatch.setattr(control, "baseline", lambda *args: prepared["baseline"])
    monkeypatch.setattr(control, "validate_current_baseline", lambda state: ({}, {}))
    current = {"snapshot": _snapshot(control, set())}
    monkeypatch.setattr(control, "resource_snapshot", lambda state, **kwargs: current["snapshot"])
    monkeypatch.setattr(
        control,
        "preview",
        lambda *args: {
            "creates": 0 if args[2] else 5,
            "modifies": 0,
            "deletes": 0,
            "diagnostics": 0,
        },
    )
    deployment_calls = []

    def fail_after_attempt(args):
        deployment_calls.append(args)
        raise control.ControlError("azure_transport_failed")

    monkeypatch.setattr(control, "azure", fail_after_attempt)
    args = [
        "--subscription",
        control.SUBSCRIPTION,
        "--run-id",
        run_id,
        "session-provision",
        "--approve-change",
        "--reviewed",
    ]

    assert control.main(args) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "azure_transport_failed"
    assert len(deployment_calls) == 1
    assert control.load(run_id)["provision_attempted"] is True

    current["snapshot"] = _snapshot(control, set(_expected_ids(control)))
    monkeypatch.setattr(
        control,
        "azure",
        lambda args: pytest.fail("complete owned resources must reconcile without redeploy"),
    )
    assert control.main(args) == 0
    assert json.loads(capsys.readouterr().out)["phase"] == "provisioned"


def test_main_source_hash_drift_blocks_before_any_azure_call(control, run_id, monkeypatch, capsys):
    state = _prepared_state(control, run_id)
    state["phase"] = "provision_intent"
    state["source_hash"] = "0" * 64
    control.save(state)
    monkeypatch.setattr(
        control,
        "azure",
        lambda args: pytest.fail("source drift must block before Azure calls"),
    )
    monkeypatch.setattr(
        control,
        "rest",
        lambda *args, **kwargs: pytest.fail("source drift must block before Azure calls"),
    )

    code = control.main(
        [
            "--subscription",
            control.SUBSCRIPTION,
            "--run-id",
            run_id,
            "session-provision",
            "--approve-change",
            "--reviewed",
        ]
    )

    assert code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "reviewed_source_changed"


def test_main_cleanup_reports_permission_outcome_without_changing_running_phase(
    control, run_id, monkeypatch, capsys
):
    state = _prepared_state(control, run_id)
    state["phase"] = "running"
    control.save(state)

    def fake_cleanup(current):
        current["permission_phase"] = "rights_revoked"

    monkeypatch.setattr(control, "cleanup", fake_cleanup)

    code = control.main(
        [
            "--subscription",
            control.SUBSCRIPTION,
            "--run-id",
            run_id,
            "session-cleanup",
            "--approve-change",
            "--reviewed",
        ]
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out) == {
        "schema": "session-controller/v1",
        "phase": "running",
        "permission_phase": "rights_revoked",
    }


# ---------------------------------------------------------------------------
# cleanup(): least-privilege teardown ordering and drift rejection
# ---------------------------------------------------------------------------


def _job_resource(control, state, identity):
    harness_text = (control.ROOT / "scripts/session_rotation/harness.py").read_text()
    return {
        "id": control.JOB_ID,
        "name": control.JOB,
        "type": "Microsoft.App/jobs",
        "location": "eastus2",
        "tags": None,
        "identity": {"type": "UserAssigned", "userAssignedIdentities": {identity["id"]: {}}},
        "properties": {
            "provisioningState": "Succeeded",
            "environmentId": control.PREFIX + "Microsoft.App/managedEnvironments/fcag-dev-cae",
            "workloadProfileName": "Consumption",
            "configuration": {
                "triggerType": "Manual",
                "replicaTimeout": 4500,
                "replicaRetryLimit": 0,
                "manualTriggerConfig": {"parallelism": 1, "replicaCompletionCount": 1},
                "registries": [{"server": control.IMAGE.split("/")[0], "identity": identity["id"]}],
            },
            "template": {
                "containers": [
                    {
                        "name": "session-drill",
                        "image": control.IMAGE,
                        "command": ["/app/.venv/bin/python", "-I", "-c", harness_text],
                        "env": [
                            {"name": "RUNNER_CLIENT_ID", "value": identity["clientId"]},
                            {"name": "SESSION_RUN_ID", "value": state["run_id"]},
                            {
                                "name": "SESSION_EXPECTED_HASH",
                                "value": state["baseline"]["session_hash"],
                            },
                            {"name": "SESSION_APP_HOST", "value": state["config"]["host"]},
                        ],
                        "resources": {"cpu": 0.25, "memory": "0.5Gi"},
                    }
                ],
            },
        },
    }


def _prepared_state(control, run_id):
    return {
        "schema": 1,
        "run_id": run_id,
        "source_hash": control.source_hash(),
        "phase": "provisioned",
        "provision_attempted": True,
        "config": {
            "fingerprint": "f" * 64,
            "revision": "rev0",
            "container": "app",
            "host": "fcag-dev-app.example.azurecontainerapps.io",
        },
        "members": {},
        "baseline": {"session_hash": "1" * 12, "entra_hash": "2" * 12},
    }


def _wire_validate_job(
    control,
    monkeypatch,
    state,
    principal,
    identity,
    executions=None,
    already_deleted=(),
    job_provisioning_state="Succeeded",
):
    """Wire fake azure()/rest() for cleanup()-focused tests.

    `already_deleted` simulates resources a prior, interrupted cleanup attempt already
    removed: a probing rest("get", rid, ..., not_found_code=...) call for one of these
    IDs returns None (the real "known not-found" outcome), exactly like a genuine ARM
    404 with the matching documented error code would after passing through rest()'s
    own not-found handling.
    """
    deleted = set(already_deleted)
    monkeypatch.setattr(control, "validate_current_baseline", lambda state: ({}, {}))
    monkeypatch.setattr(control, "app_config", lambda: state["config"])

    def fake_azure(args):
        if args[:3] == ["containerapp", "job", "execution"]:
            return list(executions or [])
        if args[:3] == ["role", "assignment", "list"]:
            if "--assignee-object-id" in args:
                return [
                    rid
                    for rid in (control.ASSIGNMENT, control.ACR_ASSIGNMENT)
                    if rid not in deleted
                ]
            return []
        raise AssertionError(f"unexpected azure() call: {args}")

    def fake_rest(method, resource_id, api, body=None, not_found_code=None):
        if method == "delete":
            deleted.add(resource_id)
            return None
        if not_found_code is not None and resource_id in deleted:
            return None
        if resource_id == control.IDENTITY:
            return {
                "id": identity["id"],
                "name": control.JOB + "-id",
                "type": "Microsoft.ManagedIdentity/userAssignedIdentities",
                "location": "eastus2",
                "tags": None,
                "properties": {
                    "clientId": identity["clientId"],
                    "principalId": principal,
                },
            }
        if resource_id == control.JOB_ID:
            item = _job_resource(control, state, identity)
            item["properties"]["provisioningState"] = job_provisioning_state
            return item
        if resource_id == control.ROLE:
            return {"id": control.ROLE, "properties": _role_properties(control)}
        if resource_id in (control.ASSIGNMENT, control.ACR_ASSIGNMENT):
            return {
                "id": resource_id,
                "properties": {
                    "principalId": principal,
                    "roleDefinitionId": (
                        control.ROLE if resource_id == control.ASSIGNMENT else control.ACR_ROLE
                    ),
                    "scope": (
                        control.SECRET_ID if resource_id == control.ASSIGNMENT else control.REGISTRY
                    ),
                    "principalType": "ServicePrincipal",
                },
            }
        raise AssertionError(f"unexpected rest() call: {method} {resource_id}")

    monkeypatch.setattr(control, "azure", fake_azure)
    monkeypatch.setattr(control, "rest", fake_rest)


def test_cleanup_happy_path_revokes_rights_in_order(control, run_id, monkeypatch):
    identity = {"clientId": str(uuid4()), "principalId": str(uuid4()), "id": control.IDENTITY}
    state = _prepared_state(control, run_id)
    deleted = []
    _wire_validate_job(control, monkeypatch, state, identity["principalId"], identity)
    original_rest = control.rest

    def recording_rest(method, resource_id, api, body=None, not_found_code=None):
        if method == "delete":
            deleted.append(resource_id)
        return original_rest(method, resource_id, api, body, not_found_code=not_found_code)

    monkeypatch.setattr(control, "rest", recording_rest)
    control.cleanup(state)

    assert state["phase"] == "rights_revoked"
    # Both scoped role assignments are revoked before the shared role definition.
    assert deleted == [control.ASSIGNMENT, control.ACR_ASSIGNMENT, control.ROLE]


def test_cleanup_partial_intent_without_job_marks_aborted_then_revokes(
    control, run_id, monkeypatch
):
    identity = {"clientId": str(uuid4()), "principalId": str(uuid4()), "id": control.IDENTITY}
    state = _prepared_state(control, run_id)
    state["phase"] = "provision_intent"
    phases = []
    deleted = []
    _wire_validate_job(
        control,
        monkeypatch,
        state,
        identity["principalId"],
        identity,
        already_deleted={control.JOB_ID, control.ASSIGNMENT},
    )
    monkeypatch.setattr(control, "save", lambda current: phases.append(current["phase"]))
    original_rest = control.rest

    def recording_rest(method, resource_id, api, body=None, not_found_code=None):
        if method == "delete":
            deleted.append(resource_id)
        return original_rest(method, resource_id, api, body, not_found_code=not_found_code)

    monkeypatch.setattr(control, "rest", recording_rest)
    control.cleanup(state)
    assert phases == ["aborted", "rights_revoked"]
    assert deleted == [control.ACR_ASSIGNMENT, control.ROLE]


def test_cleanup_blocked_while_execution_is_active(control, run_id, monkeypatch):
    identity = {"clientId": str(uuid4()), "principalId": str(uuid4()), "id": control.IDENTITY}
    state = _prepared_state(control, run_id)
    state["phase"] = "accepted_stopped"
    _wire_validate_job(
        control,
        monkeypatch,
        state,
        identity["principalId"],
        identity,
        executions=[{"name": "exec-1", "status": "Running"}],
    )

    with pytest.raises(control.ControlError) as excinfo:
        control.cleanup(state)
    assert excinfo.value.args[0] == "execution_active_or_unknown"


def test_cleanup_running_failed_execution_revokes_without_claiming_recovery(
    control, run_id, monkeypatch
):
    identity = {"clientId": str(uuid4()), "principalId": str(uuid4()), "id": control.IDENTITY}
    state = _prepared_state(control, run_id)
    state.update(phase="running", execution="fcag-dev-session-rotation-ownedfailed1")
    deleted = []
    _wire_validate_job(
        control,
        monkeypatch,
        state,
        identity["principalId"],
        identity,
        executions=[{"name": state["execution"], "status": "Failed"}],
    )
    original_rest = control.rest

    def recording_rest(method, resource_id, api, body=None, not_found_code=None):
        if method == "delete":
            deleted.append(resource_id)
        return original_rest(method, resource_id, api, body, not_found_code=not_found_code)

    monkeypatch.setattr(control, "rest", recording_rest)
    control.cleanup(state)

    assert deleted == [control.ASSIGNMENT, control.ACR_ASSIGNMENT, control.ROLE]
    assert state["phase"] == "running"
    assert state["permission_phase"] == "rights_revoked"


@pytest.mark.parametrize(
    "executions",
    [
        [{"name": "foreign-execution", "status": "Failed"}],
        [{"name": "fcag-dev-session-rotation-ownedfailed1", "status": "Running"}],
        [
            {"name": "fcag-dev-session-rotation-ownedfailed1", "status": "Failed"},
            {"name": "foreign-execution", "status": "Failed"},
        ],
    ],
)
def test_cleanup_running_rejects_foreign_or_nonterminal_execution(
    control, run_id, monkeypatch, executions
):
    identity = {"clientId": str(uuid4()), "principalId": str(uuid4()), "id": control.IDENTITY}
    state = _prepared_state(control, run_id)
    state.update(phase="running", execution="fcag-dev-session-rotation-ownedfailed1")
    deleted = []
    _wire_validate_job(
        control,
        monkeypatch,
        state,
        identity["principalId"],
        identity,
        executions=executions,
    )
    original_rest = control.rest

    def recording_rest(method, resource_id, api, body=None, not_found_code=None):
        if method == "delete":
            deleted.append(resource_id)
        return original_rest(method, resource_id, api, body, not_found_code=not_found_code)

    monkeypatch.setattr(control, "rest", recording_rest)

    with pytest.raises(control.ControlError) as excinfo:
        control.cleanup(state)

    assert excinfo.value.args[0] == "owned_failed_execution_not_terminal"
    assert deleted == []
    assert state["phase"] == "running"
    assert "permission_phase" not in state


def test_cleanup_running_rejects_absent_job(control, run_id, monkeypatch):
    identity = {"clientId": str(uuid4()), "principalId": str(uuid4()), "id": control.IDENTITY}
    state = _prepared_state(control, run_id)
    state.update(phase="running", execution="fcag-dev-session-rotation-ownedfailed1")
    _wire_validate_job(
        control,
        monkeypatch,
        state,
        identity["principalId"],
        identity,
        executions=[{"name": state["execution"], "status": "Failed"}],
        already_deleted={control.JOB_ID},
    )

    with pytest.raises(control.ControlError) as excinfo:
        control.cleanup(state)

    assert excinfo.value.args[0] == "owned_failed_job_absent"
    assert state["phase"] == "running"
    assert "permission_phase" not in state


def test_cleanup_revokes_rights_for_contract_valid_failed_job(control, run_id, monkeypatch):
    identity = {"clientId": str(uuid4()), "principalId": str(uuid4()), "id": control.IDENTITY}
    state = _prepared_state(control, run_id)
    state["phase"] = "provision_intent"
    deleted = []
    _wire_validate_job(
        control,
        monkeypatch,
        state,
        identity["principalId"],
        identity,
        job_provisioning_state="Failed",
    )
    original_rest = control.rest

    def recording_rest(method, resource_id, api, body=None, not_found_code=None):
        if method == "delete":
            deleted.append(resource_id)
        return original_rest(method, resource_id, api, body, not_found_code=not_found_code)

    monkeypatch.setattr(control, "rest", recording_rest)
    control.cleanup(state)

    assert state["phase"] == "rights_revoked"
    assert deleted == [control.ASSIGNMENT, control.ACR_ASSIGNMENT, control.ROLE]
    snapshot = control.resource_snapshot(state, allow_failed_job=True)
    assert snapshot["job_provisioning_state"] == "Failed"


def test_cleanup_failed_job_stops_if_execution_appears_between_deletes(
    control, run_id, monkeypatch
):
    identity = {"clientId": str(uuid4()), "principalId": str(uuid4()), "id": control.IDENTITY}
    state = _prepared_state(control, run_id)
    state["phase"] = "provision_intent"
    deleted = []
    _wire_validate_job(
        control,
        monkeypatch,
        state,
        identity["principalId"],
        identity,
        job_provisioning_state="Failed",
    )
    original_azure = control.azure
    execution_checks = iter(([], [], [{"name": "unexpected", "status": "Running"}]))

    def dynamic_azure(args):
        if args[:3] == ["containerapp", "job", "execution"]:
            return next(execution_checks)
        return original_azure(args)

    original_rest = control.rest

    def recording_rest(method, resource_id, api, body=None, not_found_code=None):
        if method == "delete":
            deleted.append(resource_id)
        return original_rest(method, resource_id, api, body, not_found_code=not_found_code)

    monkeypatch.setattr(control, "azure", dynamic_azure)
    monkeypatch.setattr(control, "rest", recording_rest)

    with pytest.raises(control.ControlError) as excinfo:
        control.cleanup(state)

    assert excinfo.value.args[0] == "failed_job_has_executions"
    assert deleted == [control.ASSIGNMENT]


def test_cleanup_app_configuration_drift_blocks_before_next_delete(control, run_id, monkeypatch):
    identity = {"clientId": str(uuid4()), "principalId": str(uuid4()), "id": control.IDENTITY}
    state = _prepared_state(control, run_id)
    deleted = []
    _wire_validate_job(control, monkeypatch, state, identity["principalId"], identity)
    configs = iter((state["config"], {**state["config"], "fingerprint": "drift"}))
    monkeypatch.setattr(control, "app_config", lambda: next(configs))
    original_rest = control.rest

    def recording_rest(method, resource_id, api, body=None, not_found_code=None):
        if method == "delete":
            deleted.append(resource_id)
        return original_rest(method, resource_id, api, body, not_found_code=not_found_code)

    monkeypatch.setattr(control, "rest", recording_rest)

    with pytest.raises(control.ControlError) as excinfo:
        control.cleanup(state)

    assert excinfo.value.args[0] == "app_configuration_changed"
    assert deleted == [control.ASSIGNMENT]
    assert state["phase"] == "provisioned"


def test_cleanup_stale_identity_grant_blocks_rights_revoked_state(control, run_id, monkeypatch):
    identity = {"clientId": str(uuid4()), "principalId": str(uuid4()), "id": control.IDENTITY}
    state = _prepared_state(control, run_id)
    deleted = []
    _wire_validate_job(control, monkeypatch, state, identity["principalId"], identity)
    original_rest = control.rest
    original_identity_assignment_ids = control.identity_assignment_ids

    def recording_rest(method, resource_id, api, body=None, not_found_code=None):
        if method == "delete":
            deleted.append(resource_id)
        return original_rest(method, resource_id, api, body, not_found_code=not_found_code)

    def stale_after_deletes(principal):
        if control.ROLE in deleted:
            return {control.ASSIGNMENT.casefold()}
        return original_identity_assignment_ids(principal)

    monkeypatch.setattr(control, "rest", recording_rest)
    monkeypatch.setattr(control, "identity_assignment_ids", stale_after_deletes)

    with pytest.raises(control.ControlError) as excinfo:
        control.cleanup(state)

    assert excinfo.value.args[0] == "identity_assignment_consistency_invalid"
    assert deleted == [control.ASSIGNMENT, control.ACR_ASSIGNMENT, control.ROLE]
    assert state["phase"] == "provisioned"
    assert "permission_phase" not in state


def test_cleanup_missing_identity_grant_blocks_before_mutation(control, run_id, monkeypatch):
    identity = {"clientId": str(uuid4()), "principalId": str(uuid4()), "id": control.IDENTITY}
    state = _prepared_state(control, run_id)
    deleted = []
    _wire_validate_job(control, monkeypatch, state, identity["principalId"], identity)
    monkeypatch.setattr(
        control,
        "identity_assignment_ids",
        lambda principal: {control.ASSIGNMENT.casefold()},
    )
    original_rest = control.rest

    def recording_rest(method, resource_id, api, body=None, not_found_code=None):
        if method == "delete":
            deleted.append(resource_id)
        return original_rest(method, resource_id, api, body, not_found_code=not_found_code)

    monkeypatch.setattr(control, "rest", recording_rest)

    with pytest.raises(control.ControlError) as excinfo:
        control.cleanup(state)

    assert excinfo.value.args[0] == "identity_assignment_consistency_invalid"
    assert deleted == []
    assert state["phase"] == "provisioned"


def test_cleanup_rejects_from_non_terminal_phase(control, run_id):
    state = _prepared_state(control, run_id)
    state["phase"] = "start_intent"
    with pytest.raises(control.ControlError) as excinfo:
        control.cleanup(state)
    assert excinfo.value.args[0] == "cleanup_requires_terminal_proof"


def test_cleanup_detects_assignment_scope_drift(control, run_id, monkeypatch):
    identity = {"clientId": str(uuid4()), "principalId": str(uuid4()), "id": control.IDENTITY}
    state = _prepared_state(control, run_id)
    _wire_validate_job(control, monkeypatch, state, identity["principalId"], identity)
    original_rest = control.rest

    def drifted_rest(method, resource_id, api, body=None, not_found_code=None):
        if resource_id == control.ASSIGNMENT and method == "get":
            item = original_rest(method, resource_id, api, body, not_found_code=not_found_code)
            item["properties"]["scope"] = control.REGISTRY  # drifted scope
            return item
        return original_rest(method, resource_id, api, body, not_found_code=not_found_code)

    monkeypatch.setattr(control, "rest", drifted_rest)
    with pytest.raises(control.ControlError) as excinfo:
        control.cleanup(state)
    assert excinfo.value.args[0] == "assignment_drift"


def test_cleanup_resumes_after_first_assignment_already_deleted(control, run_id, monkeypatch):
    """A prior, interrupted cleanup attempt already revoked ASSIGNMENT (confirmed
    deleted). A retry must skip it (no re-delete, no drift error) and finish revoking
    ACR_ASSIGNMENT and the shared role definition.
    """
    identity = {"clientId": str(uuid4()), "principalId": str(uuid4()), "id": control.IDENTITY}
    state = _prepared_state(control, run_id)
    deleted = []
    _wire_validate_job(
        control,
        monkeypatch,
        state,
        identity["principalId"],
        identity,
        already_deleted={control.ASSIGNMENT},
    )
    original_rest = control.rest

    def recording_rest(method, resource_id, api, body=None, not_found_code=None):
        if method == "delete":
            deleted.append(resource_id)
        return original_rest(method, resource_id, api, body, not_found_code=not_found_code)

    monkeypatch.setattr(control, "rest", recording_rest)
    control.cleanup(state)

    assert state["phase"] == "rights_revoked"
    assert deleted == [control.ACR_ASSIGNMENT, control.ROLE]


def test_cleanup_resumes_after_both_assignments_already_deleted(control, run_id, monkeypatch):
    """Both role assignments were already revoked by a prior attempt; only the shared
    role definition remains and must still be deleted.
    """
    identity = {"clientId": str(uuid4()), "principalId": str(uuid4()), "id": control.IDENTITY}
    state = _prepared_state(control, run_id)
    deleted = []
    _wire_validate_job(
        control,
        monkeypatch,
        state,
        identity["principalId"],
        identity,
        already_deleted={control.ASSIGNMENT, control.ACR_ASSIGNMENT},
    )
    original_rest = control.rest

    def recording_rest(method, resource_id, api, body=None, not_found_code=None):
        if method == "delete":
            deleted.append(resource_id)
        return original_rest(method, resource_id, api, body, not_found_code=not_found_code)

    monkeypatch.setattr(control, "rest", recording_rest)
    control.cleanup(state)

    assert state["phase"] == "rights_revoked"
    assert deleted == [control.ROLE]


def test_cleanup_is_idempotent_once_fully_clean(control, run_id, monkeypatch):
    """A second cleanup call after everything (both assignments + role) is already
    gone, and phase already "rights_revoked", is a safe no-op — not an error.
    """
    identity = {"clientId": str(uuid4()), "principalId": str(uuid4()), "id": control.IDENTITY}
    state = _prepared_state(control, run_id)
    state["phase"] = "rights_revoked"
    deleted = []
    _wire_validate_job(
        control,
        monkeypatch,
        state,
        identity["principalId"],
        identity,
        already_deleted={control.ASSIGNMENT, control.ACR_ASSIGNMENT, control.ROLE},
    )
    original_rest = control.rest

    def recording_rest(method, resource_id, api, body=None, not_found_code=None):
        if method == "delete":
            deleted.append(resource_id)
        return original_rest(method, resource_id, api, body, not_found_code=not_found_code)

    monkeypatch.setattr(control, "rest", recording_rest)
    control.cleanup(state)

    assert state["phase"] == "rights_revoked"
    assert deleted == []


def test_cleanup_does_not_swallow_unrelated_probe_error(control, run_id, monkeypatch):
    """A non-404, unrelated failure (e.g. authorization/network) while probing an
    assignment must propagate — it must never be treated as "already deleted".
    """
    identity = {"clientId": str(uuid4()), "principalId": str(uuid4()), "id": control.IDENTITY}
    state = _prepared_state(control, run_id)
    _wire_validate_job(control, monkeypatch, state, identity["principalId"], identity)
    original_rest = control.rest

    def flaky_rest(method, resource_id, api, body=None, not_found_code=None):
        if resource_id == control.ASSIGNMENT and method == "get" and not_found_code is not None:
            raise control.ControlError("azure_command_failed")
        return original_rest(method, resource_id, api, body, not_found_code=not_found_code)

    monkeypatch.setattr(control, "rest", flaky_rest)
    with pytest.raises(control.ControlError) as excinfo:
        control.cleanup(state)
    assert excinfo.value.args[0] == "azure_command_failed"
    # No phase change / no partial "success" recorded on an unresolved probe failure.
    assert state["phase"] == "provisioned"


def test_cleanup_does_not_mark_clean_when_delete_fails_partway(control, run_id, monkeypatch):
    """If deleting ACR_ASSIGNMENT fails after ASSIGNMENT was already removed, the
    phase must not be advanced to "rights_revoked" — state stays exactly where a
    resumed cleanup call can pick back up.
    """
    identity = {"clientId": str(uuid4()), "principalId": str(uuid4()), "id": control.IDENTITY}
    state = _prepared_state(control, run_id)
    _wire_validate_job(control, monkeypatch, state, identity["principalId"], identity)
    original_rest = control.rest

    def failing_delete(method, resource_id, api, body=None, not_found_code=None):
        if method == "delete" and resource_id == control.ACR_ASSIGNMENT:
            raise control.ControlError("azure_command_failed")
        return original_rest(method, resource_id, api, body, not_found_code=not_found_code)

    monkeypatch.setattr(control, "rest", failing_delete)
    with pytest.raises(control.ControlError) as excinfo:
        control.cleanup(state)
    assert excinfo.value.args[0] == "azure_command_failed"
    assert state["phase"] == "provisioned"


def test_cleanup_rejects_role_scope_drift_before_role_delete(control, run_id, monkeypatch):
    """The shared role definition's own drift check (role_drift) still runs, re-fetched
    fresh, immediately before its delete — even when resuming after both assignments
    are already gone.
    """
    identity = {"clientId": str(uuid4()), "principalId": str(uuid4()), "id": control.IDENTITY}
    state = _prepared_state(control, run_id)
    _wire_validate_job(
        control,
        monkeypatch,
        state,
        identity["principalId"],
        identity,
        already_deleted={control.ASSIGNMENT, control.ACR_ASSIGNMENT},
    )
    original_rest = control.rest

    def drifted_role_rest(method, resource_id, api, body=None, not_found_code=None):
        if resource_id == control.ROLE and method == "get":
            props = _role_properties(control)
            props["permissions"][0]["dataActions"] = [
                *control.DATA_ACTIONS,
                "Microsoft.KeyVault/vaults/purge/action",
            ]
            return {"id": control.ROLE, "properties": props}
        return original_rest(method, resource_id, api, body, not_found_code=not_found_code)

    monkeypatch.setattr(control, "rest", drifted_role_rest)
    with pytest.raises(control.ControlError) as excinfo:
        control.cleanup(state)
    assert excinfo.value.args[0] == "role_drift"
    assert state["phase"] == "provisioned"


# ---------------------------------------------------------------------------
# rest(): real not_found_code parsing against actual az CLI (2.74.0) stderr shapes.
#
# Every test above fakes `control.rest` wholesale, so `rest()`'s own stderr parsing
# is never actually exercised there. These tests instead fake only `control._run_az`
# (a `subprocess.CompletedProcess`-shaped stand-in for the real `az rest` subprocess)
# and call the real `control.rest(..., not_found_code=...)`, so the parsing logic
# under test is the genuine implementation.
#
# The exact stderr text used below (both the plain "ERROR: " form and the ANSI
# color-only form) was captured by running installed azure-cli 2.74.0's
# `az rest --method GET --url <mock 404 endpoint> --only-show-errors -o json`
# against a local HTTP server returning the given ARM-shaped JSON error body, once
# piped (matching this module's real subprocess.run(capture_output=True) usage,
# which never has a stderr, so is always in the plain "ERROR: " form) and once
# attached to a pty (to also cover the color-only rendering knack uses when stderr
# is a tty), confirming both are handled identically by the same exact-shape parse.
# ---------------------------------------------------------------------------


def _completed(returncode, stdout="", stderr=""):
    import subprocess as _subprocess

    return _subprocess.CompletedProcess(
        args=["az"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _arm_error_stderr(code, message="details", reason="Not Found", colored=False):
    body = json.dumps({"error": {"code": code, "message": message}})
    if colored:
        return f"\x1b[91m{reason}({body})\x1b[0m\r\n"
    return f"ERROR: {reason}({body})\n"


@pytest.mark.parametrize("colored", [False, True], ids=["plain", "color"])
@pytest.mark.parametrize(
    "not_found_code",
    ["RoleAssignmentNotFound", "RoleDefinitionDoesNotExist"],
)
def test_rest_not_found_accepts_real_az_404_shape_for_expected_code(
    control, monkeypatch, not_found_code, colored
):
    stderr = _arm_error_stderr(not_found_code, colored=colored)
    monkeypatch.setattr(control, "_run_az", lambda args: _completed(1, stderr=stderr))
    assert (
        control.rest("get", control.ASSIGNMENT, "2022-04-01", not_found_code=not_found_code) is None
    )


@pytest.mark.parametrize("colored", [False, True], ids=["plain", "color"])
def test_rest_not_found_rejects_different_documented_code(control, monkeypatch, colored):
    """A real 404 for a *different* ARM resource type than the one being probed must
    not be treated as "already deleted" — only the caller's exact expected code
    counts.
    """
    stderr = _arm_error_stderr("RoleDefinitionDoesNotExist", colored=colored)
    monkeypatch.setattr(control, "_run_az", lambda args: _completed(1, stderr=stderr))
    with pytest.raises(control.ControlError) as excinfo:
        control.rest(
            "get", control.ASSIGNMENT, "2022-04-01", not_found_code="RoleAssignmentNotFound"
        )
    assert excinfo.value.args[0] == "azure_command_failed"


@pytest.mark.parametrize("colored", [False, True], ids=["plain", "color"])
def test_rest_not_found_rejects_403_authorization_failure(control, monkeypatch, colored):
    stderr = _arm_error_stderr("AuthorizationFailed", reason="Forbidden", colored=colored)
    monkeypatch.setattr(control, "_run_az", lambda args: _completed(1, stderr=stderr))
    with pytest.raises(control.ControlError) as excinfo:
        control.rest(
            "get", control.ASSIGNMENT, "2022-04-01", not_found_code="RoleAssignmentNotFound"
        )
    assert excinfo.value.args[0] == "azure_command_failed"


def test_rest_not_found_rejects_transient_throttling_error(control, monkeypatch):
    stderr = _arm_error_stderr("TooManyRequests", reason="Too Many Requests")
    monkeypatch.setattr(control, "_run_az", lambda args: _completed(1, stderr=stderr))
    with pytest.raises(control.ControlError) as excinfo:
        control.rest(
            "get", control.ASSIGNMENT, "2022-04-01", not_found_code="RoleAssignmentNotFound"
        )
    assert excinfo.value.args[0] == "azure_command_failed"


def test_rest_not_found_rejects_malformed_json_body(control, monkeypatch):
    stderr = "ERROR: Not Found(this is not json)\n"
    monkeypatch.setattr(control, "_run_az", lambda args: _completed(1, stderr=stderr))
    with pytest.raises(control.ControlError) as excinfo:
        control.rest(
            "get", control.ASSIGNMENT, "2022-04-01", not_found_code="RoleAssignmentNotFound"
        )
    assert excinfo.value.args[0] == "azure_command_failed"


def test_rest_not_found_rejects_missing_response_body(control, monkeypatch):
    """No body at all (e.g. a bare CLI failure with no HTTPError rendering) fails
    closed rather than being treated as a match.
    """
    stderr = "ERROR: something went wrong\n"
    monkeypatch.setattr(control, "_run_az", lambda args: _completed(2, stderr=stderr))
    with pytest.raises(control.ControlError) as excinfo:
        control.rest(
            "get", control.ASSIGNMENT, "2022-04-01", not_found_code="RoleAssignmentNotFound"
        )
    assert excinfo.value.args[0] == "azure_command_failed"


def test_rest_not_found_rejects_code_only_mentioned_inside_message(control, monkeypatch):
    """The exact ARM code text appearing inside `message` (not `code`) — e.g. an
    attacker- or bug-injected string designed to look like a match — must never be
    accepted; only the structured `error.code` field is read.
    """
    stderr = _arm_error_stderr(
        "AuthorizationFailed",
        message="Caller lacks permission (see RoleAssignmentNotFound for context)",
        reason="Forbidden",
    )
    monkeypatch.setattr(control, "_run_az", lambda args: _completed(1, stderr=stderr))
    with pytest.raises(control.ControlError) as excinfo:
        control.rest(
            "get", control.ASSIGNMENT, "2022-04-01", not_found_code="RoleAssignmentNotFound"
        )
    assert excinfo.value.args[0] == "azure_command_failed"


def test_rest_not_found_rejects_arbitrary_unrelated_chatter(control, monkeypatch):
    stderr = "Please run 'az login' to setup account.\n"
    monkeypatch.setattr(control, "_run_az", lambda args: _completed(1, stderr=stderr))
    with pytest.raises(control.ControlError) as excinfo:
        control.rest(
            "get", control.ASSIGNMENT, "2022-04-01", not_found_code="RoleAssignmentNotFound"
        )
    assert excinfo.value.args[0] == "azure_command_failed"


def test_rest_not_found_success_path_parses_existing_resource_normally(control, monkeypatch):
    """The not_found_code branch's success path (returncode == 0) is unaffected —
    an existing resource is parsed exactly like a plain rest() call.
    """
    payload = {"id": control.ASSIGNMENT, "properties": {"principalId": "abc"}}
    monkeypatch.setattr(control, "_run_az", lambda args: _completed(0, stdout=json.dumps(payload)))
    result = control.rest(
        "get", control.ASSIGNMENT, "2022-04-01", not_found_code="RoleAssignmentNotFound"
    )
    assert result == payload


def test_cleanup_end_to_end_resumes_partial_deletion_via_real_rest(control, run_id, monkeypatch):
    """End-to-end (through the real `rest()` and its real not_found parsing, with only
    the `az` subprocess faked): resuming a cleanup after ASSIGNMENT was already
    deleted by a prior, interrupted attempt must skip it (via a genuine ARM 404
    rendering) and still delete ACR_ASSIGNMENT and the role normally.
    """
    identity = {"clientId": str(uuid4()), "principalId": str(uuid4()), "id": control.IDENTITY}
    principal = identity["principalId"]
    state = _prepared_state(control, run_id)

    calls = {"deleted": []}
    monkeypatch.setattr(control, "validate_current_baseline", lambda state: ({}, {}))
    monkeypatch.setattr(control, "app_config", lambda: state["config"])

    def fake_run_az(args):
        # `azure()`-style calls (no --method/--url "rest" subcommand args distinguish
        # them): reuse the JSON stdout shape real `az ... -o json` produces.
        if args[0] == "identity":
            return _completed(
                0,
                stdout=json.dumps(
                    {
                        "clientId": identity["clientId"],
                        "principalId": principal,
                        "id": identity["id"],
                    }
                ),
            )
        if args[0] == "containerapp" and args[1] == "job":
            return _completed(0, stdout=json.dumps([]))
        if args[0] == "role" and args[1] == "assignment" and args[2] == "list":
            if "--assignee-object-id" in args:
                return _completed(
                    0,
                    stdout=json.dumps(
                        []
                        if control.ACR_ASSIGNMENT in calls["deleted"]
                        else [control.ACR_ASSIGNMENT]
                    ),
                )
            return _completed(0, stdout=json.dumps([]))
        if args[0] == "rest":
            method = args[2]
            url = args[4]
            resource_id = url.split("?", 1)[0][len("https://management.azure.com") :]
            if method == "get" and resource_id == control.IDENTITY:
                return _completed(
                    0,
                    stdout=json.dumps(
                        {
                            "id": identity["id"],
                            "name": control.JOB + "-id",
                            "type": "Microsoft.ManagedIdentity/userAssignedIdentities",
                            "location": "eastus2",
                            "tags": None,
                            "properties": {
                                "clientId": identity["clientId"],
                                "principalId": principal,
                            },
                        }
                    ),
                )
            if method == "get" and resource_id == control.JOB_ID:
                return _completed(0, stdout=json.dumps(_job_resource(control, state, identity)))
            if method == "get" and resource_id == control.ASSIGNMENT:
                # Already gone: a real ARM 404 with the documented code, rendered
                # exactly as az CLI 2.74.0 emits it.
                return _completed(1, stderr=_arm_error_stderr("RoleAssignmentNotFound"))
            if method == "get" and resource_id == control.ACR_ASSIGNMENT:
                if resource_id in calls["deleted"]:
                    return _completed(1, stderr=_arm_error_stderr("RoleAssignmentNotFound"))
                return _completed(
                    0,
                    stdout=json.dumps(
                        {
                            "id": control.ACR_ASSIGNMENT,
                            "properties": {
                                "principalId": principal,
                                "roleDefinitionId": control.ACR_ROLE,
                                "scope": control.REGISTRY,
                                "principalType": "ServicePrincipal",
                            },
                        }
                    ),
                )
            if method == "delete" and resource_id in (control.ACR_ASSIGNMENT, control.ROLE):
                calls["deleted"].append(resource_id)
                return _completed(0, stdout="")
            if method == "get" and resource_id == control.ROLE:
                if resource_id in calls["deleted"]:
                    return _completed(1, stderr=_arm_error_stderr("RoleDefinitionDoesNotExist"))
                return _completed(
                    0,
                    stdout=json.dumps(
                        {"id": control.ROLE, "properties": _role_properties(control)}
                    ),
                )
            raise AssertionError(f"unexpected rest() call: {method} {resource_id}")
        raise AssertionError(f"unexpected azure() call: {args}")

    monkeypatch.setattr(control, "_run_az", fake_run_az)
    control.cleanup(state)

    assert state["phase"] == "rights_revoked"
    assert calls["deleted"] == [control.ACR_ASSIGNMENT, control.ROLE]


# ---------------------------------------------------------------------------
# observations(): only content-free, contract-valid envelopes are trusted
# ---------------------------------------------------------------------------


def _valid_row(control, **overrides):
    row = {
        "event": "secret.rotation_observation",
        "schema_version": 1,
        "sequence": 1,
        "pid": 100,
        "incarnation": "a" * 32,
        "revision": "rev0",
        "replica": "replica-0",
        "logical_secret": "session",
        "source": "azure",
        "stage": "provider",
        "result": "unchanged",
        "version_hash": "1" * 12,
        "error_category": "none",
        "observed_at": "2026-01-01T00:00:00.000000Z",
    }
    row.update(overrides)
    return row


def test_observations_rejects_invalid_envelope(control, monkeypatch):
    monkeypatch.setattr(
        control, "query_logs", lambda query: [_valid_row(control, event="not-the-event")]
    )
    with pytest.raises(control.ControlError) as excinfo:
        control.observations("rev0")
    assert excinfo.value.args[0] == "observation_invalid"


def test_observations_rejects_replica_source_mismatch(control, monkeypatch):
    monkeypatch.setattr(
        control, "query_logs", lambda query: [_valid_row(control, replica="unknown")]
    )
    with pytest.raises(control.ControlError) as excinfo:
        control.observations("rev0")
    assert excinfo.value.args[0] == "observation_source_unproved"


def test_observations_accepts_well_formed_rows(control, monkeypatch):
    monkeypatch.setattr(control, "query_logs", lambda query: [_valid_row(control)])
    rows = control.observations("rev0")
    assert len(rows) == 1
    assert rows[0]["logical_secret"] == "session"


# ---------------------------------------------------------------------------
# measure(): overlap-window adoption proof and safety failures
# ---------------------------------------------------------------------------


def _iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def test_measure_returns_false_before_any_rotation_within_grace_window(control):
    started = datetime.now(UTC)
    state = {
        "started_at": _iso(started),
        "baseline": {"session_hash": "1" * 12, "at": _iso(started)},
        "members": {"replica-0": {"pid": 1, "start_ticks": 1}},
    }
    finished = control.measure(state, [], [], {"replica-0": {"pid": 1, "start_ticks": 1}}, started)
    assert finished is False


def test_measure_raises_write_signal_missing_after_grace_window(control):
    started = datetime.now(UTC) - timedelta(seconds=200)
    state = {
        "started_at": _iso(started),
        "baseline": {"session_hash": "1" * 12, "at": _iso(started)},
        "members": {"replica-0": {"pid": 1, "start_ticks": 1}},
    }
    with pytest.raises(control.ControlError) as excinfo:
        control.measure(
            state, [], [], {"replica-0": {"pid": 1, "start_ticks": 1}}, datetime.now(UTC)
        )
    assert excinfo.value.args[0] == "write_signal_missing"


def test_measure_raises_on_blocked_drill_event(control):
    started = datetime.now(UTC)
    state = {
        "started_at": _iso(started),
        "baseline": {"session_hash": "1" * 12, "at": _iso(started)},
        "members": {"replica-0": {"pid": 1, "start_ticks": 1}},
    }
    events = [{"stage": "blocked", "at": _iso(started), "version_hash": "", "created_on": ""}]
    with pytest.raises(control.ControlError) as excinfo:
        control.measure(state, [], events, {"replica-0": {"pid": 1, "start_ticks": 1}}, started)
    assert excinfo.value.args[0] == "private_drill_failed"


def test_measure_raises_adoption_lost_when_worker_never_catches_up(control):
    baseline_at = datetime.now(UTC) - timedelta(seconds=200)
    created = baseline_at + timedelta(seconds=5)
    target_hash = "2" * 12
    baseline = {
        "session_hash": "1" * 12,
        "at": _iso(baseline_at),
        "processes": [
            [  # noqa: E501
                "rev0",
                "replica-0",
                1,
                "inc-0",
            ]
        ],
    }
    state = {
        "started_at": _iso(baseline_at),
        "baseline": baseline,
        "members": {"replica-0": {"pid": 1, "start_ticks": 1}},
    }
    events = [
        {
            "stage": "rotated",
            "at": _iso(created),
            "version_hash": target_hash,
            "created_on": _iso(created),
        }
    ]
    rows = [
        {
            "revision": "rev0",
            "replica": "replica-0",
            "pid": 1,
            "incarnation": "inc-0",
            "logical_secret": "session",
            "stage": "provider",
            "source": "azure",
            "result": "unchanged",
            "error_category": "none",
            "version_hash": "1" * 12,
            "observed_at": _iso(created + timedelta(seconds=61)),
        }
    ]
    with pytest.raises(control.ControlError) as excinfo:
        control.measure(
            state,
            rows,
            events,
            {"replica-0": {"pid": 1, "start_ticks": 1}},
            created + timedelta(seconds=62),
        )
    assert excinfo.value.args[0] == "adoption_lost"


def test_measure_boundary_row_at_just_over_60s_is_neither_adopted_nor_fatal(control):
    """elapsed + CLOCK_MARGIN > 60 but observed <= created + 60s: the row must not
    count as adoption evidence, but must also not raise `adoption_lost` (that is
    reserved for evidence strictly past the 60s wall). This pins the exact boundary
    so `adopted_seconds_upper_bound` can never silently include >60s evidence."""
    baseline_at = datetime.now(UTC) - timedelta(seconds=10)
    created = baseline_at + timedelta(seconds=1)
    target_hash = "2" * 12
    baseline = {
        "session_hash": "1" * 12,
        "at": _iso(baseline_at),
        "processes": [["rev0", "replica-0", 1, "inc-0"]],
    }
    state = {
        "started_at": _iso(baseline_at),
        "baseline": baseline,
        "members": {"replica-0": {"pid": 1, "start_ticks": 1}},
    }
    events = [
        {
            "stage": "rotated",
            "at": _iso(created),
            "version_hash": target_hash,
            "created_on": _iso(created),
        }
    ]
    # elapsed = 58s; 58 + MARGIN(3) = 61 > 60 so must be dropped, not adopted.
    boundary_observed = created + timedelta(seconds=58)
    rows = [
        {
            "revision": "rev0",
            "replica": "replica-0",
            "pid": 1,
            "incarnation": "inc-0",
            "logical_secret": "session",
            "stage": "provider",
            "source": "azure",
            "result": "adopted",
            "error_category": "none",
            "version_hash": target_hash,
            "observed_at": _iso(boundary_observed),
        }
    ]
    finished = control.measure(
        state, rows, events, {"replica-0": {"pid": 1, "start_ticks": 1}}, boundary_observed
    )
    assert finished is False
    assert state["adoption_seconds_upper_bound"] == []


def test_measure_accepts_row_just_inside_60s_boundary(control):
    """elapsed=56s -> 56+MARGIN(3)=59<=60: must be recorded as adoption evidence."""
    baseline_at = datetime.now(UTC) - timedelta(seconds=10)
    created = baseline_at + timedelta(seconds=1)
    target_hash = "2" * 12
    baseline = {
        "session_hash": "1" * 12,
        "at": _iso(baseline_at),
        "processes": [["rev0", "replica-0", 1, "inc-0"]],
    }
    state = {
        "started_at": _iso(baseline_at),
        "baseline": baseline,
        "members": {"replica-0": {"pid": 1, "start_ticks": 1}},
    }
    events = [
        {
            "stage": "rotated",
            "at": _iso(created),
            "version_hash": target_hash,
            "created_on": _iso(created),
        }
    ]
    observed = created + timedelta(seconds=56)
    rows = [
        {
            "revision": "rev0",
            "replica": "replica-0",
            "pid": 1,
            "incarnation": "inc-0",
            "logical_secret": "session",
            "stage": "provider",
            "source": "azure",
            "result": "adopted",
            "error_category": "none",
            "version_hash": target_hash,
            "observed_at": _iso(observed),
        }
    ]
    finished = control.measure(
        state, rows, events, {"replica-0": {"pid": 1, "start_ticks": 1}}, observed
    )
    assert finished is False
    assert state["adoption_seconds_upper_bound"] == [
        {"process": ["rev0", "replica-0", 1, "inc-0"], "seconds": 59.0}
    ]


# ---------------------------------------------------------------------------
# inventory(): revision/replica/container membership and health checks
# ---------------------------------------------------------------------------


_CONFIG = {"revision": "rev0", "container": "web-nat-dev"}


def test_inventory_rejects_when_active_revision_set_changed(control, monkeypatch):
    def fake_azure(args):
        if args[1] == "revision":
            return [{"name": "rev0", "health": "Healthy"}, {"name": "rev1", "health": "Healthy"}]
        raise AssertionError("replica list should not be reached")

    monkeypatch.setattr(control, "azure", fake_azure)
    with pytest.raises(control.ControlError) as excinfo:
        control.inventory(_CONFIG)
    assert excinfo.value.args[0] == "revision_membership_changed"


def test_inventory_rejects_when_active_revision_unhealthy(control, monkeypatch):
    def fake_azure(args):
        if args[1] == "revision":
            return [{"name": "rev0", "health": "Unhealthy"}]
        raise AssertionError("replica list should not be reached")

    monkeypatch.setattr(control, "azure", fake_azure)
    with pytest.raises(control.ControlError) as excinfo:
        control.inventory(_CONFIG)
    assert excinfo.value.args[0] == "revision_membership_changed"


def test_inventory_rejects_when_no_replicas_present(control, monkeypatch):
    def fake_azure(args):
        if args[1] == "revision":
            return [{"name": "rev0", "health": "Healthy"}]
        if args[1] == "replica":
            return []
        raise AssertionError(args)

    monkeypatch.setattr(control, "azure", fake_azure)
    with pytest.raises(control.ControlError) as excinfo:
        control.inventory(_CONFIG)
    assert excinfo.value.args[0] == "replicas_missing"


def test_inventory_rejects_when_container_set_changed(control, monkeypatch):
    def fake_azure(args):
        if args[1] == "revision":
            return [{"name": "rev0", "health": "Healthy"}]
        if args[1] == "replica":
            return [
                {
                    "name": "replica-0",
                    "containers": [
                        {"name": "web-nat-dev", "restarts": 0, "ready": True, "running": "Running"},
                        {"name": "sidecar", "restarts": 0, "ready": True, "running": "Running"},
                    ],
                }
            ]
        raise AssertionError(args)

    monkeypatch.setattr(control, "azure", fake_azure)
    with pytest.raises(control.ControlError) as excinfo:
        control.inventory(_CONFIG)
    assert excinfo.value.args[0] == "container_inventory_changed"


@pytest.mark.parametrize(
    "container",
    [
        {"name": "web-nat-dev", "restarts": 0, "ready": False, "running": "Running"},
        {"name": "web-nat-dev", "restarts": 0, "ready": True, "running": "Terminated"},
        {"name": "web-nat-dev", "restarts": -1, "ready": True, "running": "Running"},
        {"name": "web-nat-dev", "restarts": "0", "ready": True, "running": "Running"},
    ],
)
def test_inventory_rejects_unhealthy_replica(control, monkeypatch, container):
    def fake_azure(args):
        if args[1] == "revision":
            return [{"name": "rev0", "health": "Healthy"}]
        if args[1] == "replica":
            return [{"name": "replica-0", "containers": [container]}]
        raise AssertionError(args)

    monkeypatch.setattr(control, "azure", fake_azure)
    with pytest.raises(control.ControlError) as excinfo:
        control.inventory(_CONFIG)
    assert excinfo.value.args[0] == "replica_unhealthy"


def test_inventory_builds_members_from_all_healthy_replicas(control, monkeypatch):
    def fake_azure(args):
        if args[1] == "revision":
            return [{"name": "rev0", "health": "Healthy"}]
        if args[1] == "replica":
            return [
                {
                    "name": "replica-0",
                    "containers": [
                        {"name": "web-nat-dev", "restarts": 0, "ready": True, "running": "Running"}
                    ],
                },
                {
                    "name": "replica-1",
                    "containers": [
                        {"name": "web-nat-dev", "restarts": 2, "ready": True, "running": "Running"}
                    ],
                },
            ]
        raise AssertionError(args)

    # process_inventory() itself (the exec/PTY wrapper) is a separately tested function;
    # stubbing it here keeps this test focused on inventory()'s own membership/health logic.
    seen = []

    def fake_process_inventory(revision, replica, container):
        seen.append((revision, replica, container))
        return {"pid": 100 + len(seen), "start_ticks": 200 + len(seen)}

    monkeypatch.setattr(control, "azure", fake_azure)
    monkeypatch.setattr(control, "process_inventory", fake_process_inventory)
    members = control.inventory(_CONFIG)
    assert set(members) == {"replica-0", "replica-1"}
    assert members["replica-0"]["restarts"] == 0
    assert members["replica-1"]["restarts"] == 2
    assert seen == [
        ("rev0", "replica-0", "web-nat-dev"),
        ("rev0", "replica-1", "web-nat-dev"),
    ]


# ---------------------------------------------------------------------------
# process_inventory(): the `az containerapp exec` PTY wrapper. A real PTY pair
# is used (pty.openpty()); only the process spawn itself (subprocess.Popen) is
# faked, writing bytes into the real slave fd exactly as a real `az` child would.
# This exercises the real select()/os.read() draining loop, not a mock of it.
# ---------------------------------------------------------------------------


class _FakeProcess:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self._polled = False

    def poll(self):
        # First poll (mid-loop) reports "still running" so the real drain loop
        # keeps reading until EOF; only report the final code once EOF was hit.
        if not self._polled:
            self._polled = True
            return None
        return self.returncode

    def terminate(self):
        pass

    def wait(self, timeout=None):
        pass


def _fake_popen(
    output: bytes | None = None, returncode: int = 0, payload_factory=None, args_sink=None
):
    """`payload_factory`, when given, is called at Popen-invocation time (i.e. after
    process_inventory()'s own `started = now()`) so the emitted `at` timestamp is
    always genuinely inside the real started..ended bracket -- not stamped before
    the call even begins."""

    def _popen(args, stdin=None, stdout=None, stderr=None):
        if args_sink is not None:
            args_sink.append(args)
        body = output
        if payload_factory is not None:
            payload = payload_factory()
            body = b"SESSION_PROCESS_INVENTORY=" + json.dumps(payload).encode() + b"\n"
        revision = args[args.index("--revision") + 1]
        replica = args[args.index("--replica") + 1]
        container = args[args.index("--container") + 1]
        connected = (
            f"INFO: Successfully connected to container: '{container}'. "
            f"[ Revision: '{revision}', Replica: '{replica}'].\r\n"
        ).encode()
        os.write(stdout, connected + body)
        return _FakeProcess(returncode)

    return _popen


def test_process_inventory_rejects_nonzero_exit(control, monkeypatch):
    monkeypatch.setattr(control.subprocess, "Popen", _fake_popen(b"garbage\n", returncode=1))
    with pytest.raises(control.ControlError) as excinfo:
        control.process_inventory("rev0", "replica-0", "web-nat-dev")
    assert excinfo.value.args[0] == "process_inventory_failed"


def test_process_inventory_rejects_missing_marker_line(control, monkeypatch):
    monkeypatch.setattr(control.subprocess, "Popen", _fake_popen(b"no marker here\n"))
    with pytest.raises(control.ControlError) as excinfo:
        control.process_inventory("rev0", "replica-0", "web-nat-dev")
    assert excinfo.value.args[0] == "process_inventory_missing"


def _inventory_payload(control, **overrides):
    payload = {
        "schema": 1,
        "unknown": 0,
        "workers": [{"pid": 4242, "start_ticks": 100}],
        "at": control.harness.stamp(datetime.now(UTC)),
    }
    payload.update(overrides)
    return payload


def test_process_inventory_rejects_when_unknown_processes_present(control, monkeypatch):
    payload = _inventory_payload(control, unknown=1)
    line = b"SESSION_PROCESS_INVENTORY=" + json.dumps(payload).encode() + b"\n"
    monkeypatch.setattr(control.subprocess, "Popen", _fake_popen(line))
    with pytest.raises(control.ControlError) as excinfo:
        control.process_inventory("rev0", "replica-0", "web-nat-dev")
    assert excinfo.value.args[0] == "worker_coverage_unknown"


def test_process_inventory_rejects_when_worker_count_is_not_exactly_one(control, monkeypatch):
    payload = _inventory_payload(
        control, workers=[{"pid": 1, "start_ticks": 1}, {"pid": 2, "start_ticks": 2}]
    )
    line = b"SESSION_PROCESS_INVENTORY=" + json.dumps(payload).encode() + b"\n"
    monkeypatch.setattr(control.subprocess, "Popen", _fake_popen(line))
    with pytest.raises(control.ControlError) as excinfo:
        control.process_inventory("rev0", "replica-0", "web-nat-dev")
    assert excinfo.value.args[0] == "worker_coverage_unknown"


@pytest.mark.parametrize(
    "worker",
    [
        {"pid": 0, "start_ticks": 100},
        {"pid": -1, "start_ticks": 100},
        {"pid": "4242", "start_ticks": 100},
        {"pid": 4242, "start_ticks": 0},
        {"pid": 4242, "start_ticks": "100"},
    ],
)
def test_process_inventory_rejects_invalid_pid_or_start_ticks(control, monkeypatch, worker):
    payload = _inventory_payload(control, workers=[worker])
    line = b"SESSION_PROCESS_INVENTORY=" + json.dumps(payload).encode() + b"\n"
    monkeypatch.setattr(control.subprocess, "Popen", _fake_popen(line))
    with pytest.raises(control.ControlError) as excinfo:
        control.process_inventory("rev0", "replica-0", "web-nat-dev")
    assert excinfo.value.args[0] == "process_inventory_invalid"


def test_process_inventory_rejects_clock_bound_violation(control, monkeypatch):
    payload = _inventory_payload(control)
    line = b"SESSION_PROCESS_INVENTORY=" + json.dumps(payload).encode() + b"\n"
    monkeypatch.setattr(control.subprocess, "Popen", _fake_popen(line))
    # Force the wall-clock bracket (started..ended) to exceed CLOCK_MARGIN so the
    # conservative bound can no longer be proved, independent of a slow exec.
    real_now = control.now
    calls = {"n": 0}

    def fake_now():
        calls["n"] += 1
        if calls["n"] == 1:
            return real_now()
        return real_now() + timedelta(seconds=control.MARGIN + 5)

    monkeypatch.setattr(control, "now", fake_now)
    with pytest.raises(control.ControlError) as excinfo:
        control.process_inventory("rev0", "replica-0", "web-nat-dev")
    assert excinfo.value.args[0] == "worker_clock_bound_unproved"


def test_process_inventory_accepts_healthy_worker(control, monkeypatch):
    calls = []
    monkeypatch.setattr(
        control.subprocess,
        "Popen",
        _fake_popen(payload_factory=lambda: _inventory_payload(control), args_sink=calls),
    )
    worker = control.process_inventory("rev0", "replica-0", "web-nat-dev")
    assert worker == {"pid": 4242, "start_ticks": 100}
    command = calls[0][calls[0].index("--command") + 1]
    assert command.startswith('/app/.venv/bin/python -I -c "import base64;exec(')
    assert "SESSION_PROCESS_INVENTORY" not in command


# ---------------------------------------------------------------------------
# observations(): Kusto timestamp rendering and envelope validation
# ---------------------------------------------------------------------------


def _observation_row(**overrides):
    row = {
        "event": "secret.rotation_observation",
        "schema_version": 1,
        "observed_at": "2026-09-14T07:33:07.884383Z",
        "sequence": 1,
        "pid": 10,
        "incarnation": "d63edabb1e1944e4bb0419000819011f",
        "revision": "rev0",
        "replica": "replica-0",
        "logical_secret": "session",
        "source": "azure",
        "stage": "provider",
        "result": "unchanged",
        "version_hash": "1" * 12,
        "error_category": "none",
    }
    row.update(overrides)
    return row


def test_observations_accepts_kusto_seven_digit_timestamp(control, monkeypatch):
    monkeypatch.setattr(
        control,
        "query_logs",
        lambda query: [_observation_row(observed_at="2026-09-14T07:33:07.8843830Z")],
    )
    rows = control.observations("rev0")
    assert rows[0]["observed_at"] == "2026-09-14T07:33:07.884383Z"


@pytest.mark.parametrize(
    "observed_at",
    [
        "2026-09-14T07:33:07.8843831Z",
        "2026-09-14T07:33:07.88438Z",
        "2026-09-14T07:33:07Z",
    ],
)
def test_observations_rejects_other_timestamp_shapes(control, monkeypatch, observed_at):
    monkeypatch.setattr(
        control, "query_logs", lambda query: [_observation_row(observed_at=observed_at)]
    )
    with pytest.raises(control.ControlError) as excinfo:
        control.observations("rev0")
    assert excinfo.value.args[0] == "observation_invalid"


# ---------------------------------------------------------------------------
# baseline(): per-replica log-stream reconciliation into a trusted snapshot
# ---------------------------------------------------------------------------


def _baseline_row(**overrides):
    row = {
        "revision": "rev0",
        "replica": "replica-0",
        "pid": 1,
        "incarnation": "inc-0",
        "logical_secret": "session",
        "source": "azure",
        "stage": "provider",
        "result": "unchanged",
        "error_category": "none",
        "version_hash": "1" * 12,
        "observed_at": None,
    }
    row.update(overrides)
    return row


def _full_stream_rows(
    at, replica="replica-0", pid=1, incarnation="inc-0", session_hash="1" * 12, entra_hash="2" * 12
):
    observed = _iso(at)
    return [
        _baseline_row(
            replica=replica,
            pid=pid,
            incarnation=incarnation,
            logical_secret="session",
            stage="provider",
            result="unchanged",
            version_hash=session_hash,
            observed_at=observed,
        ),
        _baseline_row(
            replica=replica,
            pid=pid,
            incarnation=incarnation,
            logical_secret="entra",
            stage="provider",
            result="unchanged",
            version_hash=entra_hash,
            observed_at=observed,
        ),
        _baseline_row(
            replica=replica,
            pid=pid,
            incarnation=incarnation,
            logical_secret="entra",
            stage="entra_binding",
            result="unchanged",
            version_hash=entra_hash,
            observed_at=observed,
        ),
    ]


def test_baseline_rejects_duplicate_or_restarted_process_for_a_replica(control):
    at = datetime.now(UTC)
    rows = _full_stream_rows(at) + _full_stream_rows(at, incarnation="inc-1")
    members = {"replica-0": {"pid": 1, "start_ticks": 1}}
    with pytest.raises(control.ControlError) as excinfo:
        control.baseline(members, rows, "rev0", at)
    assert excinfo.value.args[0] == "baseline_process_missing_or_restarted"


def test_baseline_rejects_missing_stream(control):
    at = datetime.now(UTC)
    rows = _full_stream_rows(at)[:2]  # drop entra_binding
    members = {"replica-0": {"pid": 1, "start_ticks": 1}}
    with pytest.raises(control.ControlError) as excinfo:
        control.baseline(members, rows, "rev0", at)
    assert excinfo.value.args[0] == "baseline_stream_missing"


def test_baseline_rejects_unhealthy_row_bad_hash_format(control):
    at = datetime.now(UTC)
    rows = _full_stream_rows(at)
    rows[0]["version_hash"] = "not-a-hash"
    members = {"replica-0": {"pid": 1, "start_ticks": 1}}
    with pytest.raises(control.ControlError) as excinfo:
        control.baseline(members, rows, "rev0", at)
    assert excinfo.value.args[0] == "baseline_unhealthy"


def test_baseline_rejects_unhealthy_row_wrong_result(control):
    at = datetime.now(UTC)
    rows = _full_stream_rows(at)
    rows[0]["result"] = "adopted"  # still not "unchanged"; loses the required unchanged row
    members = {"replica-0": {"pid": 1, "start_ticks": 1}}
    with pytest.raises(control.ControlError) as excinfo:
        control.baseline(members, rows, "rev0", at)
    assert excinfo.value.args[0] == "baseline_unchanged_missing"


def test_baseline_rejects_version_instability_across_replicas(control):
    at = datetime.now(UTC)
    rows = _full_stream_rows(at, replica="replica-0", pid=1, incarnation="inc-0")
    rows += _full_stream_rows(
        at, replica="replica-1", pid=2, incarnation="inc-1", session_hash="3" * 12
    )
    members = {
        "replica-0": {"pid": 1, "start_ticks": 1},
        "replica-1": {"pid": 2, "start_ticks": 1},
    }
    with pytest.raises(control.ControlError) as excinfo:
        control.baseline(members, rows, "rev0", at)
    assert excinfo.value.args[0] == "baseline_version_unstable"


def test_baseline_rejects_unaccounted_worker_in_recent_window(control):
    at = datetime.now(UTC)
    rows = _full_stream_rows(at)
    # A row for a replica/pid not present in `members` at all -- an extra worker
    # observed in logs that inventory() never saw.
    rows += [
        _baseline_row(
            replica="replica-ghost",
            pid=999,
            incarnation="inc-ghost",
            logical_secret="session",
            stage="provider",
            result="unchanged",
            version_hash="1" * 12,
            observed_at=_iso(at),
        )
    ]
    members = {"replica-0": {"pid": 1, "start_ticks": 1}}
    with pytest.raises(control.ControlError) as excinfo:
        control.baseline(members, rows, "rev0", at)
    assert excinfo.value.args[0] == "unaccounted_worker"


def test_baseline_accepts_healthy_multi_replica_snapshot(control):
    at = datetime.now(UTC)
    rows = _full_stream_rows(at, replica="replica-0", pid=1, incarnation="inc-0")
    rows += _full_stream_rows(at, replica="replica-1", pid=2, incarnation="inc-1")
    members = {
        "replica-0": {"pid": 1, "start_ticks": 1},
        "replica-1": {"pid": 2, "start_ticks": 1},
    }
    evidence = control.baseline(members, rows, "rev0", at)
    assert evidence["session_hash"] == "1" * 12
    assert evidence["entra_hash"] == "2" * 12
    assert evidence["revision"] == "rev0"
    assert sorted(evidence["processes"]) == [
        ["rev0", "replica-0", 1, "inc-0"],
        ["rev0", "replica-1", 2, "inc-1"],
    ]


# ---------------------------------------------------------------------------
# job_events(): partial/malformed/missing job-log evidence must never be trusted
# ---------------------------------------------------------------------------


def _job_row(**overrides):
    row = {
        "stage": "rotated",
        "at": "2026-01-01T00:00:00.000000Z",
        "version_hash": "1" * 12,
        "created_on": "2026-01-01T00:00:00.000000Z",
    }
    row.update(overrides)
    return row


def test_job_events_rejects_unknown_stage(control, monkeypatch):
    monkeypatch.setattr(control, "query_logs", lambda q: [_job_row(stage="not-a-real-stage")])
    with pytest.raises(control.ControlError) as excinfo:
        control.job_events(str(uuid4()))
    assert excinfo.value.args[0] == "job_event_invalid"


def test_job_events_rejects_naive_at_timestamp(control, monkeypatch):
    monkeypatch.setattr(control, "query_logs", lambda q: [_job_row(at="2026-01-01T00:00:00")])
    with pytest.raises(control.ControlError) as excinfo:
        control.job_events(str(uuid4()))
    assert excinfo.value.args[0] == "timestamp_invalid"


def test_job_events_rejects_malformed_version_hash(control, monkeypatch):
    monkeypatch.setattr(control, "query_logs", lambda q: [_job_row(version_hash="not-hex")])
    with pytest.raises(control.ControlError) as excinfo:
        control.job_events(str(uuid4()))
    assert excinfo.value.args[0] == "job_event_invalid"


def test_job_events_rejects_malformed_created_on(control, monkeypatch):
    monkeypatch.setattr(
        control, "query_logs", lambda q: [_job_row(created_on="2026-01-01T00:00:00")]
    )
    with pytest.raises(control.ControlError) as excinfo:
        control.job_events(str(uuid4()))
    assert excinfo.value.args[0] == "timestamp_invalid"


def test_job_events_accepts_well_formed_rows(control, monkeypatch):
    monkeypatch.setattr(control, "query_logs", lambda q: [_job_row(), _job_row(created_on="")])
    rows = control.job_events(str(uuid4()))
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# observe(): the live observation loop -- approval gating, configuration/execution
# turnover rejection, stop-confirmation, and watchdog-timeout escalation. Every
# Azure-touching seam (`app_config`/`inventory`/`observations`/`job_events`/
# `executions`/`azure`) is faked; `measure()` itself runs for real so a genuine
# worker-turnover rejection is exercised end-to-end, not just asserted in isolation.
# ---------------------------------------------------------------------------


def _observe_state(**overrides):
    started = datetime.now(UTC)
    config = {"revision": "rev0", "container": "web-nat-dev", "fingerprint": "f0"}
    state = {
        "run_id": str(uuid4()),
        "phase": "running",
        "execution": "exec-0",
        "config": config,
        "members": {"replica-0": {"pid": 1, "start_ticks": 1}},
        "started_at": _iso(started),
        "baseline": {
            "session_hash": "1" * 12,
            "entra_hash": "2" * 12,
            "at": _iso(started),
            "processes": [["rev0", "replica-0", 1, "inc-0"]],
            "revision": "rev0",
        },
    }
    state.update(overrides)
    return state


def _wire_observe_fakes(
    control, monkeypatch, *, members=None, executions_seq=None, measure_result=False
):
    state_config = {"revision": "rev0", "container": "web-nat-dev", "fingerprint": "f0"}
    monkeypatch.setattr(control, "app_config", lambda: state_config)
    monkeypatch.setattr(
        control, "inventory", lambda config: members or {"replica-0": {"pid": 1, "start_ticks": 1}}
    )
    monkeypatch.setattr(control, "observations", lambda revision: [])
    monkeypatch.setattr(control, "job_events", lambda run_id: [])
    monkeypatch.setattr(control, "save", lambda state: None)
    exec_iter = iter(executions_seq or [[{"name": "exec-0", "status": "Running"}]])
    monkeypatch.setattr(control, "executions", lambda: next(exec_iter))
    monkeypatch.setattr(control, "measure", lambda *a, **k: measure_result)
    monkeypatch.setattr(control.time, "sleep", lambda seconds: None)


def test_observe_rejects_without_approval(control):
    state = _observe_state()
    with pytest.raises(control.ControlError) as excinfo:
        control.observe(state, False)
    assert excinfo.value.args[0] == "observer_stop_approval_required"


def test_observe_rejects_when_phase_is_not_running(control):
    state = _observe_state(phase="provisioned")
    with pytest.raises(control.ControlError) as excinfo:
        control.observe(state, True)
    assert excinfo.value.args[0] == "execution_not_running"


def test_observe_rejects_on_app_configuration_turnover(control, monkeypatch):
    state = _observe_state()
    monkeypatch.setattr(control, "app_config", lambda: {**state["config"], "fingerprint": "drift"})
    with pytest.raises(control.ControlError) as excinfo:
        control.observe(state, True)
    assert excinfo.value.args[0] == "app_configuration_changed"


def test_observe_rejects_on_execution_membership_turnover(control, monkeypatch):
    state = _observe_state()
    _wire_observe_fakes(
        control, monkeypatch, executions_seq=[[{"name": "exec-1", "status": "Running"}]]
    )
    with pytest.raises(control.ControlError) as excinfo:
        control.observe(state, True)
    assert excinfo.value.args[0] == "execution_membership_changed"


def test_observe_propagates_real_member_turnover_rejection_from_measure(control, monkeypatch):
    """`measure()` runs unmocked here: a worker inventory that no longer matches the
    proven baseline (turnover) must be rejected end-to-end through observe(), not
    just when calling measure() directly."""
    state = _observe_state()
    turned_over_members = {"replica-0": {"pid": 999, "start_ticks": 1}}
    monkeypatch.setattr(control, "app_config", lambda: state["config"])
    monkeypatch.setattr(control, "inventory", lambda config: turned_over_members)
    monkeypatch.setattr(control, "observations", lambda revision: [])
    monkeypatch.setattr(control, "job_events", lambda run_id: [])
    monkeypatch.setattr(control, "save", lambda state: None)
    monkeypatch.setattr(control, "executions", lambda: [{"name": "exec-0", "status": "Running"}])
    monkeypatch.setattr(control.time, "sleep", lambda seconds: None)
    with pytest.raises(control.ControlError) as excinfo:
        control.observe(state, True)
    assert excinfo.value.args[0] == "baseline_member_disappeared_or_restarted"


def test_observe_stops_job_and_reaches_accepted_stopped_when_proof_completes(control, monkeypatch):
    state = _observe_state()
    _wire_observe_fakes(
        control,
        monkeypatch,
        measure_result=True,
        executions_seq=[
            [{"name": "exec-0", "status": "Running"}],  # membership check
            [{"name": "exec-0", "status": "Stopped"}],  # post-stop confirmation poll
        ],
    )
    stop_calls = []

    def fake_azure(args):
        stop_calls.append(args)
        return None

    monkeypatch.setattr(control, "azure", fake_azure)
    control.observe(state, True)
    assert state["phase"] == "accepted_stopped"
    assert any(args[:3] == ["containerapp", "job", "stop"] for args in stop_calls)


def test_observe_raises_watchdog_when_stop_never_confirmed(control, monkeypatch):
    state = _observe_state()
    # Membership check succeeds once, then every subsequent executions() poll
    # (12 of them) keeps reporting "Running" -- the stop is never confirmed.
    running = [{"name": "exec-0", "status": "Running"}]
    _wire_observe_fakes(
        control, monkeypatch, measure_result=True, executions_seq=[running] + [running] * 20
    )
    monkeypatch.setattr(control, "azure", lambda args: None)
    with pytest.raises(control.ControlError) as excinfo:
        control.observe(state, True)
    assert excinfo.value.args[0] == "stop_unconfirmed_watchdog_may_recover"


def test_observe_loops_until_finished_then_stops(control, monkeypatch):
    state = _observe_state()
    results = iter([False, False, True])
    running_polls = [[{"name": "exec-0", "status": "Running"}] for _ in range(3)]
    stopped_poll = [{"name": "exec-0", "status": "Stopped"}]
    exec_iter = iter(running_polls + [stopped_poll])
    monkeypatch.setattr(control, "app_config", lambda: state["config"])
    monkeypatch.setattr(
        control, "inventory", lambda config: {"replica-0": {"pid": 1, "start_ticks": 1}}
    )
    monkeypatch.setattr(control, "observations", lambda revision: [])
    monkeypatch.setattr(control, "job_events", lambda run_id: [])
    saved_phases = []
    monkeypatch.setattr(control, "save", lambda st: saved_phases.append(st["phase"]))
    monkeypatch.setattr(control, "executions", lambda: next(exec_iter))
    monkeypatch.setattr(control, "measure", lambda *a, **k: next(results))
    monkeypatch.setattr(control, "azure", lambda args: None)
    sleeps = []
    monkeypatch.setattr(control.time, "sleep", lambda seconds: sleeps.append(seconds))
    control.observe(state, True)
    assert state["phase"] == "accepted_stopped"
    # Two non-finished iterations must each sleep(10) before looping again.
    assert sleeps.count(10) == 2


def test_observe_rejects_clock_step_between_wall_and_monotonic(control, monkeypatch):
    state = _observe_state()
    real_now = control.now
    calls = {"n": 0}

    def stepping_now():
        calls["n"] += 1
        # The very first call establishes start_wall; the second (inside the loop's
        # own consistency check) is deliberately skewed far past any real scheduling
        # jitter, simulating a wall-clock step unrelated to monotonic time.
        if calls["n"] == 1:
            return real_now()
        return real_now() + timedelta(seconds=30)

    monkeypatch.setattr(control, "now", stepping_now)
    with pytest.raises(control.ControlError) as excinfo:
        control.observe(state, True)
    assert excinfo.value.args[0] == "controller_clock_step"


# ---------------------------------------------------------------------------
# No accidental default wiring into azure.yaml / infra/main.bicep / deploy.sh
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("relative", ["azure.yaml", "deploy.sh", "infra/main.bicep"])
def test_default_deploy_flows_do_not_reference_session_rotation(relative):
    text = (ROOT / relative).read_text()
    assert "session-rotation" not in text
    assert "session_rotation" not in text


def test_session_rotation_bicep_is_gated_behind_disabled_by_default_flag():
    text = (ROOT / "infra/session-rotation.bicep").read_text()
    assert "param enableSessionRotation bool = false" in text


def test_session_rotation_bicep_uses_canonical_role_id_without_changing_guids():
    text = (ROOT / "infra/modules/session-rotation-runner.bicep").read_text()
    assert (
        "var sessionRoleDefinitionId = subscriptionResourceId("
        "'Microsoft.Authorization/roleDefinitions', sessionRoleGuid)"
    ) in text
    assert "roleDefinitionId: sessionRoleDefinitionId" in text
    assert ("name: guid(vault.id, 'app-session-secret-key', identity.id, sessionRole.id)") in text
    assert "name: guid(registry.id, identity.id, acrPullRoleId)" in text
    assert "assignableScopes: [resourceGroup().id]" in text


# ---------------------------------------------------------------------------
# Offline Bicep build: top-level template and module compile cleanly
# ---------------------------------------------------------------------------


def _bicep_available():
    import shutil

    return shutil.which("az") is not None


@pytest.mark.skipif(
    not _bicep_available(), reason="az CLI with the bicep extension is not installed"
)
@pytest.mark.parametrize(
    "relative",
    ["infra/session-rotation.bicep", "infra/modules/session-rotation-runner.bicep"],
)
def test_bicep_builds_offline(relative):
    import subprocess

    result = subprocess.run(
        ["az", "bicep", "build", "--file", str(ROOT / relative), "--stdout"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    compiled = json.loads(result.stdout)
    assert compiled["resources"] or compiled.get("resources") == {}
