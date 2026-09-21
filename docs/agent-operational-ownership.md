# Agent operational ownership — card-orchestrator

This runbook defines operational ownership, monitoring, rollout, and rollback for the
Foundry-hosted `card-orchestrator`. Hosted-agent versions and sessions are independent
of the public web Container App. No Azure deployment or model invocation was performed
while implementing this runbook.

## Ownership

| Concern | Accountable owner | Required evidence |
|---|---|---|
| Hosted runtime and version routing | DevOps, with Lead approval for production | Agent/version JSON, endpoint selector, bounded smoke result |
| Agent monitoring and alert routing | DevOps | Bicep what-if, workbook query results, Action Group test |
| Public web ACA | Web operations | Before/after revision, image, configuration, and traffic snapshot |
| Model capacity | AI platform operations | Deployment capacity and throttle evidence |
| Privacy boundary | Backend and RAI reviewers | Metric schema review and content-exclusion tests |
| Cross-runtime incident decision | Lead / Architect | Incident timeline and rollback decision |

## Implemented telemetry contract

Foundry project monitoring injects the reserved
`APPLICATIONINSIGHTS_CONNECTION_STRING` into hosted containers. It is not declared in
`azure.yaml`, written to azd state by this deployment, printed by a runbook command, or
copied between environments. Root Bicep creates account- and project-level
`AppInsights` connections to the existing workspace-based Application Insights
resource. The hosted manifest explicitly sets only non-secret
`TELEMETRY_ENABLED=true`, `AGENT_TRACE_ENABLED` (default `true`), the environment
name, and the experimental
`AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING=true` SDK tracing opt-in.

Foundry fixes `service.name` to the agent name, so `AppRoleName ==
"card-orchestrator"` is the service boundary. `OTEL_SERVICE_NAME` is not configured
because Foundry ignores overrides for hosted agents.

The runtime emits these custom metrics:

| Metric | Bounded dimensions |
|---|---|
| `fcg.generation.requests` | operation, outcome, agent version |
| `fcg.generation.duration` | operation, outcome, agent version |
| `fcg.dependency.attempts` | stage, provider, attempt bucket, outcome, error code, retryable, agent version |
| `fcg.dependency.duration` | same as dependency attempts |
| `fcg.dependency.throttles` | provider |
| `fcg.dependency.timeouts` | provider |
| `fcg.moderation.decisions` | stage, allowed/blocked outcome, reason, policy, agent version |

Generation outcomes are one of `completed`, `held`, `refused`, `routing_defer`,
`throttled`, `timed_out`, or `failed`. Stages, providers, reasons, error codes, and
versions pass through closed allowlists or bounded identifier validation.

The runtime never adds prompts, responses, card fields, art prompts, user or session
identifiers, tokens, URLs, endpoints, exception messages, or arbitrary caller values
to these measurements. Named HOSTED execution spans also remain content-free.
The approved #159 r1 exception is limited to bounded sanitized candidates in private,
optional, independently versioned response metadata, released as **WEB-owned linked
detail only after terminal full business success**. Every candidate needs its own
privacy/safety acceptance; a safe final card does not approve rejected intermediates.
All later refusals, partial/held/deferred/unknown outcomes, errors, cancellations,
and validation, image, persistence or response failures suppress all content.
There is no UI, endpoint, business-response field, or card/audit persistence.

The strict business request/response boundary is unchanged. Framework payload
instrumentation and SDK payload-bearing logs remain disabled in both flag states.
`store:false` / `NoResponseStore` do not prove provider non-retention of internally
transported metadata before WEB's decision. Standalone HOSTED calls export no
diagnostic content; platform non-persistence and forwarding evidence remain
separately authorized delivery gates, not claims of this implementation.

See [the exact privacy, flag and budget contract](operational-monitoring.md#privacy-exclusions-and-approved-159-r1-exception)
for the four mixed runtime states, closed versions/statuses, actual trusted
application instruction (shared rules + stage + schema), UTF-8 limits and residual
PII risk. Oversized structured projections omit their entire payload, never
partial JSON. Redacted or omitted input/output projections carry
`validation='modified'`, not `validation='validated'`.
Both runtimes parse trimmed case-insensitive true/false at startup,
default ON when unset and reject invalid/empty values. OFF suppresses only new
process-local detail spans/capture-only work; baseline spans/metrics/safe errors,
`TELEMETRY_ENABLED`, mandatory `configure_telemetry()` startup and experimental
GenAI opt-in semantics are unchanged. It does not control platform `invoke_agent`.

The single custom version dimension is `fcg.agent_version`. In Foundry it uses the
platform-injected hosted version; local tests fall back to the immutable image
candidate version. `service.version` follows the same precedence.

## Monitoring and alerts

The authoritative root `infra/modules/agent-monitoring.bicep` deploys one
environment-isolated workbook, Action Group, and four scheduled-query alerts:

| Alert | Window | Default threshold | Source |
|---|---:|---:|---|
| Adverse request outcomes | 15 min | 3 | `fcg.generation.requests` |
| Model throttling | 15 min | 3 | `fcg.dependency.attempts`, `foundry_text`, `throttled` |
| Model failures/timeouts | 15 min | 3 | `fcg.dependency.attempts`, `failed` or `timed_out` |
| Container restart/unhealthy events | 15 min | 3 | `ContainerAppSystemLogs_CL` |

Alert rules are disabled unless both the explicit alert switch and at least one
approved receiver are present. The workbook filters on service and
`fcg.agent_version`; the environment is fixed by the isolated deployment. Histogram
averages use `sum(Sum) / sum(ItemCount)`.

There is no fabricated heartbeat alert. Foundry does not expose the internal
`/readiness` endpoint to Azure availability tests. Operators use the bounded managed
identity smoke below; absence of traffic alone is not treated as runtime failure.

The root stack passes its monitoring outputs directly to the agent monitoring
module; do not copy IDs into a second azd environment or provision a duplicate
stack. All commands below are future, separately approved operator procedures,
not actions authorized by implementation approval.

```bash
# Repository root, selected dev environment.
./deploy.sh preview --environment dev
./deploy.sh provision --environment dev --approve-change
```

For production, there is no implicit fallback from dev and no prod default:

```bash
./deploy.sh preview --environment prod --approve-prod
./deploy.sh provision --environment prod --approve-change --approve-prod
```

Review the Bicep what-if and validate the environment-specific IDs before either
command. Never reuse dev monitoring IDs for prod.

## Independent rollout

Run all deployment commands from the repository root using root `azure.yaml`.
The nested manifest/launcher under `deployments/card-orchestrator` are deprecated
references, not supported entrypoints. Set
`AZURE_DEV_USER_AGENT=microsoft_foundry_skill` inline for every azd command.

### Pre-rollout gates

1. Record the commit, immutable image candidate tag, current agent/version list, and
   endpoint selector.
2. Record the public web ACA revision, image, environment variables or configuration
   hash, and traffic weights.
3. Confirm mandatory monitoring IDs resolve and project monitoring is connected.
4. Confirm model capacity and alert routing approval; satisfy the diagnostic
   access/retention/export/deletion and retry-peak ingestion gates in
   [operational monitoring](operational-monitoring.md#rollout-access-retention-and-deletion-gates).
5. Run offline tests and Bicep compilation.
6. Obtain separate authorization for platform non-persistence, SDK suppression,
   cross-runtime correlation and synthetic terminal-success/refusal verification.
   Local injection tests are not evidence of Foundry forwarding. Failure returns
   to intake; no new resources/grants/retention changes are approved by #159.

```bash
git rev-parse HEAD
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd ai agent show --output json
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd ai agent endpoint show --output json

az containerapp show --name "<web-app>" --resource-group "<resource-group>" \
  --query "{revision:properties.latestRevisionName,image:properties.template.containers[0].image,configuration:properties.configuration,traffic:properties.configuration.ingress.traffic}" \
  --output json > "<evidence-directory>/web-before.json"
```

The evidence directory must be access-controlled and immutable under the operator's
incident/change process; it is not committed to this repository.

### Deploy and verify

```bash
export CARD_ORCHESTRATOR_VERSION="$(git rev-parse HEAD)"
AZURE_DEV_USER_AGENT=microsoft_foundry_skill \
  azd env set CARD_ORCHESTRATOR_VERSION "$CARD_ORCHESTRATOR_VERSION"
./deploy.sh agent
./deploy.sh agent --approve-change
# Inject the newly stamped exact hosted version while preserving the web image.
./deploy.sh provision --approve-change

AZURE_DEV_USER_AGENT=microsoft_foundry_skill \
  azd ai agent show --output json > "<evidence-directory>/agent-after.json"
AZURE_DEV_USER_AGENT=microsoft_foundry_skill \
  azd ai agent endpoint show --output json > "<evidence-directory>/endpoint-after.json"
```

For a deliberately approved production rollout, add `--environment prod
--approve-prod` to the root launcher commands, select the same environment for
azd settings/inspection, and add `--approve-prod` to the production
managed-identity probe.

Run exactly one bounded target-managed-identity invocation against the new hosted
version, using the pinned ACA revision/replica and the expected immutable candidate:

```bash
python deployments/card-orchestrator/aca_identity_probe.py \
  --environment dev \
  --subscription "<subscription-id>" \
  --resource-group "<resource-group>" \
  --app "<web-app>" \
  --revision "<recorded-web-revision>" \
  --replica "<running-replica>" \
  --container web \
  --project-endpoint "https://<account>.services.ai.azure.com/api/projects/<project>" \
  --expected-principal "<web-aca-system-identity-principal-id>" \
  --invoke-once \
  --hosted-version "<new-hosted-version>" \
  --expected-version "$CARD_ORCHESTRATOR_VERSION" \
  --session-id "<owned-bounded-session-id>" \
  --require-persisted-endpoint \
  --execute
```

The probe must report `invocation_verified`, one invocation attempted, a schema-valid
response, and the expected version. Complete the probe's owned session cleanup. Then
capture the web ACA state again and compare it byte-for-byte or structurally with the
pre-rollout snapshot. The web image, identity and traffic must be preserved.
A new WEB revision and the intended stamped agent configuration are expected
after root provision; reject unrelated configuration changes. An agent-only
deploy without the matching WEB configuration provision can fail readiness.

### Detail configuration rollout

`AGENT_TRACE_ENABLED` defaults ON for dev and prod through root hosted service
substitution and WEB azd/Bicep parameters. Bicep passes the resolved string
unchanged. Root preprovision, prepackage, prepublish and predeploy hooks validate
the raw azd-injected key with `hooks/validate_agent_trace.py` before deployment
can consume the default. Unset keeps ON; trimmed case-insensitive true/false pass
unchanged; empty, whitespace-only and invalid values stop both supported deployment
paths with a content-free error. The guard reads only that key, not environment
files or bulk azd output. See
[operational monitoring](operational-monitoring.md#startup-setting-and-process-local-off).
Use explicit `false`, never an empty setting, to opt out.
No request or configuration-state edit reloads a running process.

For a separately approved diagnostic rollback, set the shared value to `false`
in the selected environment, then deploy the intended immutable hosted artifact
and provision WEB configuration using the root procedure above. HOSTED needs a
new version/restart with that environment; WEB needs its configuration revision
and restart. A mere web image deploy does not apply Bicep parameter changes.
Verify the effective value in **both** processes, their exact artifact/version
binding, and baseline monitoring health. Restore ON using the same procedure
only after the privacy/operations gates are satisfied.

Mixed deployments are not globally OFF: WEB false suppresses its processing and
release but HOSTED true can still capture/transport private candidates; HOSTED
false with WEB true permits WEB-only detail. Neither setting disables platform
`invoke_agent` or erases exported data. Do not set `TELEMETRY_ENABLED=false` or
bypass mandatory hosted initialization to suppress detail.

Current 30-day retention, 0.25 GB/day shared cap and 100% sampling are baseline
defaults, not consent to content retention. Confirm actual readers/inherited RBAC,
downstream exports, deletion and cost headroom before rollout. Card/account deletion
does not remove operational telemetry automatically. Sanitization/truncation is
explicit, bounded and not a universal PII guarantee.

## Restore-first rollback

The web runtime has no legacy/direct text path to enable during an incident.
Because readiness requires the configured `FOUNDRY_AGENT_VERSION` to equal the
endpoint's active 100% selector, changing only the endpoint selector is not a
valid rollback: it deliberately makes the web revision unready.

Restore service by deploying the last approved agent artifact as a **new
immutable Foundry version**, then stamp and provision that new platform version:

1. Record the current web revision/image, current endpoint selector, failed
   Foundry version, and the last approved agent commit, image digest, and
   configuration. Abort if the approved artifact cannot be proven.
2. In a separate clean checkout of the approved commit, select the same azd
   environment and set `CARD_ORCHESTRATOR_VERSION` to that approved commit.
   This explicit, bounded value is required because Foundry's platform outputs
   expose the immutable hosted version but not the application metadata inside
   the artifact.
3. Run the guarded targeted deployment:

   ```bash
   AZURE_DEV_USER_AGENT=microsoft_foundry_skill \
     azd deploy card-orchestrator --environment "<environment>" --no-prompt
   ```

   Foundry creates and activates a new immutable platform version. The
   `postdeploy` hook validates the new platform outputs and the selected
   rollback artifact before atomically writing `FOUNDRY_AGENT_NAME`,
   `FOUNDRY_AGENT_VERSION`, and `FOUNDRY_AGENT_EXPECTED_VERSION`. If the
   artifact value is absent or malformed, the hook fails before changing any of
   those three azd values.
4. From the repository root, run:

   ```bash
   AZURE_DEV_USER_AGENT=microsoft_foundry_skill \
     azd provision --environment "<environment>" --no-prompt
   ```

   The preprovision hook reads the currently deployed `web-nat` image and
   passes it as `CONTAINER_IMAGE`. Bicep updates the stamped agent version and
   creates the necessary Container App configuration revision without building,
   pushing, or replacing the web image.
5. Verify `azd ai agent show --output json` reports the newly created version
   active and the endpoint selector routes 100% to it. Verify the azd
   `FOUNDRY_AGENT_VERSION` value matches and
   `FOUNDRY_AGENT_EXPECTED_VERSION` equals the approved application build SHA.
   Then verify `/healthz` returns 200 and one bounded ACA managed-identity smoke
   reports `invocation_verified` with that same approved SHA.
6. Confirm the web image digest is unchanged. The Container App revision may
   change because its `FOUNDRY_AGENT_VERSION` environment value changed; the
   web image is not rebuilt or redeployed, model deployments are not changed,
   and the old Foundry platform version is not reactivated in place.

Do not use an endpoint-selector-only patch, invent an `azd rollback` command,
or restore a direct text fallback. Retain failed and prior versions until the
new version, readiness, telemetry, and smoke checks pass. Version deletion is
optional cleanup after approval and is never part of service restoration.

## Residual limitations

- Foundry-hosted internal readiness cannot be monitored by a public availability
  test. The bounded managed-identity invocation is an operator procedure, not a
  recurrent platform heartbeat.
- Alert thresholds are initial operational baselines and require tuning after
  representative dev traffic.
- Project monitoring linkage is deployed by root Bicep; the deprecated nested
  agent infra is not a supported alternative entrypoint.
- #159 implementation approval is not rollout approval. No live platform
  non-persistence, payload-suppression or forwarding validation was performed for
  this change; these remain delivery gates alongside approved content
  retention/access and baseline ingestion headroom.

Authoritative platform references:

- [Export hosted agent telemetry](https://learn.microsoft.com/azure/foundry/agents/how-to/configure-hosted-agent-telemetry)
- [Configure hosted agent environment variables](https://learn.microsoft.com/azure/foundry/agents/how-to/configure-hosted-agent-env-variables)
- [Manage hosted agents](https://learn.microsoft.com/azure/foundry/agents/how-to/manage-hosted-agent)
