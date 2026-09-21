"""Offline deployment-boundary tests; never provision, deploy, or invoke models."""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks/validate_agent_trace.py"
ERROR = "ERROR: AGENT_TRACE_ENABLED must be true or false when set."
VALUES = [
    None,
    "",
    " \t\r\n ",
    "true",
    "false",
    " \ttrue\r\n",
    "\n false ",
    "TrUe",
    "FaLsE",
    "\u2003TrUe\u2003",
    "1",
    "yes",
    "false invalid-canary",
    "true\nfalse",
]
VALUE_IDS = [
    "unset",
    "empty",
    "whitespace",
    "true",
    "false",
    "trimmed-true",
    "trimmed-false",
    "mixed-true",
    "mixed-false",
    "unicode-trimmed",
    "numeric",
    "yes",
    "invalid",
    "embedded-newline",
]
EVENTS = ("preprovision", "prepackage", "prepublish", "predeploy")


def _environment() -> dict[str, str]:
    return {
        "PATH": os.defpath,
        "PYTHONIOENCODING": "utf-8",
        "UNRELATED_SECRET": "unrelated-secret-canary",
    }


@pytest.mark.parametrize("value", VALUES, ids=VALUE_IDS)
def test_raw_setting_validation(value: str | None) -> None:
    env = _environment()
    if value is not None:
        env["AGENT_TRACE_ENABLED"] = value
    result = subprocess.run(
        [sys.executable, str(HOOK)], env=env, capture_output=True, text=True, check=False
    )
    valid = value is None or value.strip().lower() in {"true", "false"}
    assert result.returncode == (0 if valid else 1)
    assert result.stdout == ""
    assert result.stderr == ("" if valid else ERROR + "\n")


def _root_hook_block(event: str) -> str:
    root_hooks = (ROOT / "azure.yaml").read_text().split("\nhooks:\n", 1)[1]
    match = re.search(rf"^  {event}:\n(.*?)(?=^  \w+:|\Z)", root_hooks, re.M | re.S)
    assert match is not None
    return match[1]


@pytest.mark.parametrize("event", EVENTS)
def test_guard_is_first_fail_closed_root_hook(event: str) -> None:
    block = _root_hook_block(event)
    commands = re.findall(r"^      run: (.+)$", block, re.M)
    assert commands[0] == "python3 ./hooks/validate_agent_trace.py"
    assert "continueOnError: true" not in block


def test_authoritative_invocations_reach_root_hooks() -> None:
    manifest = (ROOT / "azure.yaml").read_text()
    assert "    - azd: provision\n" in manifest
    assert "    - azd: deploy card-orchestrator\n" in manifest
    assert "    - azd: deploy web-nat\n" in manifest
    wrapper = (ROOT / "deploy.sh").read_text()
    for command in ("provision", "deploy web-nat", "deploy card-orchestrator"):
        assert f'azd --cwd "$ROOT" {command} ' in wrapper
    assert "--skip-hooks" not in wrapper


@pytest.fixture
def azd_project(tmp_path: Path) -> tuple[Path, dict[str, str], str]:
    """Use a disposable azd project, never the developer's selected environment."""
    azd = shutil.which("azd")
    if azd is None:
        pytest.skip("azd is not installed")
    project = tmp_path / "project"
    project.mkdir()
    (project / "hooks").mkdir()
    shutil.copyfile(HOOK, project / "hooks/validate_agent_trace.py")
    # Keep the authoritative manifest; unrelated hooks become local tripwires.
    manifest = (ROOT / "azure.yaml").read_text()
    (project / "azure.yaml").write_text(manifest)
    for name in re.findall(r"run: \./hooks/([\w]+\.sh)", manifest):
        script = project / "hooks" / name
        script.write_text("#!/bin/sh\nprintf 'downstream-hook\\n' >> downstream.log\n")
        script.chmod(0o755)
    env = _environment() | {
        "AZD_CONFIG_DIR": str(tmp_path / "azd-config"),
        "AZURE_DEV_COLLECT_TELEMETRY": "no",
        "AZURE_DEV_USER_AGENT": "microsoft_foundry_skill",
    }
    result = subprocess.run(
        [
            azd,
            "--cwd",
            str(project),
            "env",
            "new",
            "offline",
            "--subscription",
            "00000000-0000-0000-0000-000000000000",
            "--location",
            "eastus",
            "--no-prompt",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return project, env, azd


@pytest.mark.parametrize("value", VALUES, ids=VALUE_IDS)
def test_real_azd_preserves_raw_setting_for_guard(
    azd_project: tuple[Path, dict[str, str], str], value: str | None
) -> None:
    project, env, azd = azd_project
    command = [azd, "--cwd", str(project)]
    if value is not None:
        result = subprocess.run(
            [*command, "env", "set", "AGENT_TRACE_ENABLED", value, "--no-prompt"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        # Persisted azd state must win even over a valid inherited shell value.
        env["AGENT_TRACE_ENABLED"] = "true"
    # Exercise the actual azd hook runner against the root manifest, not a shell
    # approximation of substitution. Only the guard and local tripwires can run.
    for event in EVENTS:
        result = subprocess.run(
            [*command, "hooks", "run", event, "--no-prompt"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        valid = value is None or value.strip().lower() in {"true", "false"}
        output = result.stdout + result.stderr
        assert (result.returncode == 0) == valid, output
        assert (ERROR in output) != valid, output
        assert "unrelated-secret-canary" not in output
        assert "invalid-canary" not in output
        assert (project / "downstream.log").exists() == valid
    if value is not None:
        result = subprocess.run(
            [*command, "env", "get-value", "AGENT_TRACE_ENABLED", "--no-prompt"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
        assert result.stdout == value.replace("\r\n", "\n") + "\n"
