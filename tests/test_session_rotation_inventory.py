"""Tests for scripts/session_rotation/process_inventory.py.

This script is a fixed, read-only `/proc` probe: it is `.read_text()`'d verbatim by
control.py and executed inside the target container via `az containerapp exec`
(see control.py's `process_inventory()`), so it deliberately has no `__main__`
guard or importable functions -- it always runs top-level on import/exec.

To test it without touching a real container or the real host `/proc`, these
tests build a small fake `/proc`-shaped directory tree under `tmp_path` and
redirect the script's single `Path("/proc")` call into it (all other `Path`
usage -- `entry / "exe"`, `.resolve()`, etc. -- is untouched real pathlib
behaviour operating on that fake tree). The script is then executed with
`runpy.run_path`, and stdout is parsed for the `SESSION_PROCESS_INVENTORY=`
line, exactly as control.py does over a real PTY.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import runpy
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INVENTORY_PATH = ROOT / "scripts/session_rotation/process_inventory.py"

# Fake PIDs chosen far away from any plausible real pytest-process PID.
WORKER_PID = 900001
EXTRA_ARGS_PID = 900002
NON_PYTHON_PID = 900003
UNRECOGNIZED_ARGS_PID = 900004
VANISHED_PID = 900005


def _stat_line(pid: int, comm: str, starttime: int) -> str:
    # Fields 3..22 of /proc/[pid]/stat (state..starttime); only index 19 (starttime,
    # 0-based after the script's rsplit(")",1)) is read, the rest are placeholders.
    fields = [
        "S",
        "1",
        str(pid),
        str(pid),
        "0",
        "-1",
        "4194304",
        "100",
        "0",
        "0",
        "0",
        "10",
        "5",
        "0",
        "0",
        "20",
        "0",
        "4",
        "0",
        str(starttime),
    ]
    return f"{pid} ({comm}) {' '.join(fields)}\n"


def _make_proc_entry(
    proc_dir: Path,
    pid: int,
    *,
    exe_target: str,
    cmdline_args: list[bytes],
    starttime: int = 123456,
    include_exe: bool = True,
    include_cmdline: bool = True,
    include_stat: bool = True,
):
    entry = proc_dir / str(pid)
    entry.mkdir(parents=True)
    if include_exe:
        exe_link = entry / "exe"
        exe_link.symlink_to(exe_target)
    if include_cmdline:
        (entry / "cmdline").write_bytes(b"\x00".join(cmdline_args) + b"\x00")
    if include_stat:
        (entry / "stat").write_text(_stat_line(pid, Path(exe_target).name, starttime))
    return entry


@pytest.fixture()
def proc_tree(tmp_path):
    proc_dir = tmp_path / "proc"
    proc_dir.mkdir()
    return proc_dir


def _run_inventory(monkeypatch, proc_dir):
    real_path = pathlib.Path

    def redirecting_path(*args, **kwargs):
        if args and str(args[0]) == "/proc":
            return real_path(proc_dir, *args[1:], **kwargs)
        return real_path(*args, **kwargs)

    monkeypatch.setattr(pathlib, "Path", redirecting_path)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        runpy.run_path(str(INVENTORY_PATH), run_name="__session_process_inventory_test__")
    return buffer.getvalue()


def _parse_inventory(output: str) -> dict:
    lines = [line for line in output.splitlines() if line.startswith("SESSION_PROCESS_INVENTORY=")]
    assert len(lines) == 1, f"expected exactly one inventory line, got: {output!r}"
    return json.loads(lines[0][len("SESSION_PROCESS_INVENTORY=") :])


PYTHON_EXE = "/usr/bin/python3"


def _touch_fake_python(tmp_path):
    fake = tmp_path / "python3.12"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    return fake


def test_single_healthy_uvicorn_worker_is_reported(monkeypatch, proc_tree, tmp_path):
    fake_python = _touch_fake_python(tmp_path)
    _make_proc_entry(
        proc_tree,
        WORKER_PID,
        exe_target=str(fake_python),
        cmdline_args=[
            b"/app/.venv/bin/python",
            b"-m",
            b"uvicorn",
            b"app.entrypoint:app",
            b"--host",
            b"0.0.0.0",
        ],
        starttime=555555,
    )

    output = _run_inventory(monkeypatch, proc_tree)
    result = _parse_inventory(output)

    assert result["schema"] == 1
    assert result["unknown"] == 0
    assert result["workers"] == [{"pid": WORKER_PID, "start_ticks": 555555}]
    assert result["at"].endswith("Z")


def test_worker_with_reload_or_workers_flag_is_unknown_not_a_worker(
    monkeypatch, proc_tree, tmp_path
):
    fake_python = _touch_fake_python(tmp_path)
    _make_proc_entry(
        proc_tree,
        EXTRA_ARGS_PID,
        exe_target=str(fake_python),
        cmdline_args=[
            b"/app/.venv/bin/python",
            b"-m",
            b"uvicorn",
            b"app.entrypoint:app",
            b"--workers",
            b"4",
        ],
    )

    output = _run_inventory(monkeypatch, proc_tree)
    result = _parse_inventory(output)

    assert result["workers"] == []
    assert result["unknown"] == 1


def test_worker_with_workers_equals_flag_is_unknown(monkeypatch, proc_tree, tmp_path):
    fake_python = _touch_fake_python(tmp_path)
    _make_proc_entry(
        proc_tree,
        EXTRA_ARGS_PID,
        exe_target=str(fake_python),
        cmdline_args=[
            b"/app/.venv/bin/python",
            b"-m",
            b"uvicorn",
            b"app.entrypoint:app",
            b"--workers=4",
        ],
    )

    output = _run_inventory(monkeypatch, proc_tree)
    result = _parse_inventory(output)

    assert result["workers"] == []
    assert result["unknown"] == 1


def test_non_python_process_is_ignored_entirely(monkeypatch, proc_tree, tmp_path):
    shell = tmp_path / "fake-shell"
    shell.write_text("#!/bin/sh\n")
    shell.chmod(0o755)
    _make_proc_entry(
        proc_tree,
        NON_PYTHON_PID,
        exe_target=str(shell),
        cmdline_args=[b"/bin/sh", b"-c", b"sleep 1"],
    )

    output = _run_inventory(monkeypatch, proc_tree)
    result = _parse_inventory(output)

    assert result["workers"] == []
    assert result["unknown"] == 0  # silently ignored, not "unknown"


def test_python_process_without_expected_entrypoint_is_unknown(monkeypatch, proc_tree, tmp_path):
    fake_python = _touch_fake_python(tmp_path)
    _make_proc_entry(
        proc_tree,
        UNRECOGNIZED_ARGS_PID,
        exe_target=str(fake_python),
        cmdline_args=[b"/app/.venv/bin/python", b"-m", b"some_other_tool"],
    )

    output = _run_inventory(monkeypatch, proc_tree)
    result = _parse_inventory(output)

    assert result["workers"] == []
    assert result["unknown"] == 1


def test_process_that_vanished_during_enumeration_counts_as_unknown(monkeypatch, proc_tree):
    # entry/"exe" does not exist -> resolve(strict=True) raises FileNotFoundError,
    # simulating a process that exited mid-scan rather than a hidden worker.
    entry = proc_tree / str(VANISHED_PID)
    entry.mkdir(parents=True)

    output = _run_inventory(monkeypatch, proc_tree)
    result = _parse_inventory(output)

    assert result["workers"] == []
    assert result["unknown"] == 1


def test_non_numeric_and_self_pid_entries_are_skipped(monkeypatch, proc_tree, tmp_path):
    (proc_tree / "self").symlink_to(str(os.getpid()), target_is_directory=False)
    (proc_tree / "net").mkdir()
    _make_proc_entry(
        proc_tree,
        os.getpid(),
        exe_target=str(_touch_fake_python(tmp_path)),
        cmdline_args=[b"ignored"],
    )

    output = _run_inventory(monkeypatch, proc_tree)
    result = _parse_inventory(output)

    assert result["workers"] == []
    assert result["unknown"] == 0


def test_multiple_healthy_workers_are_all_reported(monkeypatch, proc_tree, tmp_path):
    fake_python = _touch_fake_python(tmp_path)
    _make_proc_entry(
        proc_tree,
        WORKER_PID,
        exe_target=str(fake_python),
        cmdline_args=[b"/app/.venv/bin/python", b"-m", b"uvicorn", b"app.entrypoint:app"],
        starttime=111,
    )
    _make_proc_entry(
        proc_tree,
        WORKER_PID + 1,
        exe_target=str(fake_python),
        cmdline_args=[b"/app/.venv/bin/python", b"-m", b"uvicorn", b"app.entrypoint:app"],
        starttime=222,
    )

    output = _run_inventory(monkeypatch, proc_tree)
    result = _parse_inventory(output)

    assert {(w["pid"], w["start_ticks"]) for w in result["workers"]} == {
        (WORKER_PID, 111),
        (WORKER_PID + 1, 222),
    }
    assert result["unknown"] == 0


def test_output_contains_no_cmdline_environment_or_user_data(monkeypatch, proc_tree, tmp_path):
    fake_python = _touch_fake_python(tmp_path)
    secret_arg = b"--super-secret-flag=leak-me-not"
    _make_proc_entry(
        proc_tree,
        WORKER_PID,
        exe_target=str(fake_python),
        cmdline_args=[
            b"/app/.venv/bin/python",
            b"-m",
            b"uvicorn",
            b"app.entrypoint:app",
            secret_arg,
        ],
    )

    output = _run_inventory(monkeypatch, proc_tree)

    assert b"leak-me-not" not in output.encode()
    result = _parse_inventory(output)
    assert set(result["workers"][0]) == {"pid", "start_ticks"}
