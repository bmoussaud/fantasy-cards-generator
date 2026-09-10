# Consolidated root azure.yaml entry point (issue #130)

**Date:** 2026-09-10
**By:** Gimli

## Decision

The root `azure.yaml` is now the single supported entry point for both `web-nat`
(Container App, port 8000) and `card-orchestrator` (Foundry hosted agent, port 8088).

## Key design choices

1. **Mixed host types in single manifest:** azd 1.32.0 schema supports
   `host: containerapp` and `host: azure.ai.agent` per service. Verified against
   installed azd schema and extension capabilities; not yet verified against a live
   Azure deployment.

2. **Safe default workflow:** The `workflows.up` section deploys only `web-nat`.
   The card-orchestrator requires explicit `azd deploy card-orchestrator` after
   enabling prerequisites.

3. **Conditional infrastructure:** Agent prerequisites (ACR pull, registry connection,
   monitoring) are gated by `enableCardOrchestratorPrerequisites=false`. Agent RBAC
   stays gated by `enableFoundryAgentAccess=false`.

4. **No duplicate RBAC:** Root `agent-prerequisites.bicep` contains only ACR pull
   and registry connection. Foundry User and Agent Consumer roles remain in
   `ai-foundry.bicep` to avoid assignment drift.

5. **Deprecated nested manifest:** `deployments/card-orchestrator/azure.yaml` and
   `deploy.py` are marked DEPRECATED but retained as legacy references.

## Unsupported acceptance gate

The old launcher's `--approve-prod` / `--approve-change` flags have no direct
`azd` equivalent. Production approval must be enforced via external gates
(GitHub Environment protection rules, manual review).

## Impact

- All agents and operators should use root `azure.yaml` for new deployments
- Existing nested workflows continue to work but are deprecated
- No Azure resources are provisioned or deployed by this change
