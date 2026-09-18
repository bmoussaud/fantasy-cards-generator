#!/bin/sh
# Service-level lifecycle guard for card-orchestrator.
# Blocks deployment unless the operator has explicitly enabled prerequisites.
# Registered on prebuild, prepackage, prepublish, and predeploy so the hosted
# agent cannot be built, packaged, pushed, or deployed before explicit opt-in.
set -e

if [ "${CARD_ORCHESTRATOR_ENABLE_PREREQUISITES}" != "true" ]; then
    echo "" >&2
    echo "ERROR: card-orchestrator deployment is blocked." >&2
    echo "" >&2
    echo "Prerequisites must be enabled before building or deploying the hosted agent." >&2
    echo "Run the following commands, then retry:" >&2
    echo "" >&2
    echo "  azd env set CARD_ORCHESTRATOR_ENABLE_PREREQUISITES true" >&2
    echo "  azd env set CARD_ORCHESTRATOR_CREATE_REGISTRY_CONNECTION true" >&2
    echo "  azd provision" >&2
    echo "  azd deploy card-orchestrator" >&2
    echo "" >&2
    exit 1
fi

model_deployment="${AZURE_AI_MODEL_DEPLOYMENT_NAME:-}"
case "$model_deployment" in
    ""|*[!A-Za-z0-9._-]*)
        echo "ERROR: AZURE_AI_MODEL_DEPLOYMENT_NAME must identify the provisioned text model. Run 'azd provision' before packaging the agent." >&2
        exit 1
        ;;
esac
if [ "${#model_deployment}" -gt 128 ]; then
    echo "ERROR: AZURE_AI_MODEL_DEPLOYMENT_NAME must not exceed 128 characters." >&2
    exit 1
fi

artifact_version="${CARD_ORCHESTRATOR_VERSION:-}"
case "$artifact_version" in
    ""|[!A-Za-z0-9]*|*[!A-Za-z0-9._-]*)
        echo "ERROR: CARD_ORCHESTRATOR_VERSION must identify the application artifact. Run 'azd provision' or set the intended version before packaging the agent." >&2
        exit 1
        ;;
esac
if [ "${#artifact_version}" -gt 64 ]; then
    echo "ERROR: CARD_ORCHESTRATOR_VERSION must not exceed 64 characters." >&2
    exit 1
fi

echo "card-orchestrator lifecycle gate: prerequisites enabled, proceeding."
