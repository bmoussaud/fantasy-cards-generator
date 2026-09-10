#!/bin/sh
# Service-level predeploy guard for card-orchestrator.
# Blocks deployment unless the operator has explicitly enabled prerequisites.
# This prevents accidental hosted-agent builds/pushes/deploys via bare
# 'azd deploy' or premature 'azd deploy card-orchestrator'.
set -e

if [ "${CARD_ORCHESTRATOR_ENABLE_PREREQUISITES}" != "true" ]; then
    echo "" >&2
    echo "ERROR: card-orchestrator deployment is blocked." >&2
    echo "" >&2
    echo "Prerequisites must be enabled before deploying the hosted agent." >&2
    echo "Run the following commands, then retry:" >&2
    echo "" >&2
    echo "  azd env set ENABLE_FOUNDRY_AGENT_ACCESS true" >&2
    echo "  azd env set CARD_ORCHESTRATOR_ENABLE_PREREQUISITES true" >&2
    echo "  azd provision" >&2
    echo "  azd deploy card-orchestrator" >&2
    echo "" >&2
    exit 1
fi

echo "card-orchestrator deploy gate: prerequisites enabled, proceeding."
