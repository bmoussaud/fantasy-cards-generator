# Private metadata runner (dev only)

**Prepared, not provisioned or executed. Independent review and coordinator approval
are required before either mutation command below.** This is a metadata preflight,
not a secret rotation service, GitHub runner, shell service, or proof of application
credential adoption.

## Read-only inventory and evidence

Inventory on 2026-09-11 found no Container Apps jobs, VMs, VM scale sets, or container
instances in the intended dev resource group. Its VNet subnet IP configurations
belong to private endpoints, not a reusable runner VM. The existing Container App is
the application, not an authorized execution host; its identity is not reused.
This conclusion is scoped to the intended RG/VNet, not a subscription-wide runner
search or GitHub registration inventory.

The existing Container Apps environment has a Consumption profile and uses the
VNet's ACA infrastructure subnet. The Key Vault private endpoint is approved in
the same VNet, and its private DNS zone link reports `Completed`. Key Vault uses
RBAC with public network access disabled. The private endpoint NIC address is
recorded in the canonical module as an exact DNS expectation. These are
management-plane observations; actual private data-plane reachability and
individual-secret authorization remain unproven until the approved job runs.

The live Key Vault provider operation catalog confirms
`Microsoft.KeyVault/vaults/secrets/readMetadata/action` is a **data action**; the
built-in Key Vault Reader includes it. The custom role deliberately omits that
built-in role's other data actions. Microsoft documents both
[individual-secret RBAC scope](https://learn.microsoft.com/azure/key-vault/general/rbac-guide)
and the
[`GET /secrets/{secret-name}/versions` metadata-only endpoint](https://learn.microsoft.com/rest/api/keyvault/secrets/get-secret-versions/get-secret-versions).
The latter requires secret list permission, returns no values, and maps to this
metadata action under RBAC. We do not call the vault-wide list endpoint or
`get_secret`, nor grant Secrets User, get-value, write, or rotation permissions.

The additive ARM validation succeeded. The complete leaf what-if reports:

| Action | Resource kind | Count |
| --- | --- | --- |
| Create | Container Apps job | 1 |
| Create | User-assigned managed identity | 1 |
| Create | Custom role definition | 1 |
| Create | Role assignments | 3 |

There are **six resource creates, no modifications or deletions**; existing
resources are ignored. Nested ARM deployment records are orchestration metadata,
not extra workloads. Validation is not an assurance that future authorization,
policy, image pull, DNS, or RBAC propagation will succeed.

## Infrastructure and privilege boundary

`infra/modules/private-metadata-runner.bicep` is the single canonical module.
Both `infra/main.bicep` and the additive `infra/private-runner.bicep` call it behind
`enablePrivateMetadataRunner=false`. The root module is additionally dev-only;
the leaf allows only dev, and the canonical module has an ARM validation guard
for the reviewed resource group/subscription. The root wrapper requires explicit
`--environment dev` and the reviewed subscription ID. No prod path is supported.

The job has one dedicated user-assigned identity, no ingress, secrets, PAT,
GitHub registration, schedule, event trigger, app identity, or arbitrary command
input. It runs one replica with 0.25 vCPU and 0.5 GiB, zero retries, and a
120-second replica timeout. The script has a separate 60-second wall-clock limit,
five-second network timeouts, no SDK retries, and at most 100 versions per named
secret. The operator must serialize starts; the wrapper rejects an already
running execution, but this check is not a distributed lock against concurrent
operators.

The only Key Vault grants are at the **two named secret resource scopes**:
`app-session-secret-key` and `entra-client-secret`. The custom role's assignable
scope is the dev RG (where the role is defined); that does **not** grant the
identity RG-wide or vault-wide access. The third assignment is AcrPull on the
existing private registry, required because the job reuses the existing project
image. No operator permission, network rule, app resource, agent setting, or
credential is changed.

The image is the already deployed dev web image, pinned by its manifest SHA-256
digest verified through ACR metadata on the inventory date. Its source contract
contains the frozen `/app/.venv` with `azure-identity` and
`azure-keyvault-secrets`. The job explicitly invokes that interpreter rather than
the image's web CMD. Bicep embeds the reviewed preflight script using
`loadTextContent`: **no image rebuild, registry push, Dockerfile modification, or
script download is required**. Import/runtime compatibility is finally confirmed
by the first approved execution; a missing SDK fails without a traceback.

AVM App Job `0.7.2` was evaluated first. Its nested-module parameters include the
new identity's runtime client ID; actual what-if skipped the job with a diagnostic
and showed only five creates. A native `Microsoft.App/jobs@2024-03-01` resource
keeps the full six-resource preview visible. Native Bicep is also used for the
small exact-scope RBAC bundle. The wrapper refuses diagnostics, incomplete plans,
unexpected resource kinds/scopes, or anything other than the six expected creates.
This intentionally blocks updates/reprovisioning after the first creation; such
changes need a separately reviewed plan.

## Canonical root commands

The root `azure.yaml` remains the sole azd manifest. Existing web, agent, full,
and shared provisioning flows are unchanged. `./deploy.sh` orchestrates those
flows with azd and the runner with an **explicit incremental ARM leaf deployment**.
This is not a claimed azd service-scoped provision command. Installed azd help
supports provisioning configured layers, but this project has no isolated runner
layer, and its shared hooks initialize/mint credentials. Runner commands never
call azd provision, those hooks, `azd env get-values`, or read `.env` files.
Do not use the broad shared-stack preview/provision for this preparation: its
unrelated changes are not part of the runner approval.

Run from the repository root with the existing Azure CLI login; no new persistent
credentials are needed. The allowlisted subscription ID is non-secret:

```bash
# Read-only: allowed during preparation.
./deploy.sh runner-preview --environment dev \
  --subscription b8ff3e15-7e2d-4fac-a773-992fb59ccedd

# Plan only: does not invoke Azure.
./deploy.sh runner-provision --environment dev \
  --subscription b8ff3e15-7e2d-4fac-a773-992fb59ccedd

# LATER ONLY: after independent review and coordinator provisioning approval.
# Repeats and validates the complete six-create preview before incremental apply.
./deploy.sh runner-provision --environment dev \
  --subscription b8ff3e15-7e2d-4fac-a773-992fb59ccedd --approve-change

# SEPARATE LATER EXECUTION APPROVAL: starts only the fixed metadata job.
./deploy.sh runner-start --environment dev \
  --subscription b8ff3e15-7e2d-4fac-a773-992fb59ccedd --approve-change
```

Provision does not start the job. Start compares live identity, image, command,
environment, replica limits and resource settings to the reviewed contract before
requesting execution; no template overrides are accepted. Operator needs existing
deployment/identity/job write rights plus role-definition and scoped
role-assignment write rights to provision. Start requires job read/start,
execution read, and dedicated identity read. **No such operator roles are
assigned here.** Authorization failures block; do not broaden permissions or
substitute an app identity to bypass them.

After a separately approved start, observe execution status with an allowlisted
query (status is not proof of rotation):

```bash
az containerapp job execution list --resource-group rg-fcag-dev \
  --name fcag-dev-metadata-runner \
  --subscription b8ff3e15-7e2d-4fac-a773-992fb59ccedd \
  --query '[].properties.status' --output json
```

The preflight emits one JSON record: `schema`, normalized `status`, and on success
only `version_hash` (SHA-256 of the sorted, bounded two-secret version set). It
never emits names, IDs, URIs, addresses, credentials, metadata tags, values, or raw
SDK errors. Hashes identify a version set, not which version the app uses.
Normal platform execution/image-pull diagnostics are separate from script output;
do not publish raw platform logs or request payloads.

## Failure and recovery

`metadata_forbidden` can mean RBAC propagation or private-network denial; it is
intentionally not a raw Azure error. Check only approved control-plane metadata
and retry a single manual execution after propagation if separately authorized.
DNS must resolve exclusively to the inventoried private endpoint address;
unexpected DNS/IP changes fail closed and require re-inventory and review.
No public firewall exception, ARM secret-child write, get-value fallback,
credential rotation, or app restart is part of recovery.

No job replicas run while idle, so incremental job compute cost is zero when
unused. Each execution is bounded to 30 vCPU-seconds and 60 GiB-seconds by the
120-second replica limit; logging and existing shared infrastructure charges
remain. Image retention in the existing registry also remains necessary.

To disable operation, stop issuing manual starts and stop any active execution
through a separately approved operator action. Setting the opt-in false under
incremental deployment **does not delete or disable an existing job**. Later
cleanup must explicitly target only this job, its dedicated identity, three
assignments and the custom role after confirming no other assignment uses it.
Never delete the RG/environment/vault, use complete-mode deployment, run azd down,
or prune the shared stack. Existing resources remain intact.

## Offline checks

```bash
az bicep build --file infra/modules/private-metadata-runner.bicep --stdout >/dev/null
az bicep build --file infra/private-runner.bicep --stdout >/dev/null
az bicep build --file infra/main.bicep --stdout >/dev/null
bash -n deploy.sh
python -m pytest tests/test_deployment_runner.py tests/test_deployment_config.py \
  tests/test_hosted_agent_deployment_config.py -q
```

Fake-SDK tests cover only named metadata calls, pagination bounds, private DNS,
managed-identity selection, error redaction and output shape. Deployment tests
cover approval gates, create-only complete previews, exact compiled RBAC scopes,
fixed commands, and unchanged root web/agent contracts.
