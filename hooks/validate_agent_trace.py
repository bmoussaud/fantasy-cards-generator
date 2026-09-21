"""Validate the raw azd-injected setting before deployment consumes its default."""

import os
import sys


def main() -> int:
    # azd injects raw environment entries into hooks, preserving explicit empty values.
    value = os.environ.get("FCG_AGENT_TRACE_ENABLED")
    if value is not None and value.strip().lower() not in {"true", "false"}:
        print("ERROR: FCG_AGENT_TRACE_ENABLED must be true or false when set.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
