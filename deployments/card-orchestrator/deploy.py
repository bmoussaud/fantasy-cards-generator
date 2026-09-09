"""Explicit hosted-agent launcher. Planning is the default; prod needs a second gate."""

from __future__ import annotations

import argparse
import shlex
import subprocess
from pathlib import Path

PROJECT = Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preview", "provision", "deploy"))
    parser.add_argument("--environment", choices=("dev", "prod"), default="dev")
    parser.add_argument("--execute", action="store_true", help="Otherwise only print the command.")
    parser.add_argument(
        "--approve-change",
        action="store_true",
        help="Confirm reviewed prerequisites or a separately approved billable agent deployment.",
    )
    parser.add_argument(
        "--approve-prod",
        action="store_true",
        help="Required with --environment prod; never inferred or defaulted.",
    )
    args = parser.parse_args(argv)
    if args.environment == "prod" and not args.approve_prod:
        parser.error("prod requires --approve-prod after separate production review")
    if args.execute and args.action != "preview" and not args.approve_change:
        parser.error("provision/deploy require --approve-change after the runbook gates")

    command = ["azd", "provision"]
    if args.action == "preview":
        command.append("--preview")
    elif args.action == "deploy":
        command = ["azd", "deploy", "card-orchestrator"]
    command.extend(["--environment", args.environment, "--no-prompt"])
    print(f"Project: {PROJECT}\nCommand: {shlex.join(command)}", flush=True)
    if not args.execute:
        print("PLAN ONLY: no azd process started.")
        return 0
    # Scope azd to this manifest; never inherit a caller's root web project.
    return subprocess.run(command, cwd=PROJECT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
