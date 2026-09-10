#!/usr/bin/env bash
# Root deployment orchestrator with approval gates.
#
# This is the supported entry point for production-safe deployments,
# preserving the --approve-change / --approve-prod enforcement semantics
# of the deprecated deployments/card-orchestrator/deploy.py launcher.
#
# Plan-only by default.  Mutations require --approve-change.
# Production requires --approve-prod after separate production review.
#
# Usage:
#   ./deploy.sh web                                            # Plan web deploy
#   ./deploy.sh web --approve-change                           # Execute web deploy (dev)
#   ./deploy.sh agent --approve-change                         # Execute agent deploy (dev)
#   ./deploy.sh full --approve-change                          # Execute full deploy (dev)
#   ./deploy.sh provision --approve-change                     # Execute provision (dev)
#   ./deploy.sh preview                                        # Provision preview (always safe)
#   ./deploy.sh web --environment prod --approve-change --approve-prod
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ACTION="${1:-}"
shift || true

ENVIRONMENT="dev"
APPROVE_CHANGE=false
APPROVE_PROD=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --environment)
            ENVIRONMENT="${2:-}"
            shift 2
            ;;
        --approve-change)
            APPROVE_CHANGE=true
            shift
            ;;
        --approve-prod)
            APPROVE_PROD=true
            shift
            ;;
        *)
            echo "ERROR: Unknown option: $1" >&2
            exit 2
            ;;
    esac
done

case "$ACTION" in
    web|agent|full|provision|preview) ;;
    *)
        echo "Usage: $0 {web|agent|full|provision|preview} [OPTIONS]" >&2
        echo "" >&2
        echo "Actions:" >&2
        echo "  web        Deploy web-nat Container App only" >&2
        echo "  agent      Deploy card-orchestrator hosted agent only" >&2
        echo "  full       Deploy both web-nat and card-orchestrator" >&2
        echo "  provision  Provision shared infrastructure" >&2
        echo "  preview    Provision preview (safe, no mutations)" >&2
        echo "" >&2
        echo "Options:" >&2
        echo "  --environment dev|prod  Target environment (default: dev)" >&2
        echo "  --approve-change        Required for all mutations" >&2
        echo "  --approve-prod          Required for prod (after separate review)" >&2
        exit 2
        ;;
esac

if [[ "$ENVIRONMENT" != "dev" && "$ENVIRONMENT" != "prod" ]]; then
    echo "ERROR: Only dev and prod environments are supported." >&2
    exit 2
fi

if [[ "$ENVIRONMENT" == "prod" && "$APPROVE_PROD" != "true" ]]; then
    echo "ERROR: Production deployments require --approve-prod after separate production review." >&2
    exit 2
fi

if [[ "$ACTION" != "preview" && "$APPROVE_CHANGE" != "true" ]]; then
    echo "Project: $ROOT"
    case "$ACTION" in
        web)
            echo "Command: azd --cwd $ROOT deploy web-nat --environment $ENVIRONMENT --no-prompt"
            ;;
        agent)
            echo "Command: azd --cwd $ROOT deploy card-orchestrator --environment $ENVIRONMENT --no-prompt"
            ;;
        full)
            echo "Command: azd --cwd $ROOT deploy web-nat --environment $ENVIRONMENT --no-prompt"
            echo "Command: azd --cwd $ROOT deploy card-orchestrator --environment $ENVIRONMENT --no-prompt"
            ;;
        provision)
            echo "Command: azd --cwd $ROOT provision --environment $ENVIRONMENT --no-prompt"
            ;;
    esac
    echo ""
    echo "PLAN ONLY: no azd process started. Add --approve-change to execute."
    exit 0
fi

case "$ACTION" in
    preview)
        exec azd --cwd "$ROOT" provision --preview --environment "$ENVIRONMENT" --no-prompt
        ;;
    web)
        exec azd --cwd "$ROOT" deploy web-nat --environment "$ENVIRONMENT" --no-prompt
        ;;
    agent)
        exec azd --cwd "$ROOT" deploy card-orchestrator --environment "$ENVIRONMENT" --no-prompt
        ;;
    full)
        azd --cwd "$ROOT" deploy web-nat --environment "$ENVIRONMENT" --no-prompt
        exec azd --cwd "$ROOT" deploy card-orchestrator --environment "$ENVIRONMENT" --no-prompt
        ;;
    provision)
        exec azd --cwd "$ROOT" provision --environment "$ENVIRONMENT" --no-prompt
        ;;
esac
