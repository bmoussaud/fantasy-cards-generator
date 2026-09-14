"""Fixed read-only /proc probe; outputs no cmdline, environment, or user data."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

workers = []
unknown = 0
for entry in Path("/proc").iterdir():
    if not entry.name.isdecimal() or int(entry.name) == os.getpid():
        continue
    try:
        executable = (entry / "exe").resolve(strict=True).name
        if not executable.startswith("python"):
            continue
        args = (entry / "cmdline").read_bytes().split(b"\0")
        if b"app.entrypoint:app" not in args or not any(b"uvicorn" in arg for arg in args):
            unknown += 1
            continue
        if any(arg in (b"--workers", b"--reload") or arg.startswith(b"--workers=") for arg in args):
            unknown += 1
            continue
        stat = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        workers.append({"pid": int(entry.name), "start_ticks": int(stat[19])})
    except FileNotFoundError:
        # Process exit during enumeration invalidates the snapshot, rather than hiding a worker.
        unknown += 1
print(
    "SESSION_PROCESS_INVENTORY="
    + json.dumps(
        {
            "schema": 1,
            "workers": workers,
            "unknown": unknown,
            "at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
    ),
    flush=True,
)
