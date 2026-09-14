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
#   ./deploy.sh web-pinned-preview --environment dev --subscription ID
#   ./deploy.sh web-pinned --environment dev --subscription ID --expect-fingerprint HASH --approve-change --reviewed
#   ./deploy.sh web --environment prod --approve-change --approve-prod
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ACTION="${1:-}"
shift || true

ENVIRONMENT="dev"
APPROVE_CHANGE=false
APPROVE_PROD=false
REVIEWED=false
ENVIRONMENT_EXPLICIT=false
SUBSCRIPTION=""
EXPECT_FINGERPRINT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --environment)
            ENVIRONMENT="${2:-}"
            ENVIRONMENT_EXPLICIT=true
            shift 2
            ;;
        --subscription)
            SUBSCRIPTION="${2:-}"
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
        --reviewed)
            REVIEWED=true
            shift
            ;;
        --expect-fingerprint)
            EXPECT_FINGERPRINT="${2:-}"
            shift 2
            ;;
        *)
            echo "ERROR: Unknown option: $1" >&2
            exit 2
            ;;
    esac
done

case "$ACTION" in
    web|agent|full|provision|preview|runner-preview|runner-provision|runner-start|web-pinned-preview|web-pinned) ;;
    *)
        echo "Usage: $0 {web|agent|full|provision|preview} [OPTIONS]" >&2
        echo "" >&2
        echo "Actions:" >&2
        echo "  web        Deploy web-nat Container App only" >&2
        echo "  agent      Deploy card-orchestrator hosted agent only" >&2
        echo "  full       Deploy both web-nat and card-orchestrator" >&2
        echo "  provision  Provision shared infrastructure" >&2
        echo "  preview    Provision preview (safe, no mutations)" >&2
        echo "  runner-preview|runner-provision|runner-start  Isolated dev metadata runner" >&2
        echo "  web-pinned-preview|web-pinned  Reviewed immutable dev web image" >&2
        echo "" >&2
        echo "Options:" >&2
        echo "  --environment dev|prod  Target environment (default: dev)" >&2
        echo "  --approve-change        Required for all mutations" >&2
        echo "  --approve-prod          Required for prod (after separate review)" >&2
        echo "  --subscription ID       Required for runner and pinned web commands" >&2
        echo "  --reviewed              Required with --approve-change for web-pinned" >&2
        echo "  --expect-fingerprint H  Required reviewed baseline for web-pinned mutation" >&2
        exit 2
        ;;
esac

# This deployment-only path never provisions, builds, pushes, or invokes shared hooks.
if [[ "$ACTION" == web-pinned* ]]; then
    if [[ "$ENVIRONMENT_EXPLICIT" != true || "$ENVIRONMENT" != dev || -z "$SUBSCRIPTION" ]]; then
        echo "ERROR: Pinned web deployment requires explicit --environment dev and --subscription ID." >&2
        exit 2
    fi
    PINNED_ARGS=("preview" "--subscription" "$SUBSCRIPTION")
    if [[ "$ACTION" == "web-pinned" ]]; then
        PINNED_ARGS[0]="deploy"
    fi
    if [[ "$APPROVE_CHANGE" == true ]]; then
        PINNED_ARGS+=("--approve-change")
    fi
    if [[ "$REVIEWED" == true ]]; then
        PINNED_ARGS+=("--reviewed")
    fi
    if [[ -n "$EXPECT_FINGERPRINT" ]]; then
        PINNED_ARGS+=("--expect-fingerprint" "$EXPECT_FINGERPRINT")
    fi
    exec python3 "$ROOT/scripts/pinned_web_image.py" "${PINNED_ARGS[@]}"
fi

# This additive path must never enter azd's shared credential-minting hooks.
if [[ "$ACTION" == runner-* ]]; then
    if [[ "$ENVIRONMENT_EXPLICIT" != true || "$ENVIRONMENT" != dev || -z "$SUBSCRIPTION" ]]; then
        echo "ERROR: Runner requires explicit --environment dev and --subscription ID." >&2
        exit 2
    fi
    RUNNER_ARGS=("$ACTION" "--subscription" "$SUBSCRIPTION")
    if [[ "$APPROVE_CHANGE" == true ]]; then
        RUNNER_ARGS+=("--approve-change")
    fi
    exec python3 "$ROOT/scripts/private_runner/control.py" "${RUNNER_ARGS[@]}"
fi

if [[ -n "$SUBSCRIPTION" ]]; then
    echo "ERROR: --subscription is supported only by runner and pinned web commands." >&2
    exit 2
fi

if [[ "$REVIEWED" == true ]]; then
    echo "ERROR: --reviewed is supported only by pinned web commands." >&2
    exit 2
fi

if [[ -n "$EXPECT_FINGERPRINT" ]]; then
    echo "ERROR: --expect-fingerprint is supported only by pinned web commands." >&2
    exit 2
fi

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
