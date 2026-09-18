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
project_endpoint="$(
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill \
    azd env get-value AZURE_AI_PROJECT_ENDPOINT 2>/dev/null || true
)"

if [ "$agent_name" != "card-orchestrator" ]; then
  echo "ERROR: azd did not report the expected card-orchestrator agent name." >&2
  exit 1
fi

is_version_identifier() {
  case "$1" in
    ""|[!A-Za-z0-9]*|*[!A-Za-z0-9._-]*) return 1 ;;
  esac
  [ "${#1}" -le 64 ]
}

if ! is_version_identifier "$agent_version"; then
  echo "ERROR: azd did not report a bounded card-orchestrator agent version." >&2
  exit 1
fi

if ! is_version_identifier "$expected_version"; then
  echo "ERROR: CARD_ORCHESTRATOR_VERSION must identify the deployed application artifact." >&2
  exit 1
fi

case "$project_endpoint" in
  ""|*[!A-Za-z0-9:/._-]*)
    echo "ERROR: AZURE_AI_PROJECT_ENDPOINT must be a valid Foundry project URL." >&2
    exit 1
    ;;
esac
if ! printf '%s' "$project_endpoint" | grep -Eq '^https://[A-Za-z0-9-]+\.services\.ai\.azure\.com/api/projects/[A-Za-z0-9._-]+/?$'; then
  echo "ERROR: AZURE_AI_PROJECT_ENDPOINT must be a valid Foundry project URL." >&2
  exit 1
fi

# The installed azd extension does not expand variables inside agentEndpoint.
endpoint_body="{\"agent_endpoint\":{\"version_selector\":{\"version_selection_rules\":[{\"type\":\"FixedRatio\",\"agent_version\":\"$agent_version\",\"traffic_percentage\":100}]},\"protocols\":[\"responses\"],\"authorization_schemes\":[{\"type\":\"Entra\"}]}}"
if ! az rest --method PATCH \
  --resource https://ai.azure.com \
  --url "${project_endpoint%/}/agents/$agent_name?api-version=2025-11-15-preview" \
  --body "$endpoint_body" --output none >/dev/null; then
  echo "ERROR: Failed to pin the agent endpoint to the deployed version. Web deployment identity was not changed." >&2
  exit 1
fi

if ! AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set \
  "FOUNDRY_AGENT_NAME=$agent_name" \
  "FOUNDRY_AGENT_VERSION=$agent_version" \
  "FOUNDRY_AGENT_EXPECTED_VERSION=$expected_version" >/dev/null; then
  echo "ERROR: Failed to persist the hosted-agent deployment identity transaction." >&2
  exit 1
fi

echo "Hosted-agent endpoint pinned; deployment identity and artifact version stored for web provisioning." >&2
