# Agent operational ownership — card-orchestrator

Issue #99: explicit operational ownership for the `card-orchestrator` hosted-agent runtime. This runbook covers the ownership matrix, telemetry boundary, alert definitions, rollout/rollback procedure, and synthetic readiness probe.

## Ownership matrix

| Concern | Owner | Authority |
|---|---|---|
| Hosted-agent runtime (`card-orchestrator`) | Gimli (DevOps / Infra) | Deploy, rollback, delete hosted version |
| Web ACA application (`web-nat`) | Gimli (DevOps / Infra) | Independent; agent deploy MUST NOT touch web revision, traffic, or image |
| Model capacity (`gpt-5-5`, `gpt-image-2`) | Gimli (DevOps / Infra) | Governed by `infra/text-model-capacity.bicep`; separate approval gate |
| Agent monitoring alerts / workbook | Gimli (DevOps / Infra) | `deployments/card-orchestrator/infra/modules/agent-monitoring.bicep` |
| Incident escalation | Gimli → Gandalf (Architect) | Escalate to Gandalf if root cause is architectural or cross-runtime |
| Privacy / telemetry boundary | Aragorn (Backend Dev) review | Any telemetry schema change requires Aragorn review before merge |
| Evidence retention | Scribe | Decisions and deployment evidence written to `.squad/decisions/` |

## Telemetry boundary

### What the agent exports

The `card-orchestrator` runtime initialises telemetry via `app.telemetry.configure_telemetry()` before the host starts. The framework payload instrumentation is **disabled** by `_disable_payload_telemetry()` in `server.py`.

| Exported | Not exported |
|---|---|
| `AppRequests`: HTTP status codes, route, duration | Prompt text, card text, art prompts |
| `AppDependencies`: model provider calls, HTTP status, duration | Response body content, token counts |
| `AppExceptions`: exception type only | Exception messages that may echo request content |
| `AppMetrics` (`fcg.*`): outcome codes, stage names, error codes | Raw user input, session IDs, tenant IDs |
| `AppTraces`: bounded severity level + error code | Bearer tokens, auth headers |

### OTel service name

`OTEL_SERVICE_NAME=card-orchestrator` is injected via `azure.yaml`. In Application Insights, this maps to `AppRoleName == 'card-orchestrator'`. All agent-scoped alert and workbook KQL uses this value to isolate agent signals from the web app.

### Application Insights wiring

The root infra outputs `APPLICATIONINSIGHTS_CONNECTION_STRING` as an azd env var. The agent `azure.yaml` injects it as `APPLICATIONINSIGHTS_CONNECTION_STRING` using `${APPLICATIONINSIGHTS_CONNECTION_STRING=}` (empty default so startup succeeds without export when not set). Telemetry is disabled when the value is absent or `TELEMETRY_ENABLED=false`.

Both runtimes share the same Log Analytics workspace and Application Insights component. Signals are isolated by `AppRoleName`.

### Privacy invariants

- No prompt text, card content, art prompts, or generated text in any telemetry field.
- No session IDs, user identifiers, tenant IDs, or blob keys in metrics or traces.
- Exception events retain only a normalised exception type; message and stack are excluded before export.
- `X-Request-ID` is excluded from metrics dimensions (diagnostic headers are bounded and sanitised elsewhere).
- IP masking remains enabled on the shared Application Insights component.

## Alert definitions

Alerts are deployed by `deployments/card-orchestrator/infra/modules/agent-monitoring.bicep` and owned by the agent deployment. They are **disabled by default** (`enableAlerts=false`). Alert rules become active only when `CARD_ORCHESTRATOR_ENABLE_AGENT_ALERTS=true` **and** at least one receiver is configured.

To activate after an approved rollout:

```bash
azd env set CARD_ORCHESTRATOR_ENABLE_AGENT_ALERTS true
azd env set CARD_ORCHESTRATOR_ALERT_EMAIL_RECEIVERS '[{"name":"operations","emailAddress":"approved@example.com","useCommonAlertSchema":true}]'
# cd deployments/card-orchestrator && python deploy.py provision --execute --approve-change
```

| Alert | Window | Trigger | Severity |
|---|---|---|---|
| Invocation adverse outcomes | 15 min | 3 failures / throttles / timeouts | 1 (critical) |
| Model provider throttling (429) | 15 min | 3 throttle events | 1 (critical) |
| Runtime exception burst | 15 min | 5 exceptions | 1 (critical) |
| Container restart / unhealthy | 15 min | 3 events | 1 (critical) |

These are initial thresholds. Revisit after two weeks of representative traffic in dev.

### Signals not available via Azure native metrics

**Hosted-version heartbeat**: the `/readiness` endpoint (port 8088) is Foundry-internal and not reachable from Application Insights availability test locations. Operational readiness is inferred from invocation outcome metrics. A direct readiness probe can be run by the operator as described in the synthetic probe section below.

**Agent deployment version**: `CARD_ORCHESTRATOR_VERSION` is an ACA container environment variable, not an App Insights dimension. Version correlation is done via `fcg.agent_version` in `AppMetrics` if the orchestrator emits it, or via the `AppTraces` `service.version` resource attribute set from `CARD_ORCHESTRATOR_VERSION`.

**Model capacity exhaustion**: detect via `AppDependencies` `ResultCode == "429"` on the text model dependency. Azure model quota counters are not exported to App Insights.

## Dashboard / workbook

The Bicep module deploys an Azure Workbook per environment: `card-orchestrator DEV Agent Operations` and `card-orchestrator PROD Agent Operations`. Views:

- **Invocation outcomes**: `fcg.generation.requests` by outcome code and version
- **Stage latency**: `fcg.generation.duration` by stage (concept / lore / art_direction)
- **Model dependencies**: `AppDependencies` calls, failures, and 429 throttles
- **Exceptions and errors**: `AppExceptions` (type only) and `AppTraces` (severity ≥ warning)
- **Moderation signals**: `fcg.moderation.decisions` by stage and reason
- **ACA health**: `ContainerAppSystemLogs_CL` restart and unhealthy events

All KQL queries filter on `AppRoleName == 'card-orchestrator'`. No prompt, card text, art prompt, identity, or raw content field is selected.

## Synthetic readiness probe

Because `/readiness` is not publicly accessible, operators use the ACA identity probe tool to confirm hosted-version liveness without sending a model request:

```bash
python deployments/card-orchestrator/aca_identity_probe.py \
  --environment dev --subscription "<dev-subscription-id>" \
  --resource-group "<dev-resource-group>" --app "<aca-app-name>" \
  --revision "<ready-revision>" --replica "<running-replica>" --container web \
  --project-endpoint "https://<account>.services.ai.azure.com/api/projects/<project>" \
  --expected-principal "<ACA-system-principal-id>"
# Add --execute after verifying the plan output.
```

This verifies the managed-identity token, project access, and agent endpoint without invoking a model. Success requires `status: access_verified` and HTTP 200 from the agents list endpoint.

## Independent rollout procedure

All steps use `deployments/card-orchestrator/` as the working directory. The web ACA application is deployed independently via the root `azure.yaml`. **Never run both in the same `azd up` invocation.**

### Pre-rollout gates

```bash
# 1. Record existing web app state (do not modify).
az containerapp show --name "<web-app-name>" --resource-group "<rg>" \
  --query "{revision:properties.latestRevisionName, image:properties.template.containers[0].image, traffic:properties.configuration.ingress.traffic}" \
  --output json

# 2. Verify model capacity is sufficient for three sequential calls.
az cognitiveservices account deployment show \
  --name "<ai-account>" --resource-group "<rg>" \
  --deployment-name "gpt-5-5" \
  --query "{capacity:sku.capacity, rpmLimit:properties.rateLimits[?key=='request'].count | [0]}" \
  --output json
# Dev minimum: capacity 10 (10 RPM / 10K TPM). See infra/text-model-capacity.bicep.

# 3. Run offline packaging tests.
uv run python -m pytest tests/test_hosted_agent_deployment_config.py tests/test_agent_operational_monitoring.py -v
```

### Rollout

```bash
# Build and deploy an immutable tagged version.
export CARD_ORCHESTRATOR_VERSION="$(git rev-parse HEAD)"
export AZURE_DEV_USER_AGENT="microsoft_foundry_skill"

cd deployments/card-orchestrator
python deploy.py preview          # review the azd command
python deploy.py deploy --execute --approve-change
```

The `deploy` action runs `azd deploy card-orchestrator`. This builds and pushes a tagged image (`card-orchestrator:${CARD_ORCHESTRATOR_VERSION}`) and activates a new hosted version. The hosted version number is assigned by Foundry; record it from the deploy output.

### Post-rollout health gates

```bash
# 1. Verify hosted version is active (replace VERSION with the Foundry-assigned hosted version number).
az ai foundry agent version show \
  --project "<project-endpoint>" \
  --agent "card-orchestrator" --version VERSION 2>/dev/null || echo "Use Foundry CLI or SDK"

# 2. Run ACA identity probe (no model call).
python deployments/card-orchestrator/aca_identity_probe.py \
  --environment dev ... --execute

# 3. Confirm web app is unchanged.
az containerapp show --name "<web-app-name>" --resource-group "<rg>" \
  --query "{revision:properties.latestRevisionName, image:properties.template.containers[0].image, traffic:properties.configuration.ingress.traffic}" \
  --output json
# Compare with pre-rollout snapshot; values must be identical.

# 4. Confirm /healthz on the web app.
curl -sf "https://<web-app-fqdn>/healthz" | jq .status

# 5. Check workbook: zero adverse invocations; no 429s; no container restarts.
```

Only treat the rollout as successful when all gates pass.

### Version traffic / activation semantics

Foundry hosted agents use single-version activation. Deploying a new version makes it the active version for all new sessions; there is no gradual traffic split at the Foundry layer. Rolling back to a previous version requires deleting the current version and re-deploying (or re-activating) the prior image. If both dev and prod environments exist, each has its own hosted version namespace; a dev rollback does not affect prod.

### Rollback procedure

**Rollback does not happen automatically.** An operator must execute the following steps:

```bash
# Step 1: Identify and record the exact hosted version to remove.
# (Replace VERSION with the numeric version assigned during deploy.)
export FOUNDRY_PROJECT_ENDPOINT="https://<account>.services.ai.azure.com/api/projects/<project>"
export AZURE_DEV_USER_AGENT="microsoft_foundry_skill"

# Step 2: Stop and delete the exact owned version.
# Use the Foundry SDK, azd agent commands, or REST API.
# Verify ownership before deletion: the version tag must match CARD_ORCHESTRATOR_VERSION.
#
# REST example (requires operator Bearer token):
#   DELETE <FOUNDRY_PROJECT_ENDPOINT>/agents/versions/VERSION?api-version=2025-11-15-preview
#
# Python SDK example (uses existing ACA managed identity):
#   from azure.ai.projects import AIProjectClient
#   client = AIProjectClient(endpoint=FOUNDRY_PROJECT_ENDPOINT, credential=ManagedIdentityCredential())
#   client.agents.delete_agent_version("card-orchestrator", VERSION)

# Step 3: Verify absence.
# GET <FOUNDRY_PROJECT_ENDPOINT>/agents/versions/VERSION  → must return 404

# Step 4: If a known-good previous version is available, re-deploy it.
export CARD_ORCHESTRATOR_VERSION="<prior-sha>"
cd deployments/card-orchestrator
python deploy.py deploy --execute --approve-change

# Step 5: Confirm web app UNCHANGED (image, revision, traffic identical to pre-rollout snapshot).
az containerapp show --name "<web-app-name>" --resource-group "<rg>" \
  --query "{revision:properties.latestRevisionName, image:properties.template.containers[0].image, traffic:properties.configuration.ingress.traffic}" \
  --output json
```

If there is no known-good previous version, the agent is simply absent until a new image is reviewed and deployed. The web app continues to serve traffic independently.

### What rollback does NOT do

- It does NOT alter the web ACA container image, revision, traffic weights, or environment variables.
- It does NOT remove model deployments or capacity changes.
- It does NOT revoke the managed identity's Foundry agent consumer role.
- It does NOT affect other hosted agent versions (if any).

## Monitoring deployment procedure

Agent monitoring resources (workbook, alerts, action group) are provisioned by:

```bash
cd deployments/card-orchestrator
python deploy.py provision --execute --approve-change
```

This runs `azd provision` scoped to `deployments/card-orchestrator/infra/`. Monitoring resources are created only when `AZURE_LOG_ANALYTICS_WORKSPACE_RESOURCE_ID` and `AZURE_APP_INSIGHTS_RESOURCE_ID` are set (populated from root infra outputs). These are optional; if absent, the monitoring module is skipped (`= if (!empty(...))`).

To populate them from root infra:

```bash
# Run from repo root after root infra is provisioned:
azd env get-values | grep -E "AZURE_LOG_ANALYTICS_WORKSPACE_RESOURCE_ID|AZURE_APP_INSIGHTS_RESOURCE_ID|APPLICATIONINSIGHTS_CONNECTION_STRING"
# Copy values into the card-orchestrator azd environment:
cd deployments/card-orchestrator
azd env set AZURE_LOG_ANALYTICS_WORKSPACE_RESOURCE_ID "<value-from-root>"
azd env set AZURE_APP_INSIGHTS_RESOURCE_ID "<value-from-root>"
azd env set APPLICATIONINSIGHTS_CONNECTION_STRING "<value-from-root>"
```

Alert activation requires separate approval. Enable only after a workload review confirms thresholds are appropriate for observed traffic.

## SLO baseline

No contractual SLO is defined in this rollout. The alert thresholds are operational baselines to be revised after two weeks of representative traffic. Current capacity (10 RPM / 10K TPM in dev) supports approximately 3–4 concurrent bounded orchestrations. Do not deploy more concurrent workloads than the capacity allows without first reviewing and approving a quota increase.

## Validation evidence

See `docs/foundry-agent-operations.md` for the 2026-09-09 successful ACA-MI E2E run at commit `669899d23701983fc0a540ad93d932fee688577e`. That run proved:

- Foundry hosted version deploys and activates within ~60 seconds.
- ACA managed-identity token matches the expected principal.
- Invocation returns HTTP 200 with a completed, schema-valid card result.
- Cleanup (stop session, delete session, delete hosted version) completes within ~20 seconds.
- Web ACA image, revision, traffic, and persisted endpoint are unchanged after agent lifecycle.
