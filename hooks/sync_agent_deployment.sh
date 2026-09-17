#!/bin/sh
# Persist the immutable hosted-agent identity emitted by azd deploy together
# with the application artifact version used to build that hosted version.
set -eu

agent_name="$(
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill \
    azd env get-value AGENT_CARD_ORCHESTRATOR_NAME 2>/dev/null || true
)"
agent_version="$(
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill \
    azd env get-value AGENT_CARD_ORCHESTRATOR_VERSION 2>/dev/null || true
)"
expected_version="$(
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill \
    azd env get-value CARD_ORCHESTRATOR_VERSION 2>/dev/null || true
)"

if [ "$agent_name" != "card-orchestrator" ]; then
  echo "ERROR: azd did not report the expected card-orchestrator agent name." >&2
  exit 1
fi

if ! printf '%s' "$agent_version" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'; then
  echo "ERROR: azd did not report a bounded card-orchestrator agent version." >&2
  exit 1
fi

if ! printf '%s' "$expected_version" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'; then
  echo "ERROR: CARD_ORCHESTRATOR_VERSION must identify the deployed application artifact." >&2
  exit 1
fi

if ! AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set \
  "FOUNDRY_AGENT_NAME=$agent_name" \
  "FOUNDRY_AGENT_VERSION=$agent_version" \
  "FOUNDRY_AGENT_EXPECTED_VERSION=$expected_version" >/dev/null; then
  echo "ERROR: Failed to persist the hosted-agent deployment identity transaction." >&2
  exit 1
fi

echo "Hosted-agent deployment identity and artifact version stored for web provisioning." >&2
