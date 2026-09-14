"""Dev-only external controller. Azure mutation and value access are separate boundaries."""

import argparse
import base64
import fcntl
import hashlib
import importlib.util
import json
import logging
import os
import pty
import re
import select
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid5

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.rotation_observability import validate_envelope  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "session_harness",
    ROOT / "scripts/session_rotation/harness.py",
)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)

SUBSCRIPTION = "b8ff3e15-7e2d-4fac-a773-992fb59ccedd"
GROUP = "rg-fcag-dev"
APP = "fcag-dev-app"
JOB = "fcag-dev-session-rotation"
LOCATION = "eastus2"
RG_ID = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}"
PREFIX = RG_ID + "/providers/"
IDENTITY = PREFIX + f"Microsoft.ManagedIdentity/userAssignedIdentities/{JOB}-id"
JOB_ID = PREFIX + f"Microsoft.App/jobs/{JOB}"
REGISTRY = PREFIX + "Microsoft.ContainerRegistry/registries/fcagdevqhg3qc4rlbt4gacr"
VAULT = PREFIX + "Microsoft.KeyVault/vaults/kvfcagdevqhg3qc4rlbt4g"
SECRET_ID = VAULT + "/secrets/app-session-secret-key"
IMAGE = (
    "fcagdevqhg3qc4rlbt4gacr.azurecr.io/fantasy-cards-generator/web-nat-dev"
    "@sha256:bb7c5c4e49b9f3860d0f5aca5ccf2ff66e43921f512726551de7fc8c60ee8a11"
)
IMAGE_REGISTRY, IMAGE_REMAINDER = IMAGE.split("/", 1)
IMAGE_REPOSITORY, IMAGE_DIGEST = IMAGE_REMAINDER.split("@", 1)
ACR_NAME = IMAGE_REGISTRY.split(".", 1)[0]
AZD_TAG = re.compile(r"azd-deploy-[1-9][0-9]*")
ARM_NAMESPACE = UUID("11fb06fb-712d-4ddd-98c7-e71bbd588830")
ROLE = f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Authorization/roleDefinitions/" + str(
    uuid5(ARM_NAMESPACE, RG_ID + "-private-session-rotation-v1")
)
ROLE_RESOURCE = (
    PREFIX
    + "Microsoft.Authorization/roleDefinitions/"
    + str(uuid5(ARM_NAMESPACE, RG_ID + "-private-session-rotation-v1"))
)
ACR_ROLE = (
    f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Authorization/roleDefinitions/"
    "7f951dda-4ed3-4680-a7ca-43fe172d538d"
)
ASSIGNMENT = (
    SECRET_ID
    + "/providers/Microsoft.Authorization/roleAssignments/"
    + str(
        uuid5(
            ARM_NAMESPACE,
            "-".join((VAULT, "app-session-secret-key", IDENTITY, ROLE_RESOURCE)),
        )
    )
)
ACR_ASSIGNMENT = (
    REGISTRY
    + "/providers/Microsoft.Authorization/roleAssignments/"
    + str(uuid5(ARM_NAMESPACE, "-".join((REGISTRY, IDENTITY, ACR_ROLE))))
)
# Exact ARM error codes for "this resource type does not exist", used only to make
# cleanup()'s per-resource revocation resumable after a partial/interrupted attempt.
ASSIGNMENT_NOT_FOUND = "RoleAssignmentNotFound"
ROLE_NOT_FOUND = "RoleDefinitionDoesNotExist"
RESOURCE_NOT_FOUND = "ResourceNotFound"
DATA_ACTIONS = [
    "Microsoft.KeyVault/vaults/secrets/getSecret/action",
    "Microsoft.KeyVault/vaults/secrets/setSecret/action",
    "Microsoft.KeyVault/vaults/secrets/readMetadata/action",
]
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
MARGIN = harness.CLOCK_MARGIN
TERMINAL = {"Succeeded", "Failed", "Stopped"}
ACTIONS = tuple(
    "session-" + name for name in ("preview", "provision", "run", "observe", "recover", "cleanup")
)


class ControlError(Exception):
    pass


def require(condition, code):
    if not condition:
        raise ControlError(code)


def same_id(left, right):
    return isinstance(left, str) and isinstance(right, str) and left.casefold() == right.casefold()


def same_location(value, expected):
    """Compare an ARM `location` value against an expected compact region name.

    ARM APIs are inconsistent about location representation: some return the
    compact form (e.g. "eastus2"), others the display form (e.g. "East US 2").
    Only whitespace and case differences are treated as equivalent; any other
    difference (a different region, punctuation, or a non-string value) fails
    closed. This intentionally does not perform any broader region name
    normalization (no hyphen/underscore folding, no alias tables).
    """
    if not isinstance(value, str) or not isinstance(expected, str):
        return False
    normalized = "".join(value.split()).casefold()
    return normalized == expected.casefold()


def now():
    return datetime.now(UTC)


def date(value):
    require(isinstance(value, str), "timestamp_invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.tzinfo is not None, "timestamp_invalid")
    return parsed


def safe_id(value):
    require(isinstance(value, str) and ID_PATTERN.fullmatch(value), "identifier_invalid")
    return value


def _run_az(args):
    try:
        return subprocess.run(
            ["az", *args, "--subscription", SUBSCRIPTION, "--only-show-errors", "-o", "json"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ControlError("azure_transport_failed") from None


def _parse_json_result(result):
    require(result.returncode == 0, "azure_command_failed")
    try:
        return json.loads(result.stdout) if result.stdout.strip() else None
    except ValueError:
        raise ControlError("azure_response_invalid") from None


def azure(args):
    return _parse_json_result(_run_az(args))


# az CLI (Knack) wraps every emitted line in optional ANSI SGR codes when stderr is a
# tty; when it is not (our case: subprocess.run with capture_output=True always pipes,
# never a tty) no color is emitted and a plain "ERROR: " level prefix is used instead.
# Verified against installed azure-cli 2.74.0 (knack._CustomStreamHandler.format /
# knack.cli.CLI._should_enable_color): a piped stderr is never a tty, so color is
# never enabled for this module's real invocations; the color branch below is kept
# only as defense in depth in case that invariant ever changes upstream.
_ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")
_ERROR_PREFIX = "ERROR: "


def _last_nonblank_line(text):
    # az CLI's error is always the single trailing emitted line; taking only that
    # line (rather than scanning the whole blob) means stray/earlier chatter can
    # never be mistaken for the structured error we're looking for.
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _parse_az_rest_not_found(stderr_text, not_found_code):
    """Return True only if `stderr_text` is exactly az CLI's rendering of
    `send_raw_request`'s `HTTPError(reason + '(' + r.text + ')')` for an HTTP 404
    whose JSON body is `{"error": {"code": <not_found_code>, ...}}`.

    This is a narrow, exact-shape parse, not a substring/regex search over the
    message: the ARM error code must appear as the JSON `error.code` field of the
    single trailing stderr line, and the HTTP reason phrase preceding it must be
    "Not Found". Any deviation — wrong/missing code, code merely mentioned inside
    `message` text, non-404 reason, malformed/missing JSON body, extra surrounding
    chatter, or a transport/auth failure that never reached this shape — returns
    False so the caller fails closed (`azure_command_failed`), same as any other
    unexpected failure.
    """
    line = _ANSI_SGR_RE.sub("", _last_nonblank_line(stderr_text)).strip()
    if line.startswith(_ERROR_PREFIX):
        line = line[len(_ERROR_PREFIX) :]
    if not line.endswith(")"):
        return False
    paren = line.find("(")
    if paren <= 0:
        return False
    reason, body_text = line[:paren].strip(), line[paren + 1 : -1]
    if reason.casefold() != "not found":
        return False
    try:
        body = json.loads(body_text)
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    error = body.get("error")
    if not isinstance(error, dict):
        return False
    code = error.get("code")
    expected = {not_found_code} if isinstance(not_found_code, str) else set(not_found_code)
    return isinstance(code, str) and code in expected


def rest(method, resource_id, api, body=None, not_found_code=None):
    args = [
        "rest",
        "--method",
        method,
        "--url",
        f"https://management.azure.com{resource_id}?api-version={api}",
    ]
    if body is not None:
        args += ["--body", json.dumps(body)]
    if not_found_code is None:
        return azure(args)
    # Idempotent-probe path used only by cleanup()'s resumable revocation: a prior,
    # interrupted cleanup attempt may already have deleted this exact resource. Only
    # the single, documented ARM error code for "this resource type does not exist"
    # is ever treated as already-clean; any other failure (auth, network, throttling,
    # an unexpected/malformed error body) still raises azure_command_failed /
    # azure_transport_failed like every other call in this module. Note `-o json`
    # (added by `_run_az`) only affects stdout formatting on success; on failure az
    # writes a human-oriented "ERROR: <reason>(<body>)" line to stderr regardless, so
    # the body must be recovered from that rendering rather than from `-o json`.
    result = _run_az(args)
    if result.returncode != 0:
        require(_parse_az_rest_not_found(result.stderr, not_found_code), "azure_command_failed")
        return None
    return _parse_json_result(result)


def state_path(run_id):
    git_dir = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--absolute-git-dir"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return Path(git_dir) / f"session-rotation-{str(UUID(run_id))}.json"


def save(state):
    path = state_path(state["run_id"])
    # Only controller-built, allowlisted metadata is saved; never HTTP/SDK responses.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags, 0o600), "w") as handle:
        json.dump(state, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())


def load(run_id):
    path = state_path(run_id)
    require(not path.is_symlink(), "state_symlink")
    state = json.loads(path.read_text())
    require(state.get("schema") == 1 and state.get("run_id") == run_id, "state_invalid")
    require(state.get("source_hash") == source_hash(), "reviewed_source_changed")
    require(isinstance(state.get("provision_attempted"), bool), "state_invalid")
    return state


def source_hash():
    names = (
        "scripts/session_rotation/harness.py",
        "scripts/session_rotation/control.py",
        "scripts/session_rotation/process_inventory.py",
        "infra/session-rotation.bicep",
        "infra/modules/session-rotation-runner.bicep",
    )
    return hashlib.sha256(b"".join((ROOT / name).read_bytes() for name in names)).hexdigest()


def deployment(run_id, expected, identity_principal=""):
    args = [
        "--resource-group",
        GROUP,
        "--name",
        "session-rotation-bootstrap",
        "--template-file",
        str(ROOT / "infra/session-rotation.bicep"),
        "--mode",
        "Incremental",
        "--parameters",
        "environmentName=dev",
        "enableSessionRotation=true",
        "sessionRunId=" + run_id,
        "sessionExpectedHash=" + expected,
    ]
    if identity_principal:
        args.append("existingIdentityPrincipalId=" + identity_principal)
    return args


def preview(run_id, expected, present=(), identity_principal=""):
    result = azure(
        [
            "deployment",
            "group",
            "what-if",
            *deployment(run_id, expected, identity_principal),
            "--no-pretty-print",
            "--result-format",
            "FullResourcePayloads",
            "--exclude-change-types",
            "Ignore",
        ]
    )
    require(
        result.get("status") == "Succeeded" and not result.get("diagnostics"),
        "preview_failed_or_diagnostics",
    )
    expected_ids = {
        item.casefold() for item in (JOB_ID, IDENTITY, ROLE_RESOURCE, ASSIGNMENT, ACR_ASSIGNMENT)
    }
    present_ids = {item.casefold() for item in present}
    require(present_ids <= expected_ids, "preview_extra_scope")
    actual = {}
    for change in result.get("changes", []):
        rid = change.get("resourceId", "").casefold()
        require(rid in expected_ids, "preview_extra_scope")
        require(rid not in actual, "preview_duplicate_resource")
        change_type = change.get("changeType")
        require(change_type in ("Create", "NoChange"), "preview_not_create_or_nochange")
        actual[rid] = change_type
    require(set(actual) == expected_ids, "preview_not_exact_five")
    require(
        all(
            actual[rid] == ("NoChange" if rid in present_ids else "Create") for rid in expected_ids
        ),
        "preview_reconciliation_mismatch",
    )
    creates = sum(change == "Create" for change in actual.values())
    return {"creates": creates, "modifies": 0, "deletes": 0, "diagnostics": 0}


def app_image(image, revision_created):
    if image == IMAGE:
        return {"reference": image, "digest": IMAGE_DIGEST, "kind": "digest"}
    require(isinstance(image, str), "worker_image_or_command_drift")
    match = re.fullmatch(r"([^/]+)/([^@:]+(?:/[^@:]+)*):([^/:@]+)", image)
    require(bool(match), "worker_image_or_command_drift")
    registry, repository, tag = match.groups()
    require(
        registry == IMAGE_REGISTRY
        and repository == IMAGE_REPOSITORY
        and bool(AZD_TAG.fullmatch(tag)),
        "worker_image_or_command_drift",
    )
    result = azure(
        [
            "acr",
            "repository",
            "show-tags",
            "--name",
            ACR_NAME,
            "--repository",
            IMAGE_REPOSITORY,
            "--detail",
            "--query",
            (
                f"[?name=='{tag}'].{{name:name,digest:digest,createdTime:createdTime,"
                "lastUpdateTime:lastUpdateTime,changeableAttributes:changeableAttributes}"
            ),
        ]
    )
    require(isinstance(result, list) and len(result) == 1, "worker_image_metadata_invalid")
    item = result[0]
    require(
        isinstance(item, dict)
        and item.get("name") == tag
        and item.get("digest") == IMAGE_DIGEST
        and item.get("changeableAttributes")
        == {
            "deleteEnabled": False,
            "listEnabled": True,
            "readEnabled": True,
            "writeEnabled": False,
        },
        "worker_image_or_command_drift",
    )
    created = date(item.get("createdTime"))
    updated = date(item.get("lastUpdateTime"))
    require(created <= updated <= revision_created, "worker_image_or_command_drift")
    return {
        "reference": image,
        "digest": item["digest"],
        "kind": "locked_azd_tag",
        "tag_created": harness.stamp(created),
        "tag_locked": harness.stamp(updated),
    }


def app_config():
    # Project env names + secret references, not general values or secret configuration.
    result = azure(
        [
            "containerapp",
            "show",
            "-g",
            GROUP,
            "-n",
            APP,
            "--query",
            "{revision:properties.latestReadyRevisionName,"
            "latest:properties.latestRevisionName,mode:properties.configuration.activeRevisionsMode,"
            "fqdn:properties.configuration.ingress.fqdn,traffic:properties.configuration.ingress.traffic,"
            "provision:properties.provisioningState,running:properties.runningStatus,"
            "containers:properties.template.containers[].{name:name,image:image,command:command,args:args,"
            "env:env[].{name:name,secretRef:secretRef},"
            "settings:env[?name=='SESSION_SIGNING_KEY_OVERLAP_SECONDS' || "
            "name=='SECRET_PROVIDER_BACKEND' || name=='AGENT_GENERATION_ENABLED' || "
            "name=='WEB_CONCURRENCY'].{name:name,value:value}},scale:properties.template.scale}",
        ]
    )
    require(
        isinstance(result, dict)
        and result.get("provision") == "Succeeded"
        and result.get("running") == "Running"
        and result.get("revision") == result.get("latest")
        and result.get("mode") == "Single",
        "app_not_stable",
    )
    safe_id(result["revision"])
    require(
        re.fullmatch(r"fcag-dev-app\.[a-z0-9.-]+\.azurecontainerapps\.io", result["fqdn"]),
        "app_host_invalid",
    )
    containers = result["containers"]
    require(len(containers) == 1, "container_count_unknown")
    container = containers[0]
    safe_id(container["name"])
    revision = azure(
        [
            "containerapp",
            "revision",
            "show",
            "-g",
            GROUP,
            "-n",
            APP,
            "--revision",
            result["revision"],
            "--query",
            "{name:name,created:properties.createdTime,active:properties.active,"
            "health:properties.healthState,provision:properties.provisioningState,"
            "containers:properties.template.containers[]."
            "{name:name,image:image,command:command,args:args}}",
        ]
    )
    require(
        isinstance(revision, dict)
        and revision.get("name") == result["revision"]
        and revision.get("active") is True
        and revision.get("health") == "Healthy"
        and revision.get("provision") == "Provisioned"
        and revision.get("containers")
        == [
            {
                "name": container["name"],
                "image": container["image"],
                "command": container["command"],
                "args": container["args"],
            }
        ],
        "revision_configuration_drift",
    )
    revision_created = date(revision.get("created"))
    require(
        not container["command"] and not container["args"],
        "worker_image_or_command_drift",
    )
    image = app_image(container["image"], revision_created)
    settings = {v["name"]: v["value"] for v in container["settings"]}
    require(settings.get("SECRET_PROVIDER_BACKEND") == "azure", "provider_not_azure")
    require(
        settings.get("SESSION_SIGNING_KEY_OVERLAP_SECONDS", "3600") == "3600", "overlap_not_3600"
    )
    require("WEB_CONCURRENCY" not in settings, "worker_count_unknown")
    agent_flag = settings.get("AGENT_GENERATION_ENABLED")
    require(
        isinstance(agent_flag, str) and agent_flag.casefold() in ("true", "false"),
        "agent_flag_unknown",
    )
    # Persist only a fingerprint of configuration, plus needed safe identifiers.
    fingerprint = hashlib.sha256(
        json.dumps({"app": result, "revision": revision, "image": image}, sort_keys=True).encode()
    ).hexdigest()
    return {
        "fingerprint": fingerprint,
        "revision": result["revision"],
        "container": container["name"],
        "host": result["fqdn"],
        "image": image,
    }


def process_inventory(revision, replica, container):
    for value in (revision, replica, container):
        safe_id(value)
    source = (ROOT / "scripts/session_rotation/process_inventory.py").read_text()
    encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
    command = (
        "/app/.venv/bin/python -I -c "
        + "\"import base64;exec(base64.b64decode('"
        + encoded
        + "'))\""
    )
    args = [
        "az",
        "containerapp",
        "exec",
        "-g",
        GROUP,
        "-n",
        APP,
        "--subscription",
        SUBSCRIPTION,
        "--revision",
        revision,
        "--replica",
        replica,
        "--container",
        container,
        "--command",
        command,
        "--only-show-errors",
    ]
    master, slave = pty.openpty()
    process = None
    data = bytearray()
    connected_at = None
    connected_line = (
        f"INFO: Successfully connected to container: '{container}'. "
        f"[ Revision: '{revision}', Replica: '{replica}']."
    ).encode()
    deadline = time.monotonic() + 20
    try:
        process = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        slave = None
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.25)
            if ready:
                received_at = now()
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                data.extend(chunk)
                require(len(data) <= 65536, "process_inventory_output_limit")
                if connected_at is None and connected_line in data.replace(b"\r", b""):
                    connected_at = received_at
            if process.poll() is not None and not ready:
                break
        require(process.poll() == 0, "process_inventory_failed")
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        os.close(master)
        if slave is not None:
            os.close(slave)
    matches = re.findall(rb"(?:^|\n)SESSION_PROCESS_INVENTORY=(\{[^\r\n]+\})", data)
    require(len(matches) == 1, "process_inventory_missing")
    result = json.loads(matches[0])
    require(
        result.get("schema") == 1
        and result.get("unknown") == 0
        and len(result.get("workers", [])) == 1,
        "worker_coverage_unknown",
    )
    worker = result["workers"][0]
    require(
        type(worker["pid"]) is int
        and worker["pid"] > 0
        and type(worker["start_ticks"]) is int
        and worker["start_ticks"] > 0,
        "process_inventory_invalid",
    )
    measured = date(result["at"])
    # Only remote-command exchange, not websocket setup, consumes the clock margin.
    ended = now()
    require(
        connected_at is not None
        and (ended - connected_at).total_seconds() <= MARGIN
        and connected_at - timedelta(seconds=MARGIN) <= measured
        and measured <= ended + timedelta(seconds=MARGIN),
        "worker_clock_bound_unproved",
    )
    return worker


def inventory(config):
    revisions = azure(
        [
            "containerapp",
            "revision",
            "list",
            "-g",
            GROUP,
            "-n",
            APP,
            "--query",
            "[?properties.active].{name:name,health:properties.healthState}",
        ]
    )
    require(
        revisions == [{"name": config["revision"], "health": "Healthy"}],
        "revision_membership_changed",
    )
    replicas = azure(
        [
            "containerapp",
            "replica",
            "list",
            "-g",
            GROUP,
            "-n",
            APP,
            "--revision",
            config["revision"],
            "--query",
            "[].{name:name,containers:properties.containers[]."
            "{name:name,restarts:restartCount,ready:ready,running:runningState}}",
        ]
    )
    require(bool(replicas), "replicas_missing")
    members = {}
    for replica in replicas:
        name = safe_id(replica["name"])
        containers = replica["containers"]
        require(
            len(containers) == 1 and containers[0]["name"] == config["container"],
            "container_inventory_changed",
        )
        c = containers[0]
        require(
            c["ready"] is True
            and c["running"] == "Running"
            and type(c["restarts"]) is int
            and c["restarts"] >= 0,
            "replica_unhealthy",
        )
        worker = process_inventory(config["revision"], name, c["name"])
        members[name] = {**worker, "restarts": c["restarts"]}
    return members


def workspace():
    customer = azure(
        [
            "containerapp",
            "env",
            "show",
            "-g",
            GROUP,
            "-n",
            "fcag-dev-cae",
            "--query",
            "properties.appLogsConfiguration.logAnalyticsConfiguration." "customerId",
        ]
    )
    return str(UUID(customer))


def query_logs(query):
    # Azure CLI credential captures the access token internally; never az token output.
    import httpx
    from azure.identity import AzureCliCredential

    with AzureCliCredential() as credential:
        token = credential.get_token("https://api.loganalytics.io/.default").token
        with httpx.Client(timeout=20, trust_env=False) as client:
            response = client.post(
                f"https://api.loganalytics.azure.com/v1/workspaces/{workspace()}/query",
                headers={"Authorization": "Bearer " + token},
                json={"query": query, "timespan": "PT2H"},
            )
    require(response.status_code == 200, "observability_unavailable")
    data = response.json()
    require(not data.get("error") and len(data.get("tables", [])) == 1, "logs_incomplete")
    table = data["tables"][0]
    require(len(table["rows"]) < 10000, "logs_truncated")
    names = [c["name"] for c in table["columns"]]
    return [dict(zip(names, row, strict=True)) for row in table["rows"]]


def observations(revision):
    safe_id(revision)
    fields = (
        "event",
        "schema_version",
        "observed_at",
        "sequence",
        "pid",
        "incarnation",
        "revision",
        "replica",
        "logical_secret",
        "source",
        "stage",
        "result",
        "version_hash",
        "error_category",
    )
    ints = {"schema_version", "sequence", "pid"}
    project = ",".join(f"{k}={'tolong' if k in ints else 'tostring'}(o.{k})" for k in fields)
    rows = query_logs(
        "ContainerAppConsoleLogs_CL | where TimeGenerated > ago(2h) "
        f"| where ContainerAppName_s == '{APP}' and RevisionName_s == '{revision}' "
        "| where Log_s startswith '{' | extend o=parse_json(Log_s) "
        "| where o.event == 'secret.rotation_observation' and toint(o.schema_version)==1 "
        f"| project {project} | take 10000"
    )
    valid = []
    for row in rows:
        observed_at = row.get("observed_at")
        if isinstance(observed_at, str) and re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}0Z",
            observed_at,
        ):
            row = {**row, "observed_at": observed_at[:-2] + "Z"}
        item = validate_envelope(row)
        require(item is not None, "observation_invalid")
        require(
            item["revision"] == revision and item["replica"] != "unknown",
            "observation_source_unproved",
        )
        valid.append(item)
    return valid


def key(row):
    return (row["revision"], row["replica"], row["pid"], row["incarnation"])


STREAMS = {("session", "provider"), ("entra", "provider"), ("entra", "entra_binding")}


def baseline(members, rows, revision, at):
    recent = [r for r in rows if at - timedelta(seconds=90) <= date(r["observed_at"]) <= at]
    processes = []
    session_hashes, entra_hashes = set(), set()
    for replica, worker in members.items():
        own = [r for r in recent if r["replica"] == replica and r["pid"] == worker["pid"]]
        require(len({key(r) for r in own}) == 1, "baseline_process_missing_or_restarted")
        require(
            {(r["logical_secret"], r["stage"]) for r in own} == STREAMS, "baseline_stream_missing"
        )
        for r in own:
            require(
                r["source"] == "azure"
                and r["result"] in ("adopted", "unchanged")
                and r["error_category"] == "none"
                and bool(harness.HASH.fullmatch(r["version_hash"])),
                "baseline_unhealthy",
            )
            (session_hashes if r["logical_secret"] == "session" else entra_hashes).add(
                r["version_hash"],
            )
        require(
            any(r["logical_secret"] == "session" and r["result"] == "unchanged" for r in own),
            "baseline_unchanged_missing",
        )
        processes.append(list(key(own[0])))
    require(len(session_hashes) == len(entra_hashes) == 1, "baseline_version_unstable")
    require(
        all(r["replica"] in members and r["pid"] == members[r["replica"]]["pid"] for r in recent),
        "unaccounted_worker",
    )
    return {
        "processes": processes,
        "session_hash": session_hashes.pop(),
        "entra_hash": entra_hashes.pop(),
        "at": harness.stamp(at),
        "revision": revision,
    }


def job_events(run_id):
    rows = query_logs(
        "ContainerAppConsoleLogs_CL | where TimeGenerated > ago(2h) "
        f"| where ContainerJobName_s == '{JOB}' "
        "| where Log_s startswith '{' | extend o=parse_json(Log_s) "
        f"| where o.schema == '{harness.SCHEMA}' and o.run_id == '{str(UUID(run_id))}' "
        "| project stage=tostring(o.stage),at=tostring(o.at),"
        "version_hash=tostring(o.version_hash),created_on=tostring(o.created_on) | take 10000"
    )
    for row in rows:
        require(row["stage"] in harness.STAGES, "job_event_invalid")
        date(row["at"])
        require(
            not row["version_hash"] or harness.HASH.fullmatch(row["version_hash"]),
            "job_event_invalid",
        )
        if row["created_on"]:
            date(row["created_on"])
    return rows


def executions():
    return azure(
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
            "[].{name:name,status:properties.status}",
        ]
    )


def terminal_only(rows):
    require(all(row["status"] in TERMINAL for row in rows), "execution_active_or_unknown")


def canonical_role_definition_id(value, expected):
    require(isinstance(value, str), "role_definition_id_drift")
    if same_id(expected, ROLE):
        require(
            same_id(value, ROLE) or same_id(value, ROLE_RESOURCE),
            "role_definition_id_drift",
        )
        return ROLE
    require(same_id(expected, ACR_ROLE) and same_id(value, ACR_ROLE), "role_definition_id_drift")
    return ACR_ROLE


def validate_role_item(item):
    canonical_role_definition_id(item.get("id"), ROLE)
    role = item["properties"]
    require(
        role["type"] == "CustomRole"
        and role["roleName"] == f"{GROUP} temporary session rotation"
        and role["description"]
        == "Temporary session-only drill: get/set values and list version metadata."
        and len(role["assignableScopes"]) == 1
        and same_id(role["assignableScopes"][0], RG_ID)
        and role["permissions"]
        == [
            {
                "actions": [],
                "notActions": [],
                "dataActions": DATA_ACTIONS,
                "notDataActions": [],
            }
        ],
        "role_drift",
    )


def validate_role():
    validate_role_item(rest("get", ROLE, "2022-04-01"))


def validate_identity_item(item):
    require(
        same_id(item.get("id"), IDENTITY)
        and item.get("name") == JOB + "-id"
        and same_id(item.get("type"), "Microsoft.ManagedIdentity/userAssignedIdentities")
        and same_location(item.get("location", ""), LOCATION)
        and not item.get("tags")
        and isinstance(item.get("properties", {}).get("principalId"), str)
        and isinstance(item["properties"].get("clientId"), str),
        "identity_drift",
    )
    return item["properties"]


def validate_assignment_item(item, rid, principal, role, scope):
    p = item["properties"]
    require(
        same_id(item.get("id"), rid)
        and same_id(p["principalId"], principal)
        and canonical_role_definition_id(p["roleDefinitionId"], role) == role
        and same_id(p["scope"], scope)
        and not p.get("condition")
        and p.get("principalType") == "ServicePrincipal",
        "assignment_drift",
    )


def identity_assignment_ids(principal):
    rows = azure(
        [
            "role",
            "assignment",
            "list",
            "--assignee-object-id",
            principal,
            "--all",
            "--query",
            "[].id",
        ]
    )
    require(
        isinstance(rows, list) and all(isinstance(value, str) for value in rows),
        "identity_permissions_invalid",
    )
    return {value.casefold() for value in rows}


def validate_assignments(principal):
    validate_role()
    for rid, role, scope in ((ASSIGNMENT, ROLE, SECRET_ID), (ACR_ASSIGNMENT, ACR_ROLE, REGISTRY)):
        item = rest("get", rid, "2022-04-01")
        validate_assignment_item(item, rid, principal, role, scope)
    require(
        identity_assignment_ids(principal) == {ASSIGNMENT.casefold(), ACR_ASSIGNMENT.casefold()},
        "identity_extra_permissions",
    )


def validate_job_item(state, item, identity, allowed_provisioning_states=("Succeeded",)):
    assigned = item["identity"].get("userAssignedIdentities", {})
    require(
        same_id(item.get("id"), JOB_ID)
        and item.get("name") == JOB
        and same_location(item.get("location", ""), LOCATION)
        and same_id(item.get("type"), "Microsoft.App/jobs")
        and not item.get("tags")
        and item["identity"]["type"] == "UserAssigned"
        and len(assigned) == 1
        and same_id(next(iter(assigned)), IDENTITY),
        "job_identity_drift",
    )
    p = item["properties"]
    c = p["configuration"]
    require(
        p["provisioningState"] in allowed_provisioning_states
        and same_id(p["environmentId"], PREFIX + "Microsoft.App/managedEnvironments/fcag-dev-cae")
        and p["workloadProfileName"] == "Consumption"
        and c["triggerType"] == "Manual"
        and c["replicaTimeout"] == 4500
        and c["replicaRetryLimit"] == 0
        and c["manualTriggerConfig"] == {"parallelism": 1, "replicaCompletionCount": 1}
        and not any(
            c.get(k) for k in ("secrets", "scheduleTriggerConfig", "eventTriggerConfig", "ingress")
        ),
        "job_configuration_drift",
    )
    registries = c.get("registries", [])
    require(
        len(registries) == 1
        and registries[0]["server"] == IMAGE.split("/")[0]
        and same_id(registries[0].get("identity"), IDENTITY)
        and not registries[0].get("passwordSecretRef"),
        "job_registry_drift",
    )
    template = p["template"]
    require(
        not template.get("initContainers")
        and not template.get("volumes")
        and len(template["containers"]) == 1,
        "job_template_drift",
    )
    container = template["containers"][0]
    expected_env = [
        {"name": "RUNNER_CLIENT_ID", "value": identity["clientId"]},
        {"name": "SESSION_RUN_ID", "value": state["run_id"]},
        {"name": "SESSION_EXPECTED_HASH", "value": state["baseline"]["session_hash"]},
        {"name": "SESSION_APP_HOST", "value": state["config"]["host"]},
    ]
    require(
        container["name"] == "session-drill"
        and container["image"] == IMAGE
        and container["command"]
        == [
            "/app/.venv/bin/python",
            "-I",
            "-c",
            (ROOT / "scripts/session_rotation/harness.py").read_text(),
        ]
        and not container.get("args")
        and container["env"] == expected_env
        and container["resources"]["cpu"] == 0.25
        and container["resources"]["memory"] == "0.5Gi"
        and not container.get("volumeMounts"),
        "job_template_drift",
    )
    return identity["principalId"]


def validate_job_identity(state):
    """Validate the retained job/identity resources only (never deleted by cleanup())."""
    identity = azure(
        [
            "identity",
            "show",
            "-g",
            GROUP,
            "-n",
            JOB + "-id",
            "--query",
            "{clientId:clientId,principalId:principalId,id:id}",
        ]
    )
    require(same_id(identity["id"], IDENTITY), "identity_drift")
    return validate_job_item(state, rest("get", JOB_ID, "2024-03-01"), identity)


def validate_job(state):
    """Full pre-execution validation (session-run/-observe/-recover): the retained job
    resources plus a strict check that exactly the expected role assignments exist and
    nothing else. Not used by cleanup() — see validate_job_identity().
    """
    principal = validate_job_identity(state)
    validate_assignments(principal)
    return principal


EXPECTED_RESOURCE_IDS = (JOB_ID, IDENTITY, ROLE_RESOURCE, ASSIGNMENT, ACR_ASSIGNMENT)


def _probe(resource_id, api, not_found_code):
    return rest("get", resource_id, api, not_found_code=not_found_code)


def fresh_preview_context():
    identity_item = _probe(IDENTITY, "2023-01-31", RESOURCE_NOT_FOUND)
    require(_probe(ROLE, "2022-04-01", ROLE_NOT_FOUND) is None, "fresh_run_resources_exist")
    require(
        _probe(ASSIGNMENT, "2022-04-01", ASSIGNMENT_NOT_FOUND) is None,
        "fresh_run_resources_exist",
    )
    require(
        _probe(ACR_ASSIGNMENT, "2022-04-01", ASSIGNMENT_NOT_FOUND) is None,
        "fresh_run_resources_exist",
    )
    require(_probe(JOB_ID, "2024-03-01", RESOURCE_NOT_FOUND) is None, "fresh_run_resources_exist")
    if identity_item is None:
        return set(), ""
    identity = validate_identity_item(identity_item)
    require(not identity_assignment_ids(identity["principalId"]), "identity_extra_permissions")
    return {IDENTITY}, identity["principalId"]


def validate_current_baseline(state):
    config = app_config()
    require(config == state["config"], "app_configuration_changed")
    members = inventory(config)
    evidence = baseline(members, observations(config["revision"]), config["revision"], now())
    require(
        evidence["session_hash"] == state["baseline"]["session_hash"]
        and evidence["entra_hash"] == state["baseline"]["entra_hash"],
        "baseline_changed",
    )
    return members, evidence


def resource_snapshot(state, require_no_executions=False, allow_failed_job=False):
    identity_item = _probe(IDENTITY, "2023-01-31", RESOURCE_NOT_FOUND)
    role_item = _probe(ROLE, "2022-04-01", ROLE_NOT_FOUND)
    assignment_items = {
        ASSIGNMENT: _probe(ASSIGNMENT, "2022-04-01", ASSIGNMENT_NOT_FOUND),
        ACR_ASSIGNMENT: _probe(ACR_ASSIGNMENT, "2022-04-01", ASSIGNMENT_NOT_FOUND),
    }
    job_item = _probe(JOB_ID, "2024-03-01", RESOURCE_NOT_FOUND)
    present = set()
    identity = None
    if identity_item is not None:
        properties = validate_identity_item(identity_item)
        identity = {"id": identity_item["id"], **properties}
        present.add(IDENTITY)
    if role_item is not None:
        validate_role_item(role_item)
        present.add(ROLE_RESOURCE)
    require(
        identity is not None or (job_item is None and not any(assignment_items.values())),
        "provision_partial_inconsistent",
    )
    if identity is not None:
        expected_assignments = {ASSIGNMENT.casefold(), ACR_ASSIGNMENT.casefold()}
        require(
            identity_assignment_ids(identity["principalId"]) <= expected_assignments,
            "identity_extra_permissions",
        )
    for rid, role, scope in (
        (ASSIGNMENT, ROLE, SECRET_ID),
        (ACR_ASSIGNMENT, ACR_ROLE, REGISTRY),
    ):
        item = assignment_items[rid]
        if item is None:
            continue
        require(identity is not None, "provision_partial_inconsistent")
        if rid == ASSIGNMENT:
            require(role_item is not None, "provision_partial_inconsistent")
        validate_assignment_item(item, rid, identity["principalId"], role, scope)
        present.add(rid)
    if job_item is not None:
        require(identity is not None, "provision_partial_inconsistent")
        allowed_states = ("Succeeded", "Failed") if allow_failed_job else ("Succeeded",)
        validate_job_item(state, job_item, identity, allowed_states)
        present.add(JOB_ID)
        if require_no_executions:
            require(not executions(), "preexisting_execution")
    return {
        "present": present,
        "identity": identity,
        "job": job_item,
        "job_provisioning_state": (
            job_item["properties"]["provisioningState"] if job_item is not None else None
        ),
    }


def provision(state, resume):
    validate_current_baseline(state)
    attempted = state["provision_attempted"]
    snapshot = resource_snapshot(
        state,
        require_no_executions=True,
        allow_failed_job=attempted,
    )
    if not attempted and snapshot["present"]:
        require(
            snapshot["present"] == {IDENTITY}
            and snapshot["job"] is None
            and not identity_assignment_ids(snapshot["identity"]["principalId"]),
            "fresh_run_resources_exist",
        )
    principal = snapshot["identity"]["principalId"] if snapshot["identity"] else ""
    plan = preview(
        state["run_id"],
        state["baseline"]["session_hash"],
        snapshot["present"],
        principal,
    )
    deploy_required = bool(plan["creates"]) or snapshot["job_provisioning_state"] == "Failed"
    if deploy_required:
        if not attempted:
            state["provision_attempted"] = True
            save(state)
        result = azure(
            [
                "deployment",
                "group",
                "create",
                *deployment(state["run_id"], state["baseline"]["session_hash"], principal),
                "--query",
                "properties.provisioningState",
            ]
        )
        require(result == "Succeeded", "provision_unconfirmed")
    final = resource_snapshot(state, require_no_executions=True)
    require(final["present"] == set(EXPECTED_RESOURCE_IDS), "provision_incomplete")
    validate_current_baseline(state)
    state["phase"] = "provisioned"
    save(state)


def cleanup_execution_guard(snapshot, require_empty=False):
    if snapshot["job"] is None:
        return
    rows = executions()
    if snapshot["job_provisioning_state"] == "Failed":
        require(not rows, "failed_job_has_executions")
    else:
        terminal_only(rows)
    if require_empty:
        require(not rows, "unobserved_execution")


def measure(state, rows, events, members, at):
    original = state["members"]
    require(
        all(members.get(replica) == worker for replica, worker in original.items()),
        "baseline_member_disappeared_or_restarted",
    )
    state["new_members"] = {r: w for r, w in members.items() if r not in original}
    rotated = [e for e in events if e["stage"] == "rotated"]
    require(
        not any(
            e["stage"] in {"blocked", "recovered", "manual_recovery_required", "unexpected_failure"}
            for e in events
        ),
        "private_drill_failed",
    )
    if not rotated:
        require(at <= date(state["started_at"]) + timedelta(seconds=120), "write_signal_missing")
        require(
            all(
                r["version_hash"] == state["baseline"]["session_hash"]
                for r in rows
                if r["logical_secret"] == "session"
                and date(r["observed_at"]) >= date(state["baseline"]["at"])
            ),
            "baseline_changed_before_write",
        )
        return False
    require(len({(e["version_hash"], e["created_on"]) for e in rotated}) == 1, "rotation_ambiguous")
    target, created = rotated[0]["version_hash"], date(rotated[0]["created_on"])
    require(
        date(state["baseline"]["at"]) < created <= at + timedelta(seconds=MARGIN),
        "rotation_time_unproved",
    )
    baseline_keys = {tuple(p) for p in state["baseline"]["processes"]}
    adopted = {}
    recent = {}
    for row in rows:
        observed = date(row["observed_at"])
        if observed < date(state["baseline"]["at"]):
            continue
        require(observed <= at + timedelta(seconds=MARGIN), "clock_uncertain")
        require(
            row["source"] == "azure"
            and row["result"] in ("adopted", "unchanged")
            and row["error_category"] == "none",
            "provider_unhealthy",
        )
        if row["replica"] in original:
            require(key(row) in baseline_keys, "baseline_incarnation_changed")
        require(
            row["replica"] in members and row["pid"] == members[row["replica"]]["pid"],
            "unaccounted_worker",
        )
        stream = (key(row), row["logical_secret"], row["stage"])
        recent[stream] = max(observed, recent.get(stream, observed))
        if row["logical_secret"] == "entra":
            require(row["version_hash"] == state["baseline"]["entra_hash"], "entra_changed")
        elif observed < created - timedelta(seconds=MARGIN):
            require(row["version_hash"] == state["baseline"]["session_hash"], "prewrite_race")
        elif row["version_hash"] == target:
            # A conservative upper bound includes clock uncertainty, never extends 60s.
            elapsed = (observed - created).total_seconds()
            if 0 <= elapsed and elapsed + MARGIN <= 60:
                adopted[key(row)] = min(elapsed + MARGIN, adopted.get(key(row), 60))
        elif observed > created + timedelta(seconds=60):
            raise ControlError("adoption_lost")
    state["adoption_seconds_upper_bound"] = [
        {"process": list(k), "seconds": v} for k, v in sorted(adopted.items()) if k in baseline_keys
    ]
    if at > created + timedelta(seconds=180):
        require(baseline_keys <= adopted.keys(), "all_worker_adoption_unproved")
    # Ingestion may be delayed but must remain available throughout the hour.
    if at > created + timedelta(seconds=180):
        all_keys = baseline_keys | {key(r) for r in rows if r["replica"] in state["new_members"]}
        require(len({k[1] for k in all_keys}) == len(members), "new_worker_coverage_missing")
        for k in all_keys:
            for logical, stage in STREAMS:
                require(
                    recent.get((k, logical, stage), created) >= at - timedelta(seconds=120),
                    "heartbeat_missing",
                )
    stages = {e["stage"] for e in events}
    proof = {"cookie_early", "cookie_near", "cookie_boundary", "awaiting_controller_stop"}
    if proof <= stages and baseline_keys <= adopted.keys():
        for stage, low, high in (
            ("cookie_early", 65, 70),
            ("cookie_near", 3585, 3590),
            ("cookie_boundary", 3605, 3610),
            ("awaiting_controller_stop", 3605, 3620),
        ):
            matching = [e for e in events if e["stage"] == stage]
            require(
                all(
                    e["version_hash"] == target
                    and date(e["created_on"]) == created
                    and low <= (date(e["at"]) - created).total_seconds() < high
                    for e in matching
                ),
                "cookie_timing_unproved",
            )
        require(at < created + timedelta(seconds=4000), "controller_deadline")
        return True
    require(at < created + timedelta(seconds=4000), "controller_deadline")
    return False


def observe(state, approve):
    require(state["phase"] == "running", "execution_not_running")
    require(approve, "observer_stop_approval_required")
    start_wall, start_mono = now(), time.monotonic()
    while True:
        at = now()
        require(
            abs((at - start_wall).total_seconds() - (time.monotonic() - start_mono)) < 1,
            "controller_clock_step",
        )
        config = app_config()
        require(config == state["config"], "app_configuration_changed")
        members = inventory(config)
        rows = observations(config["revision"])
        events = job_events(state["run_id"])
        executions_now = executions()
        require(
            executions_now == [{"name": state["execution"], "status": "Running"}],
            "execution_membership_changed",
        )
        finished = measure(state, rows, events, members, now())
        state["last_observed_at"] = harness.stamp(now())
        save(state)
        if finished:
            state["phase"] = "proof_complete_stop_pending"
            save(state)
            azure(
                [
                    "containerapp",
                    "job",
                    "stop",
                    "-g",
                    GROUP,
                    "-n",
                    JOB,
                    "--job-execution-name",
                    state["execution"],
                ]
            )
            for _ in range(12):
                rows = executions()
                if rows == [{"name": state["execution"], "status": "Stopped"}]:
                    state["phase"] = "accepted_stopped"
                    save(state)
                    return
                time.sleep(5)
            raise ControlError("stop_unconfirmed_watchdog_may_recover")
        time.sleep(10)


def cleanup(state):
    require(
        state["phase"]
        in (
            "provision_intent",
            "aborted",
            "accepted_stopped",
            "recovered",
            "provisioned",
            "rights_revoked",
        ),
        "cleanup_requires_terminal_proof",
    )
    if state["phase"] in ("provision_intent", "aborted"):
        state["phase"] = "aborted"
        save(state)
    snapshot = resource_snapshot(state, allow_failed_job=True)
    cleanup_execution_guard(snapshot, require_empty=state["phase"] == "provisioned")
    for rid, role, scope in ((ASSIGNMENT, ROLE, SECRET_ID), (ACR_ASSIGNMENT, ACR_ROLE, REGISTRY)):
        snapshot = resource_snapshot(state, allow_failed_job=True)
        cleanup_execution_guard(snapshot)
        item = rest("get", rid, "2022-04-01", not_found_code=ASSIGNMENT_NOT_FOUND)
        if item is None:
            continue
        require(snapshot["identity"] is not None, "provision_partial_inconsistent")
        validate_assignment_item(item, rid, snapshot["identity"]["principalId"], role, scope)
        rest("delete", rid, "2022-04-01")
    snapshot = resource_snapshot(state, allow_failed_job=True)
    cleanup_execution_guard(snapshot)
    remaining = azure(
        ["role", "assignment", "list", "--all", "--query", "[?roleDefinitionId=='" + ROLE + "'].id"]
    )
    require(not remaining, "role_still_assigned")
    role_item = rest("get", ROLE, "2022-04-01", not_found_code=ROLE_NOT_FOUND)
    if role_item is not None:
        validate_role_item(role_item)
        rest("delete", ROLE, "2022-04-01")
    final = resource_snapshot(state, allow_failed_job=True)
    cleanup_execution_guard(final)
    require(
        not ({ROLE_RESOURCE, ASSIGNMENT, ACR_ASSIGNMENT} & final["present"]),
        "cleanup_unconfirmed",
    )
    state["phase"] = "rights_revoked"
    save(state)


def main(argv=None):
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--subscription", required=True, choices=(SUBSCRIPTION,))
    parser.add_argument("--run-id", required=True, type=lambda s: str(UUID(s)))
    parser.add_argument("--approve-change", action="store_true")
    parser.add_argument("--reviewed", action="store_true")
    args = parser.parse_args(argv)
    if args.action != "session-preview" and not (args.approve_change and args.reviewed):
        print("PLAN ONLY: independent review, --reviewed and --approve-change required.")
        return 0
    lock = None
    try:
        # Local operator serialization only; not a distributed lease or KV compare-and-set.
        path = state_path(args.run_id).parent / "session-rotation.lock"
        lock = os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == "session-preview":
            present, principal = fresh_preview_context()
            print(
                json.dumps(
                    preview(args.run_id, "0" * 12, present, principal),
                    sort_keys=True,
                )
            )
            return 0
        if args.action == "session-provision":
            resume = state_path(args.run_id).exists()
            if resume:
                state = load(args.run_id)
                require(state["phase"] == "provision_intent", "provision_resume_phase_invalid")
            else:
                config = app_config()
                members = inventory(config)
                evidence = baseline(
                    members,
                    observations(config["revision"]),
                    config["revision"],
                    now(),
                )
                state = {
                    "schema": 1,
                    "run_id": args.run_id,
                    "source_hash": source_hash(),
                    "phase": "provision_intent",
                    "provision_attempted": False,
                    "config": config,
                    "members": members,
                    "baseline": evidence,
                }
                save(state)
            provision(state, resume)
        else:
            state = load(args.run_id)
            if args.action == "session-run":
                require(state["phase"] == "provisioned", "run_phase_invalid")
                validate_job(state)
                require(not executions(), "preexisting_execution")
                config = app_config()
                require(config == state["config"], "app_configuration_changed")
                members = inventory(config)
                evidence = baseline(
                    members, observations(config["revision"]), config["revision"], now()
                )
                require(
                    evidence["session_hash"] == state["baseline"]["session_hash"]
                    and evidence["entra_hash"] == state["baseline"]["entra_hash"],
                    "baseline_changed",
                )
                state.update(
                    members=members,
                    baseline=evidence,
                    phase="start_intent",
                    started_at=harness.stamp(now()),
                )
                save(state)
                execution = azure(
                    ["containerapp", "job", "start", "-g", GROUP, "-n", JOB, "--query", "name"]
                )
                state.update(execution=safe_id(execution), phase="running")
                save(state)
                observe(state, args.approve_change)
            elif args.action == "session-observe":
                validate_job(state)
                observe(state, args.approve_change)
            elif args.action == "session-recover":
                require(state["phase"] in ("running", "start_intent"), "recovery_phase_invalid")
                validate_job(state)
                terminal_only(executions())
                events = job_events(args.run_id)
                stages = {e["stage"] for e in events}
                require("rotated" in stages, "rotation_unprovable")
                require("manual_recovery_required" not in stages, "ambiguous_recovery_blocked")
                if "recovered" not in stages:
                    execution = azure(
                        ["containerapp", "job", "start", "-g", GROUP, "-n", JOB, "--query", "name"]
                    )
                    state["recovery_execution"] = safe_id(execution)
                    save(state)
                    raise ControlError("recovery_started_observe_terminal_before_retry")
                terminal_only(executions())
                state["phase"] = "recovered"
                save(state)
            elif args.action == "session-cleanup":
                cleanup(state)
        print(json.dumps({"schema": "session-controller/v1", "phase": state["phase"]}))
        return 0
    except Exception as error:
        # Sole terminal privacy boundary: never print arbitrary exception contents.
        code = error.args[0] if isinstance(error, ControlError) else "controller_failed"
        print(
            json.dumps(
                {
                    "schema": "session-controller/v1",
                    "status": code,
                    "action": "blocked; do not stop a running watchdog",
                }
            )
        )
        return 1
    finally:
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    sys.exit(main())
