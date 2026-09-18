# Infrastructure deployment notes

## Monitoring configuration

Each `dev`/`prod` deployment owns an isolated Log Analytics workspace,
workspace-based Application Insights component, workbook, availability test, Action
Group, and eight alert rules. The default workspace retention is 30 days, its daily
cap is 0.25 GB, and parent-consistent trace sampling is 100%.

The initial rollout is dashboard-only: receiver arrays are empty, the Action Group is
disabled, and `MONITORING_ALERTS_ENABLED=false`, so all eight rules deploy disabled.
Adding an approved receiver enables the Action Group. After the dashboard has been
calibrated for traffic below 100 requests/day, an operator can explicitly enable the
rules.

```powershell
azd env set MONITORING_RETENTION_DAYS 30
azd env set MONITORING_DAILY_QUOTA_GB 0.25
azd env set MONITORING_INGESTION_WARNING_PERCENT 80
azd env set TELEMETRY_SAMPLING_RATIO 1.0
azd env set MONITORING_ALERTS_ENABLED false
azd env set MONITORING_REQUEST_TRAFFIC_FLOOR 5
azd env set MONITORING_EMAIL_RECEIVERS '[]'
azd env set MONITORING_WEBHOOK_RECEIVERS '[]'
```

See [`../docs/operational-monitoring.md`](../docs/operational-monitoring.md) for
receiver object schemas, thresholds, supported workspace table names, KQL, privacy
rules, cost estimation, deployment verification, and rollback. No monitoring
resources are deployed by repository changes alone; deployment remains a separate,
explicitly authorized `azd` operation.

## `/healthz` dependency probe budget

Issue [#51](https://github.com/bmoussaud/fantasy-cards-generator/issues/51)
keeps dependency-aware `/healthz` checks bounded through IaC rather than portal drift.

- `HEALTHZ_COSMOS_TIMEOUT_MS` and `HEALTHZ_BLOB_TIMEOUT_MS` are plain azd/Bicep
  parameters, both defaulting to `1500` ms and both overrideable per environment.
- ACA startup and liveness call dependency-free `/livez`; startup allows up to
  150 seconds for local process initialization without waiting on Foundry.
  Readiness alone calls dependency-aware `/healthz` with a 100-second platform
  timeout, exceeding the supported 90-second Foundry health-check maximum by
  10 seconds of bounded request/handler overhead. The normal app default remains
  70 seconds, and the overall request budget remains 225 seconds. An upstream
  outage therefore removes the replica from traffic without causing a restart
  loop. Startup checks every 5s, readiness every 10s, and liveness every 30s.
- The app response contract keeps `Cache-Control: no-store`; do not add an
  external cache in front of `/healthz`, and do not tighten the probe interval
  below the existing ACA configuration unless RU / transaction impact is
  re-evaluated.

## Dev/MVP NAT Gateway baseline for Cosmos egress

Issue [#35](https://github.com/bmoussaud/fantasy-cards-generator/issues/35)
adds the current dev/MVP network baseline:

- a dedicated VNet
- a delegated `aca-infra` subnet for a workload-profile Container Apps environment
- a NAT Gateway with a static public IP on that subnet
- Cosmos DB `ipRules` sourced from the NAT public IP

This keeps the app on the normal Cosmos public FQDN with managed-identity
data-plane auth. It does **not** introduce a private endpoint, connection
string, or Cosmos keys.

> This is **dev/MVP only**. Production hardening belongs to
> [#37](https://github.com/bmoussaud/fantasy-cards-generator/issues/37), which
> upgrades the Cosmos trust model to subnet-scoped service-endpoint rules.

## Consolidated root entry point (issue #130)

The root `azure.yaml` is the single operator entry point for both the `web-nat`
Container App (port 8000) and the `card-orchestrator` Foundry hosted agent
(port 8088). `azd up` performs the complete agent-first bootstrap. Bare `azd deploy` is
**unsupported** for this manifest because azd 1.32 deploys all declared
services by default and no verified root-hook context distinguishes bare from
targeted deploys before service hooks run.

### Deploy guards

Three layers keep hosted-agent deployment explicit:

1. **`workflows.up`** — azd 1.32 supports overriding only the `up` workflow.
   Root `azd up` provisions shared resources, deploys the hosted agent, runs a
   second provision to inject the exact generated agent name/version, and then
   deploys `web-nat`. There is no supported `workflows.deploy` override.
2. **Service lifecycle hooks** — `hooks/guard_agent_deploy.sh` is registered as
   `prebuild`, `prepackage`, `prepublish`, and `predeploy` for
   `card-orchestrator`. These azd 1.32 service hooks run before the package →
   publish → deploy phases, blocking agent build/package/push/deploy unless
   `CARD_ORCHESTRATOR_ENABLE_PREREQUISITES=true` in the azd environment.
3. **Root orchestrator (`deploy.sh`)** — the documented production-safe entry
   point preserving `--approve-change` / `--approve-prod` enforcement gates.

### Clean environment bootstrap

```bash
azd env new dev
azd env set AZURE_LOCATION eastus2
azd env set CARD_ORCHESTRATOR_ENABLE_PREREQUISITES true
azd env set CARD_ORCHESTRATOR_CREATE_REGISTRY_CONNECTION true
azd up
```

Before provisioning, `hooks/ensure_agent_version.sh` initializes an unset
`CARD_ORCHESTRATOR_VERSION` from the current Git commit and persists it in the
selected azd environment. Existing values are validated and preserved so the
second provision cannot change the packaged artifact's identity. This is a
default, not automatic release advancement: explicitly set the intended commit
before packaging a new release or rollback. Without Git metadata, provide an
explicit version. Standalone agent package/deploy commands must have the value
set already; they do not run preprovision hooks.

The first provision uses the public placeholder image and omits both hosted-agent
environment variables. Agent deployment then writes
`AGENT_CARD_ORCHESTRATOR_NAME` and `AGENT_CARD_ORCHESTRATOR_VERSION`; the
`postdeploy` hook validates and persists them as `FOUNDRY_AGENT_NAME` and
`FOUNDRY_AGENT_VERSION` in the same azd transaction as the validated
`CARD_ORCHESTRATOR_VERSION`, stored as `FOUNDRY_AGENT_EXPECTED_VERSION`. The
second provision injects the complete triplet before the real web revision can
become ready. Any partial triplet fails Bicep validation.
Before every provision, `hooks/preserve_web_image.sh` reads the currently
deployed Container App image and stores it as `CONTAINER_IMAGE`; the Bicep
parameter consumes that exact value. Therefore repeat `azd provision` runs
preserve the running web artifact. A clean environment has no deployed image,
so the empty value intentionally selects the public bootstrap image until
`azd deploy web-nat` completes the first deployment.
The second provision reuses an existing `ENTRA_CLIENT_SECRET`; the
postprovision hook creates one only when the managed registration has no stored
secret. Clear that azd value only for an explicit, coordinated rotation.

When the resource group and app name are configured, the image-preservation
hook first checks that Azure CLI can obtain an Azure Resource Manager token.
If it cannot, the hook stops and asks you to run `az login` and retry.
Signing in with `azd auth login` alone does not authenticate `az`.
The check never starts an interactive login or prints the token. A successful
token check does not guarantee Container Apps permissions; image lookup
failures are still handled separately.

To inspect the current web image manually, replace the placeholders and keep
the entire JMESPath query in double quotes:

```bash
az containerapp list --resource-group <resource-group> --query "[?name=='<app-name>'].properties.template.containers[0].image | [0]" --output tsv
```

The hook already passes this query as one argument. `/bin/sh` xtrace output
(`set -x`) is not reliably copyable shell input: it can omit protective quotes.
Pasting an unquoted query makes the shell interpret `|` as a pipeline and try
to execute `[0]`, causing `[0]: command not found` and an invalid JMESPath
argument. Keep tracing disabled in the hook so resource and image values are
not logged.

### Existing environment deployment

```bash
# 1. Enable card-orchestrator deployment prerequisites (ACR pull, monitoring)
azd env set CARD_ORCHESTRATOR_ENABLE_PREREQUISITES true

# 2. Optionally create the registry connection
azd env set CARD_ORCHESTRATOR_CREATE_REGISTRY_CONNECTION true

# 3. Provision shared infrastructure, including mandatory agent RBAC
azd provision

# 4. Deploy card-orchestrator; postdeploy stamps its exact name/version
azd deploy card-orchestrator

# 5. Re-provision the Container App configuration, then deploy web
azd provision
azd deploy web-nat
```

Or via the root orchestrator with approval gates:

```bash
./deploy.sh provision --approve-change
./deploy.sh agent --approve-change
# For production:
./deploy.sh agent --environment prod --approve-change --approve-prod
```

### Targeted deployments

Both services can be deployed independently from the repository root:

- `azd deploy web-nat` — redeploys the web Container App only.
- `azd deploy card-orchestrator` — builds, pushes, and registers the hosted
  agent only after `CARD_ORCHESTRATOR_ENABLE_PREREQUISITES=true`; otherwise the
  service lifecycle hooks fail closed before package/publish/deploy.

Do not use bare `azd deploy` with this manifest. Supported entrypoints are
`./deploy.sh {web|agent|full|provision|preview}` and fully targeted
`azd deploy <service-name>` commands.

### Agent deployment variables

| Variable | Default | Purpose |
|---|---|---|
| `CARD_ORCHESTRATOR_VERSION` | Git HEAD when unset during root preprovision | Immutable application build identifier used for the agent image/tag; existing values are preserved |
| `AZURE_AI_MODEL_DEPLOYMENT_NAME` | Provisioned text deployment output | Required model deployment for the agent; exported from `aiFoundryTextDeploymentName` |
| `CARD_ORCHESTRATOR_ENABLE_PREREQUISITES` | `false` | Must be set to `true` before the first agent deployment |
| `CARD_ORCHESTRATOR_CREATE_REGISTRY_CONNECTION` | `false` | Set to `true` for a fresh project so Foundry can pull from ACR |
| `CARD_ORCHESTRATOR_ENABLE_AGENT_ALERTS` | `false` | Enable agent monitoring alert rules |

### Production approval — `deploy.sh`

The root `deploy.sh` script is the production-safe entry point, preserving the
`--approve-change` / `--approve-prod` enforcement semantics of the deprecated
Python launcher. It is plan-only by default: without `--approve-change`, it
prints the planned azd commands and exits without starting azd.

```bash
./deploy.sh web                                 # Plan only
./deploy.sh web --approve-change                # Execute web deploy (dev)
./deploy.sh agent --approve-change              # Execute agent deploy (dev)
./deploy.sh full --approve-change               # Execute both (dev)
./deploy.sh provision --approve-change          # Execute provision (dev)
./deploy.sh preview                             # Provision preview (always safe)
./deploy.sh web --environment prod --approve-change --approve-prod
```

**azd limitations:** The azd CLI (1.32.0) has no built-in `--approve-prod` or
`--approve-change` flags, and its schema supports `workflows.up` but not
`workflows.deploy`. `deploy.sh` provides approval enforcement as a thin wrapper.
The service-level `prebuild`/`prepackage`/`prepublish`/`predeploy`
hooks are the raw-azd guards that prevent build, package, push, and deploy
before prerequisite opt-in.
They also reject missing or malformed model deployment and application version
values before publishing. `sync_agent_deployment.sh` uses `az rest` to pin the
endpoint to azd's new `AGENT_CARD_ORCHESTRATOR_VERSION`, preserving the Responses
protocol and Entra authentication, before storing the web identity triplet.
The installed azd extension does not interpolate variables inside
`agentEndpoint`; dynamic version selection is therefore applied by the hook.
If pinning fails, web identity state is not updated. After every agent deployment,
the postdeploy hook must replace the manifest's initial `@latest` selector with
the concrete version before the new web configuration becomes ready.

### Deprecated files

- `deployments/card-orchestrator/azure.yaml` — superseded by root manifest.
  Retained as a legacy reference; do not use for new deployments.
- `deployments/card-orchestrator/deploy.py` — superseded by root `azd` commands.
  Retained as a legacy reference.

### Technical notes

The azd 1.32 service graph runs service phases as package → publish → deploy.
The v1.0 schema supports service-level `prebuild`, `prepackage`, `prepublish`,
and `predeploy` hooks, but only `workflows.up` at the workflow level. The
`azure.ai.agents` extension (1.0.0-beta.13) registers `azure.ai.agent` as a
service target provider. This has been verified against the installed azd 1.32.0
schema/source and extension capabilities, but not against a live Azure
deployment in this PR. A live provisioning preview should be reviewed before any
real deployment.

## Provisioning

Use the normal azd workflow:

```bash
azd env new dev
azd env set AZURE_LOCATION eastus2
azd env set CARD_ORCHESTRATOR_ENABLE_PREREQUISITES true
azd env set CARD_ORCHESTRATOR_CREATE_REGISTRY_CONNECTION true
azd env set LEGACY_COSMOS_IP_RULE 20.10.253.231
azd up
```

The text deployment capacity default is environment-specific: dev uses 10
`GlobalStandard` capacity units, while prod remains at 1 until separately
reviewed. For a dev-only repair that must not reprovision the application or
other shared resources, preview and deploy
[`text-model-capacity.bicep`](./text-model-capacity.bicep) at resource-group
scope. The leaf updates only the existing text deployment and pins its current
model, version, SKU, Responsible AI policy, and version-upgrade policy. It has
no target parameters: a deployment-time `fail()` guard requires the exact dev
subscription, `rg-fcag-dev`, and ARM deployment name
`dev-text-model-capacity-10`; the Foundry account and `gpt-5-5` deployment are
hard-bound in the template. A production or arbitrary resource-group target
fails before the resource update. Always run ARM `validate` and `what-if`, then
hand the result to a reviewer before applying it. The exact command sequence is
documented in
[`../docs/foundry-agent-operations.md`](../docs/foundry-agent-operations.md).

Root `infra/main.bicep` resolves `deployer().objectId` once from the ARM
deployment context and passes that Microsoft Entra object ID to the data and
Foundry modules **and** to the Key Vault security module. That same derived
object ID represents the signed-in interactive user for local `azd` runs and
the federated service principal for supported CI runs. Do not replace it with a
hard-coded object ID or application client ID.

`deployer()` does not expose the caller's principal type. `azd` therefore still
supplies `AZURE_PRINCIPAL_TYPE`, which must match the caller (`User` for an
interactive deployment or `ServicePrincipal` for the supported federated CI
case). A direct Bicep deployment that bypasses `azd` must provide
`deployerPrincipalType` explicitly.

No new azd environment variable is required for the deployer principal ID: the
template derives it directly from the authenticated deployment context via
`deployer().objectId`.

`FOUNDRY_ENDPOINT` remains the Azure AI Services account URL
(`https://<account>.cognitiveservices.azure.com/`) used by image generation.
`FOUNDRY_PROJECT_ENDPOINT` is injected into the Container App as required
non-secret configuration for hosted-agent invocation, using the
same project name that the Foundry module creates:
`https://<account>.services.ai.azure.com/api/projects/<project>`. The root
template resolves that project name once with
`take('${aiFoundryProjectName}-${environmentName}', 64)` and passes the same
value to both the module and the Container App config to avoid module-output
cycles.

Hosted-agent permissions are mandatory. Provisioning grants the project
identity Foundry User at account scope and the Container App identity Foundry
Agent Consumer at project scope. There is no RBAC opt-in or runtime switch that
can restore a direct card-text path.

Provisioning grants the deployment caller only:

- **Key Vault Reader** at Key Vault scope, allowing metadata-only list/browse
  access for secrets, keys, and certificates. This role does **not** permit
  secret-value reads, key material reads, private-key export, cryptographic key
  operations, object mutation, purge/recover, or Key Vault RBAC changes.
- Cosmos DB Built-in **Data Reader** at the Cosmos account root, using Cosmos
  native data-plane RBAC. Account-root scope is required for `readMetadata`,
  database discovery, and container discovery.
- **Storage Blob Data Reader** at Storage Account scope, allowing Blob
  container enumeration and blob list/read operations.
- **Foundry User** (formerly **Azure AI User**) at the Foundry project scope.

These read-only deployer grants are intentionally separate from the existing
Container App managed identity grants: Cosmos DB Built-in **Data Contributor**,
container-scoped **Storage Blob Data Contributor** on both `card-assets` and
`profile-photos`, and account-scoped **Cognitive Services User**. Authentication
secrets are supplied through ACA-native secrets and `secretRef` environment
variables; the runtime identity does not read secret values from Key Vault.
The deployer cannot create, replace, upload, overwrite, or delete Cosmos items
or blobs through the reader roles.

Provisioning also grants the hosted-agent permissions needed for invoke-only access:

- the Foundry project's system-assigned managed identity receives **Foundry
  User** at the Foundry account scope, matching Microsoft Foundry's project
  managed-identity access requirement.
- the Container App's system-assigned managed identity receives **Foundry Agent
  Consumer** at the Foundry project scope. This is intentionally narrower than
  Foundry User/Contributor and can be narrowed to agent scope later once an
  agent resource exists.

The deploying identity must already be allowed to create assignments:

- `Microsoft.Authorization/roleAssignments/write` for the Key Vault, Storage,
  and Foundry ARM role assignments. Key Vault scope is sufficient for the vault
  reader assignment; resource-group scope also works if delegated there.
- `Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments/write` for the
  Cosmos native role assignment.

`deployer()` identifies the caller; it does not grant these control-plane
permissions. Azure RBAC and Cosmos native RBAC can also take several minutes
to propagate, so post-provision access checks should retry.

RBAC does not bypass network controls. Blob reads still require a
VNet-connected environment with working private DNS because Storage keeps
`publicNetworkAccess: Disabled` and its private endpoint. Cosmos reads likewise
use its private endpoint and VNet-linked private DNS zone. Key Vault
data-plane calls also require the private endpoint path — the vault is deployed
with `publicNetworkAccess: 'Disabled'` and no network ACLs; without the private
endpoint the ACA container's egress (via NAT Gateway, public IP) is rejected
with HTTP 403 by the vault firewall. The `keyvault-private-endpoint.bicep` module
provisions the private endpoint, `privatelink.vaultcore.azure.net` private DNS
zone, and VNet link as part of every `azd provision`. It uses pinned AVM modules
for both the private endpoint and the DNS zone with its VNet link.

For a targeted repair of an existing environment, this module can be applied
independently with `az deployment group what-if` followed by
`az deployment group create`, supplying only the existing vault, subnet and VNet
resource IDs plus the vault name and location. Review the incremental change set
before applying it. This avoids unrelated full-provision effects: the current
`postprovision` hook creates a new Entra client credential only when Bicep
explicitly reports that the deployment manages the registration, and
provisioning without `containerImage` selects the bootstrap image. Normal
full-environment orchestration remains `azd`. Ordinary root provisioning runs
the image-preservation hook first; only direct Bicep deployments that omit
`containerImage` select the bootstrap image.

Why the extra env var:

- `natGatewayPublicIpAddress` is provisioned by IaC and always lands in Cosmos
  `ipRules`.
- `LEGACY_COSMOS_IP_RULE` is only a temporary cutover aid so the pre-NAT ACA
  incident allowlist can stay in place until the new path is proven in a real
  deployment.
- Clear it after validation:

```bash
azd env set LEGACY_COSMOS_IP_RULE ""
azd provision
```

## Parallel cutover

ACA VNet mode is effectively a create-time choice, so this repo now targets a
replacement Container Apps environment/app pair (`*-cae-nat`, `*-app-nat`)
instead of mutating the original environment in place.

Recommended rollout:

1. Leave the old Container Apps environment and the live
   `20.10.253.231` Cosmos rule available.
2. Run `azd up` so the NAT-backed environment, subnet, public IP, and Cosmos
   NAT rule are provisioned in parallel.
3. Smoke-test the new app endpoint and authenticated generation flow.
4. Only after successful validation, clear `LEGACY_COSMOS_IP_RULE` and rerun
   `azd provision` to remove the temporary incident rule from the desired state.

## Smoke-test / operational checks

After a live deploy, verify:

1. `azd env get-values` reports `AZURE_CONTAINER_APP_NAME`,
   `AZURE_CONTAINER_APPS_ENVIRONMENT_NAME`, and `natGatewayPublicIpAddress`
   outputs for the replacement environment.
2. The Cosmos account still shows `publicNetworkAccess = Enabled`.
3. Cosmos `ipRules` contains the NAT public IP and, during cutover, the legacy
   stopgap IP.
4. The deployed app still uses the normal Cosmos endpoint
   (`https://<account>.documents.azure.com:443/`) and managed identity.
5. An authenticated `POST /ui/cards/generate` (or equivalent end-to-end flow)
   persists successfully to Cosmos from the NAT-backed environment.
6. Once verified, remove the temporary legacy rule by clearing
   `LEGACY_COSMOS_IP_RULE`.
7. In the Foundry project's **Access control (IAM)**, verify the signed-in
   deployer has **Foundry User** at the project scope, then open the project
   using Microsoft Entra authentication.
8. After allowing for RBAC propagation, verify the deployer has Cosmos DB
   Built-in **Data Reader** at account-root scope and can discover/query the
   database from a firewall-allowed network path.
9. From a VNet-connected host with private DNS, use Microsoft Entra
   authentication to enumerate Blob containers and list/read a blob. Confirm
   upload and delete operations remain unavailable to the deployer.
10. Using the deployment identity (Key Vault Reader), confirm the Key Vault data
    plane allows listing secrets, keys, and certificates plus reading only their
    metadata. Also confirm representative forbidden operations stay denied:
    reading a secret value, exporting certificate private key material, key
    crypto operations, and any create/update/delete/recover/purge action. All
    Key Vault data-plane access requires the
    `privatelink.vaultcore.azure.net` private endpoint path; requests from
    outside the VNet are rejected with HTTP 403.

The key live-network assertion — ACA outbound traffic actually using the NAT
public IP — requires an Azure deployment and cannot be proven from source alone.

## Saved-photo moderation and storage

Issue [#67](https://github.com/bmoussaud/fantasy-cards-generator/issues/67)
reuses the existing Azure AI Services / Foundry account endpoint as the default
`CONTENT_SAFETY_ENDPOINT` for pre-persist photo moderation and adds a separate
private Blob container, `profile-photos`, for durable user-owned photos and
thumbnails.

- Override `CONTENT_SAFETY_ENDPOINT` only if production uses a different Azure
  AI / Content Safety resource than the default Foundry-backed account.
- Live rollout still needs Azure-side verification that the chosen region/account
  exposes Content Safety capacity and quota before this feature is enabled in
  production.

## Rollback

If the replacement environment fails validation:

1. Keep the old Container Apps environment running.
2. Do not clear `LEGACY_COSMOS_IP_RULE`.
3. Point traffic/deployment back to the old environment if you temporarily
   switched consumers to the replacement app URL.
4. Fix the NAT-backed path in IaC, then redeploy and re-test.

Because the old environment is preserved in parallel, rollback is primarily a
matter of continuing to use the pre-cutover app while the new environment is
reworked.

## NAT public IP replacement implications

If the NAT Gateway public IP resource is ever replaced, Azure will allocate a
different static address unless the same Public IP resource is preserved.
Because Cosmos `ipRules` are wired from the NAT Gateway output, a subsequent
`azd provision` will update the desired firewall rule automatically — but you
must still smoke-test the app again before completing the network cutover.

## Entra redirect verification

The replacement Container Apps environment gets a different default domain, so
OIDC redirect URIs must be rechecked after the first live deploy.

- If `deployEntraAppRegistration=true`, `infra/main.bicep` derives the deployed
  redirect URI from the new Container Apps environment domain automatically.
- If the app registration is still managed manually, update the registered web
  redirect URI to the replacement app URL plus `/auth/callback`.

Either way, verify authenticated sign-in against the live replacement domain
after deployment. Do not guess or pre-change portal values without the actual
deployed hostname.
