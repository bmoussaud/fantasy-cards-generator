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
    echo "  azd env set ENABLE_FOUNDRY_AGENT_ACCESS true" >&2
    echo "  azd env set CARD_ORCHESTRATOR_ENABLE_PREREQUISITES true" >&2
    echo "  azd provision" >&2
    echo "  azd deploy card-orchestrator" >&2
    echo "" >&2
    exit 1
fi

echo "card-orchestrator lifecycle gate: prerequisites enabled, proceeding."
