#!/bin/bash
# Preprovision hook: ensures APP_SESSION_SECRET_KEY is set in the azd environment
# before any provision/deploy run. Generates a cryptographically secure value if absent.
# Secret-safe: no shell tracing, no value echoed, value unset from bash memory after storing.

set +x           # never trace — prevents secret leaking into logs
set -euo pipefail

existing=$(azd env get-values 2>/dev/null | sed -n 's/^APP_SESSION_SECRET_KEY="\?\([^"]*\)"\?$/\1/p' | tail -n 1)

if [[ -n "${existing}" ]]; then
  echo "APP_SESSION_SECRET_KEY is already set; skipping generation." >&2
  exit 0
fi

echo "APP_SESSION_SECRET_KEY is absent; generating a cryptographically secure value..." >&2

_secret=$(python3 -c "import secrets; print(secrets.token_hex(32))")

if ! azd env set APP_SESSION_SECRET_KEY "${_secret}" >/dev/null 2>&1; then
  echo "ERROR: Failed to store APP_SESSION_SECRET_KEY in azd environment." >&2
  unset _secret
  exit 1
fi

unset _secret
echo "APP_SESSION_SECRET_KEY generated and stored." >&2
