#!/bin/sh
# azd injects the selected environment's values into preprovision hooks.
set -eu

version="${CARD_ORCHESTRATOR_VERSION:-}"
if [ -z "$version" ]; then
  if ! version="$(git rev-parse --verify HEAD 2>/dev/null)"; then
    echo "ERROR: Cannot determine the agent artifact version. Set CARD_ORCHESTRATOR_VERSION explicitly before provisioning." >&2
    exit 1
  fi
fi

case "$version" in
  [!A-Za-z0-9]*|*[!A-Za-z0-9._-]*|"")
    echo "ERROR: CARD_ORCHESTRATOR_VERSION must be a 1-64 character artifact identifier using letters, digits, dots, underscores or hyphens, starting with a letter or digit." >&2
    exit 1
    ;;
esac
if [ "${#version}" -gt 64 ]; then
  echo "ERROR: CARD_ORCHESTRATOR_VERSION must not exceed 64 characters." >&2
  exit 1
fi

if ! azd env set CARD_ORCHESTRATOR_VERSION "$version" >/dev/null; then
  echo "ERROR: Failed to store CARD_ORCHESTRATOR_VERSION before provisioning." >&2
  exit 1
fi

echo "Agent artifact version stored for packaging and deployment." >&2
