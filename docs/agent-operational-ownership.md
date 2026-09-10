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

This revision is independently owned by Gandalf after reviewer rejection. The normal
operational role assignment does not permit a rejected revision author to change this
artifact during the active review cycle.

## Implemented telemetry contract

Foundry project monitoring injects the reserved
`APPLICATIONINSIGHTS_CONNECTION_STRING` into hosted containers. It is not declared in
`azure.yaml`, written to azd state by this deployment, printed by a runbook command, or
copied between environments. Root Bicep creates account- and project-level
`AppInsights` connections to the existing workspace-based Application Insights
resource. The hosted manifest explicitly sets only non-secret
`TELEMETRY_ENABLED=true`, the environment name, and the experimental
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
to these measurements. The strict request boundary and no-response-store behavior are
unchanged. Framework payload instrumentation and SDK payload-bearing logs remain
disabled.

The single custom version dimension is `fcg.agent_version`. In Foundry it uses the
platform-injected hosted version; local tests fall back to the immutable image
candidate version. `service.version` follows the same precedence.

## Monitoring and alerts

`deployments/card-orchestrator/infra/modules/agent-monitoring.bicep` deploys one
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

Monitoring resource IDs are mandatory. Provisioning fails during parameter resolution
if either root output is absent:

```bash
# Root project: provision the App Insights resource and Foundry linkage first.
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd provision --no-prompt

# Copy only non-secret ARM resource IDs into the dedicated agent azd environment.
azd env get-value AZURE_LOG_ANALYTICS_WORKSPACE_RESOURCE_ID
azd env get-value AZURE_APP_INSIGHTS_RESOURCE_ID

cd deployments/card-orchestrator
azd env set AZURE_LOG_ANALYTICS_WORKSPACE_RESOURCE_ID "<workspace-resource-id>"
azd env set AZURE_APP_INSIGHTS_RESOURCE_ID "<app-insights-resource-id>"
python deploy.py preview --execute
python deploy.py provision --execute --approve-change
```

For production, there is no implicit fallback from dev and no prod default:

```bash
cd deployments/card-orchestrator
python deploy.py preview --environment prod --execute --approve-prod
python deploy.py provision --environment prod --execute --approve-change --approve-prod
```

Review the Bicep what-if and validate the environment-specific IDs before either
command. Never reuse dev monitoring IDs for prod.

## Independent rollout

Run all agent commands from `deployments/card-orchestrator`. Set
`AZURE_DEV_USER_AGENT=microsoft_foundry_skill` inline for every azd command.

### Pre-rollout gates

1. Record the commit, immutable image candidate tag, current agent/version list, and
   endpoint selector.
2. Record the public web ACA revision, image, environment variables or configuration
   hash, and traffic weights.
3. Confirm mandatory monitoring IDs resolve and project monitoring is connected.
4. Confirm model capacity and alert routing approval.
5. Run offline tests and Bicep compilation.

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

cd deployments/card-orchestrator
python deploy.py deploy
python deploy.py deploy --execute --approve-change

AZURE_DEV_USER_AGENT=microsoft_foundry_skill \
  azd ai agent show --output json > "<evidence-directory>/agent-after.json"
AZURE_DEV_USER_AGENT=microsoft_foundry_skill \
  azd ai agent endpoint show --output json > "<evidence-directory>/endpoint-after.json"
```

For a deliberately approved production rollout, add `--environment prod
--approve-prod` to both launcher commands and add `--approve-prod` to the production
managed-identity probe.

Run exactly one bounded target-managed-identity invocation against the new hosted
version, using the pinned ACA revision/replica and the expected immutable candidate:

```bash
python aca_identity_probe.py \
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
pre-rollout snapshot. The agent rollout is rejected if web image, revision,
configuration, identity, or traffic changed.

## Restore-first rollback

The stable agent endpoint supports one 100% version-selection rule. Rollback therefore
selects the prior known-good active version first, verifies service restoration, and
only then considers deleting the bad version. Never delete the serving version before
restoration.

Set the data-plane variables:

```bash
ACCOUNT_NAME="<foundry-account>"
PROJECT_NAME="<foundry-project>"
AGENT_NAME="card-orchestrator"
BASE_URL="https://${ACCOUNT_NAME}.services.ai.azure.com/api/projects/${PROJECT_NAME}"
API_VERSION="v1"
RESOURCE="https://ai.azure.com"
PRIOR_VERSION="<known-good-hosted-version>"
PRIOR_CANDIDATE="<known-good-git-sha>"
BAD_VERSION="<failed-hosted-version>"
```

### Abort and no-op gates

```bash
test -n "$PRIOR_VERSION" && test -n "$BAD_VERSION"
test "$PRIOR_VERSION" != "$BAD_VERSION"

az rest --method GET \
  --url "${BASE_URL}/agents/${AGENT_NAME}/versions/${PRIOR_VERSION}?api-version=${API_VERSION}" \
  --resource "$RESOURCE" --output json \
  > "<evidence-directory>/prior-version.json"

az rest --method GET \
  --url "${BASE_URL}/agents/${AGENT_NAME}?api-version=${API_VERSION}" \
  --resource "$RESOURCE" --output json \
  > "<evidence-directory>/agent-before-rollback.json"
```

Abort if the prior version is not `active`, if its immutable candidate cannot be
matched to approved evidence, or if the endpoint already selects the prior version.
The last case is a no-op: verify health and do not issue another selector update.

### Select the prior version

```bash
az rest --method PATCH \
  --url "${BASE_URL}/agents/${AGENT_NAME}?api-version=${API_VERSION}" \
  --resource "$RESOURCE" \
  --headers "Content-Type=application/merge-patch+json" \
  --body "{
    \"agent_endpoint\": {
      \"version_selector\": {
        \"version_selection_rules\": [
          {
            \"type\": \"FixedRatio\",
            \"agent_version\": \"${PRIOR_VERSION}\",
            \"traffic_percentage\": 100
          }
        ]
      }
    }
  }"
```

Equivalent Python SDK operation for `azure-ai-projects>=2.3.0`:

```python
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    AgentEndpointConfig,
    FixedRatioVersionSelectionRule,
    VersionSelector,
)
from azure.identity import DefaultAzureCredential

project_client = AIProjectClient(
    endpoint=BASE_URL,
    credential=DefaultAzureCredential(),
)
project_client.agents.update_details(
    agent_name=AGENT_NAME,
    agent_endpoint=AgentEndpointConfig(
        version_selector=VersionSelector(
            version_selection_rules=[
                FixedRatioVersionSelectionRule(
                    agent_version=PRIOR_VERSION,
                    traffic_percentage=100,
                )
            ]
        )
    ),
)
```

### Verify restoration before cleanup

```bash
az rest --method GET \
  --url "${BASE_URL}/agents/${AGENT_NAME}?api-version=${API_VERSION}" \
  --resource "$RESOURCE" --output json \
  > "<evidence-directory>/agent-after-selector.json"

AZURE_DEV_USER_AGENT=microsoft_foundry_skill \
  azd ai agent endpoint show --output json \
  > "<evidence-directory>/endpoint-after-selector.json"
```

Verify exactly one `FixedRatio` rule selects `PRIOR_VERSION` at 100%, then run the
same one-invocation ACA managed-identity smoke with:

```text
--hosted-version "$PRIOR_VERSION"
--expected-version "$PRIOR_CANDIDATE"
```

Confirm the smoke reports `invocation_verified`, clean up only its owned session, and
prove the web ACA state still matches `web-before.json`.

### Optional bad-version deletion

Deletion is not rollback and is never required for service restoration. Perform it
only after the selector, managed-identity smoke, telemetry, and web immutability gates
all pass and the change owner approves cleanup:

```bash
az rest --method DELETE \
  --url "${BASE_URL}/agents/${AGENT_NAME}/versions/${BAD_VERSION}?api-version=${API_VERSION}" \
  --resource "$RESOURCE"
```

Correct Python SDK method:

```python
project_client.agents.delete_version(
    agent_name=AGENT_NAME,
    agent_version=BAD_VERSION,
)
```

The repository-pinned `azure.ai.agents` extension also exposes:

```bash
AZURE_DEV_USER_AGENT=microsoft_foundry_skill \
  azd ai agent delete "$AGENT_NAME" --version "$BAD_VERSION" --no-prompt
```

Before deletion, re-read the agent selector and abort if `BAD_VERSION` is serving.
After deletion, GET only that version and expect not found; do not delete the agent
object or any unowned session/version.

## Residual limitations

- Foundry-hosted internal readiness cannot be monitored by a public availability
  test. The bounded managed-identity invocation is an operator procedure, not a
  recurrent platform heartbeat.
- Alert thresholds are initial operational baselines and require tuning after
  representative dev traffic.
- Project monitoring linkage is deployed by root Bicep. Deploying only the dedicated
  agent infra against an unrelated pre-existing project does not create that linkage;
  operators must first verify the project has the intended Application Insights
  connection.

Authoritative platform references:

- [Export hosted agent telemetry](https://learn.microsoft.com/azure/foundry/agents/how-to/configure-hosted-agent-telemetry)
- [Configure hosted agent environment variables](https://learn.microsoft.com/azure/foundry/agents/how-to/configure-hosted-agent-env-variables)
- [Manage hosted agents](https://learn.microsoft.com/azure/foundry/agents/how-to/manage-hosted-agent)
