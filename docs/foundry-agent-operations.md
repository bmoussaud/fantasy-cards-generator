# Card-orchestrator operations

Related: #109 (hosting), #99 (operations), #117 (merged client/RBAC wiring),
#118 (read-only preflight). This package does **not** deploy or enable the web
generation path. It contains no web hooks, model deployment, new registry,
new Foundry account/project, or monitoring resource.

## Contract and status

| Dimension | Contract |
| --- | --- |
| azd project | `deployments/card-orchestrator/azure.yaml` |
| Environment / service | `dev` / `card-orchestrator`; `prod` remains blocked pending review |
| Runtime | Python 3.12, Linux amd64, non-root |
| Entrypoint | `python -m hosted_agents.card_orchestrator` |
| HTTP | `/responses` and non-model `/readiness` on port 8088 |
| Protocol declaration | `responses`, version `2.0.0` (not REST `api-version=v1`) |
| Initial reviewed allocation | 0.5 CPU / 1GiB; not automatically provisioned |
| Model | Existing text deployment; three bounded concept/lore/art-direction specialists |
| Excluded | Image generation, persistence, web activation, scheduled evaluation/probes |

The real runtime and `hosted-agent` optional extra are integrated with the
dedicated container/deployment package in PR #119. Packaging-only tests are not
startup evidence; the integrated validation below includes the correct agent
Dockerfile, real entrypoint and credential-free readiness check.

Latest live result: the [newly authorized smoke](#newly-authorized-dev-smoke--2026-09-08)
deployed successfully and sent exactly one actual ACA-MI Responses request, which
returned **HTTP 403**. Its session and version were deleted; exact-resource GETs
confirmed HTTP 404. This is not successful end-to-end card generation.

### Next separately approved window: same-identity session contract

**Offline correction only; no new allowance, deployment, session or inference.**
The last deployed image was application source
`2bdbf9967d8c397f7d88914bac06285b3b477297`; it did not include this correction.
The documented caller-Entra session scope and current Responses
`agent_session_id` field are verified contract facts. Cross-identity ownership
is a plausible explanation of the historical 403, **not a proven diagnosis**
because the error body was discarded. No RBAC escalation is justified.

For Gimli's **next independently approved** execution, this sequence supersedes
any earlier operator-created warmup-session recipe:

1. Keep the existing identity, project Consumer role, model limits and web
   baseline. Build/deploy only the reviewed new source and record its immutable
   application SHA, image digest, exact new hosted version and submission time.
   Start the **30-minute maximum runtime clock at deployment submission**, with
   cleanup in an outer `finally`, including deployment timeout. Do not deploy
   from a historical image and label it the corrected code.
2. Before any session creation or probe dispatch, durably record a fresh
   `smoke-109-` plus `uuid.uuid4().hex` identifier, agent name, exact new version,
   request-source SHA, application SHA and create-dispatch timestamp. Using the
   already-privileged operator's read-only session GET, establish that this exact
   ID is absent (404); don't reuse old IDs. A 403/timeout is not absence.
   Do **not** create a session as the operator or invoke for warmup.
3. Run GET-only `--prepare-invocation` if needed; preparation never creates a
   session and is not invocation proof. Invoke the corrected probe **once**,
   with `--invoke-once --hosted-version <new-version>
   --expected-version <application-sha> --session-id <recorded-id>`, pinned to the
   existing ACA revision/replica/container and expected system principal.
   Inside ACA, the same checked in-memory MI token creates the session (one POST)
   and invokes it (at most one Responses POST). HTTP 201 and matching
   `agent_session_id` / `version_indicator` are mandatory. `creating`/`updating`
   trigger only bounded readiness GETs; only `active` permits inference.
4. Local invocation transport is capped at **10 seconds setup + 100 seconds
   result, 110 seconds total**. Remote work is capped at **30 seconds setup**
   (source decoding/import/MI/create/readiness, at most 15 GETs) plus a separate
   **65-second invocation** guard. Preparation retains 30/80/110 local and
   70 remote. The existing model orchestration remains three stages, 20 seconds
   per stage / 65 overall, no retry. No timeout or HTTP failure authorizes a
   second inference call; own-session create permissions remain untested live.
5. In **every finally**, preserve the strict sanitized result first, then
   reconcile the pre-recorded exact ID with bounded operator `azd`/session API
   operations. `sessionCreateAttempted:true` with timeout/malformed output means
   completion can be unknown. Missing marker means creation **and** invocation
   completion are unknown; assume allowance consumed. Never depend on a
   server-returned session ID being captured before cleanup.
6. Stop/delete only the recorded newly owned session, after matching the exact
   new version and the pre-recorded absence/create interval; never delete a
   mismatched or pre-existing resource. **409 collision forbids deleting that
   session**, even if it resembles the expected ID. Reconcile unknown creation
   with exact GETs every two seconds for at most **60 seconds**, each request
   capped at **10 seconds**; no create or inference retries. Run this reconciliation
   again after deleting only the run's new hosted version, so late completion
   cannot be mistaken for early absence. Verify exact session/version GET 404
   and no active matching sessions using the privileged operator (no role
   changes). Bound the entire cleanup to **five minutes**, begin it no later than
   minute 25 of the deployment clock, and report unresolved cleanup explicitly
   on deadline/403/mismatch rather than claiming success or touching older state.
   A single early 404 after unknown create completion is not cleanup proof.

The remote probe deliberately does **not** stop/delete in its own finally:
cleanup must not consume its model/result budget or destroy evidence. Existing
privileged operator cleanup can manage cross-user sessions; the ACA caller
continues to have only its existing consumer grant. No optional telemetry
inspection is a gate, and no service messages, raw responses, tokens or arbitrary
headers are exported. See the [wire and diagnostic contract](foundry-agent-invocation.md#actual-aca-managed-identity-access-probe).

The platform supplies `FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_AGENT_NAME`,
`FOUNDRY_AGENT_VERSION`, and the Application Insights connection configuration.
Do **not** redeclare reserved values in service `environmentVariables`.
Only `AZURE_AI_MODEL_DEPLOYMENT_NAME` and `CARD_ORCHESTRATOR_VERSION` are supplied
by this manifest. The latter is the full immutable application Git commit, not a
Foundry version number. Runtime timeout/policy defaults belong to the runtime;
inspect their bounded values during integration rather than adding guessed SDK
settings here.

The implemented defaults/maxima are 20 seconds per specialist and 65 seconds
overall. These are **offline candidate budgets**, not compliance with the
proposed production budgets of 8.15 seconds per stage and 30.15 seconds overall.
Narrow local safety heuristics are not comprehensive safety or prompt-injection
protection; unobserved hosted guardrails remain unavailable and post-image checks
are not applicable. Application nonpersistence does not guarantee zero platform
telemetry.

The same `FOUNDRY_PROJECT_ENDPOINT` name is also an **azd deployment-context**
value consumed by the extension. Bicep outputs both that name and
`AZURE_AI_PROJECT_ENDPOINT`; this does not override the platform's container
environment. If provisioning is skipped, configure both locally to the same
verified project endpoint.

Current hosted-agent documentation distinguishes the dedicated **per-agent
Entra runtime identity** from the **project MI** used for infrastructure such as
registry pulls. The model client must use the supported platform credential
chain; do not force the project principal into the container. The compatibility
role repairs below preserve main's contract but are not proof of the agent's
runtime identity or successful model authorization. Verify that distinction at
the approved deployment gate without granting additional downstream roles.

## Source and tooling verification

Verified locally: azd **1.32.0**, `azure.ai.agents` **1.0.0-beta.13**,
Bicep **0.46.1**. The extension is pinned; no setup script installs/upgrades tools
automatically. JSON-compatible YAML permits offline structural tests without a
new parser dependency.

Authoritative references (reviewed 2026-09-08; Azure/azure-dev source revision
`d73bf6194913b257e02332a79932c727ac5eaaa0`):

- [azd schema: service Docker path/context/platform and extension versions](https://github.com/Azure/azure-dev/blob/d73bf6194913b257e02332a79932c727ac5eaaa0/schemas/v1.0/azure.yaml.json)
- [Agent container resource tiers](https://github.com/Azure/azure-dev/blob/d73bf6194913b257e02332a79932c727ac5eaaa0/cli/azd/extensions/azure.ai.agents/internal/project/config.go)
- [Agent deployment: project endpoint and Responses 2.0.0](https://github.com/Azure/azure-dev/blob/d73bf6194913b257e02332a79932c727ac5eaaa0/cli/azd/extensions/azure.ai.agents/internal/project/service_target_agent.go)
- [Existing-project Bicep route](https://github.com/Azure/azure-dev/blob/d73bf6194913b257e02332a79932c727ac5eaaa0/cli/azd/extensions/azure.ai.agents/internal/synthesis/templates/existing-project.bicep)
- [Official project registry connection and AcrPull identity](https://github.com/Azure/azure-dev/blob/d73bf6194913b257e02332a79932c727ac5eaaa0/cli/azd/extensions/azure.ai.agents/internal/synthesis/templates/modules/acr.bicep)
- [Hosted agent requirements](https://learn.microsoft.com/azure/ai-foundry/agents/concepts/hosted-agents?view=foundry)
- [Docker-specific ignore-file semantics](https://docs.docker.com/build/building/context/#dockerignore-files)

The connection API deliberately uses `2025-04-01-preview`: the official azd
template records a GA `2025-06-01` connection-resolution failure. This is not a
reason to change the account or project's existing API/resource configuration.
At that packaging checkpoint, installed CLI help and upstream source had been
inspected but live deployment had not yet been verified. The executed-smoke
sections below record the subsequent successful deployments and failed invocation
outcomes separately.
The hosted-agent observability documentation confirms platform injection of the
Application Insights connection string; no speculative connection resource is
added by this package.

## Offline integration gate

### Completed integrated evidence — 2026-09-08

Application build `39278a3f0f80c735ed52235a7d5e21026f10ecfb` combines runtime
`941dc39` and packaging `f17ff4c` (cherry-picked as `39278a3`). The coordinator
directly verified:

- **202 tests passed** in one invocation: `test_card_orchestrator`,
  `test_card_orchestrator_models`, `test_foundry_agent_client`,
  `test_hosted_agent_deployment_config`, and `test_deployment_config`.
  Earlier backend-only results are separate evidence, not an additive total.
- Dedicated Bicep compiled; the exact Linux amd64 agent Dockerfile built as
  `card-orchestrator-agent:39278a3`. A root web-image build is not agent evidence.
- Image entrypoint `python -m hosted_agents.card_orchestrator`, port 8088,
  non-root user `agent`. With synthetic endpoint/model settings, build version
  `39278a3`, and **no credentials, secrets or mounts**, `/readiness` returned
  HTTP 200 with `{"status":"ready","cloudProbe":false}`. A request to `/responses`
  with `store:true,input:[]` returned HTTP 400, sanitized `invalid_request`.
  An initial cold-start probe reset before startup (approximately 14 seconds);
  later probes succeeded. The test container was stopped and removed.
- Independent Samwise review of `origin/main..39278a3` found no significant
  defects. This is code-review evidence, not live authorization or model-quality
  acceptance.

No billable calls or Azure resource mutations occurred in those checks.
The subsequent publication update changes this runbook only, not that tested
application source.

For repeatable infrastructure checks from the repository root:

```bash
python -m pytest -q --noconftest tests/test_hosted_agent_deployment_config.py tests/test_deployment_config.py
az bicep build --file deployments/card-orchestrator/infra/main.bicep --stdout >/dev/null
docker build --platform linux/amd64 \
  -f hosted_agents/card_orchestrator/Dockerfile \
  -t "card-orchestrator:$(git rev-parse HEAD)" .
```

`--noconftest` is intentional for these infrastructure-only tests: it avoids
bootstrapping the web application, authentication fixtures, and local settings.
Run the runtime owner's targeted tests separately, with fake model clients and
telemetry disabled. Validate readiness in that offline harness without a model
call; don't expose a cloud credential to a local container just to test health.

The extension requires service `project: "."` inside the dedicated manifest
folder; `project: "../.."` fails its service-path validation. Docker `path` and
`context` are relative to that service root and explicitly select the repository
build context and agent Dockerfile. `remoteBuild: false` avoids uploading an ACR build archive.
`language: docker` makes azd build the Dockerfile directly, rather than attempting
a host-side Python/requirements.txt restore in the manifest directory.
The Dockerfile-specific ignore file takes precedence over root `.dockerignore`.
Only Python source, dependency metadata, README and this Dockerfile are allowed;
azd state, histories, dotenv files, credentials and assets are excluded. Runtime
data-file additions require an explicit allowlist/test update.

## Required nonsecret inputs and read-only checks

Use a separate azd `dev` state under the dedicated project. Do not copy the root
`.azure` directory or dump `azd env get-values`. Record these nonsecret bindings
with `azd env set` inside `deployments/card-orchestrator`:

| Variable | Source / constraint |
| --- | --- |
| `AZURE_SUBSCRIPTION_ID`, `AZURE_LOCATION` | Reviewed dev subscription and supported existing project region |
| `AZURE_RESOURCE_GROUP` | Existing shared resource group; this package assumes all references are in it |
| `AZURE_AI_ACCOUNT_NAME`, `AZURE_AI_PROJECT_NAME` | Existing account/project names from management-plane lookup |
| `AZURE_AI_PROJECT_ID` | Exact verified project ARM ID, never guessed from DNS |
| `AZURE_AI_PROJECT_ENDPOINT`, `FOUNDRY_PROJECT_ENDPOINT` | Same verified project endpoint |
| `AZURE_CONTAINER_REGISTRY_NAME`, `AZURE_CONTAINER_REGISTRY_ENDPOINT` | Existing registry name and login server |
| `AZURE_CONTAINER_REGISTRY_RESOURCE_ID` | Exact registry ARM ID |
| `AZURE_CONTAINER_APP_NAME` | Existing dev ACA app whose system identity invokes the agent |
| `AZURE_AI_MODEL_DEPLOYMENT_NAME` | Existing text deployment; no model capacity is created |
| `AZURE_AI_PROJECT_ACR_CONNECTION_NAME` | Existing registry connection name, or reviewed new `<registry>-conn` |
| `CARD_ORCHESTRATOR_VERSION` | Full integrated commit SHA; never reuse a tag for a different build |

For a fresh isolated azd state, the manifest already exists:

```bash
cd deployments/card-orchestrator
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env new dev
# Set the reviewed inputs individually; examples deliberately use placeholders.
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set AZURE_SUBSCRIPTION_ID "<subscription-id>"
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set AZURE_RESOURCE_GROUP "<existing-dev-rg>"
```

Check identity principal IDs, exact project ID/endpoint, registry role-assignment
mode/network policy, existing connection **names/categories/targets only**, and
existing role-assignment **IDs/scopes/principals/role IDs only**. Do not request
connection credentials, listKeys, tokens, dotenv contents or complete app config.
Verify the image publisher already has the registry's appropriate push role;
this package does not grant deployer privileges.

Classic registry RBAC is the only implemented path. If
`roleAssignmentMode=AbacRepositoryPermissions`, stop: classic AcrPull is ignored.
Require a separately reviewed repository-scoped ABAC policy, not a wider role or
a registry mode change. Also stop if existing network restrictions prevent
access; no public-access relaxation is authorized.

Application Insights linkage is a prerequisite for end-to-end tracing, **not**
hosted deployment. The existing-project template and deployment validation do
not require it. This runtime disables instrumentation, host observability and
SDK/access logging; proceed without telemetry IaC changes. This is not a promise
about platform retention or evidence that monitoring is configured.

## Bounded prerequisite preview, then separate approvals

The launcher is **plan-only by default**, dev-only, always uses the dedicated
manifest folder and requires an extra approval flag for execution of mutations.
It is an operator guard, not a security boundary: raw `azd deploy` bypasses it.
No scheduled workflow or automatic deployment is installed.

```bash
python deploy.py preview
python deploy.py deploy
```

Infrastructure defaults are `CARD_ORCHESTRATOR_ENABLE_PREREQUISITES=false` and
`CARD_ORCHESTRATOR_CREATE_REGISTRY_CONNECTION=false`. With both off, provisioning
only resolves existing references and outputs; it creates no agent compute.
After nonsecret inventory and approval to **preview** the proposed role repair:

```bash
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set CARD_ORCHESTRATOR_ENABLE_PREREQUISITES true
# Only if the registry connection is confirmed absent:
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set CARD_ORCHESTRATOR_CREATE_REGISTRY_CONNECTION true
AZURE_DEV_USER_AGENT=microsoft_foundry_skill python deploy.py preview --execute
```

Allow only these changes in the what-if:

1. Project system MI → **Foundry User** at its existing account.
2. Existing ACA system MI → **Foundry Agent Consumer** at the existing project.
3. Project system MI → **AcrPull** at the existing registry (classic RBAC).
4. Optional `ContainerRegistry` project connection using managed identity,
   existing login server/registry ID, and the official project-principal shape.

The first two assignments are a **repair facade**, not new ownership:
`infra/modules/ai-foundry.bicep` remains canonical. Their names, scopes,
principals and role IDs exactly match main; offline tests enforce parity.
Do not run root and isolated provisioning concurrently. Use incremental
deployment only, never complete mode or `azd down` on this shared-resource
package. At the next independently approved root rollout keep its existing
`ENABLE_FOUNDRY_AGENT_ACCESS=true` setting aligned.

A same-scope/principal/role assignment under another GUID must be reconciled
before apply: do not delete/recreate or silently invent a second assignment.
Likewise reuse an existing registry connection by setting its discovered name
and leaving connection creation off. Root already owns a **different** ACA image
pull identity; do not modify that assignment.

Reject any what-if that writes an account/project/registry/app, deploys a model,
changes networking/traffic/image/secrets, or removes existing resources. Bicep
compilation alone cannot validate ARM-time permissions or connection semantics.
Only after separate approval:

```bash
AZURE_DEV_USER_AGENT=microsoft_foundry_skill python deploy.py provision --execute --approve-change
```

### Actual dev prerequisite apply and ACA identity — 2026-09-08

PR #119 merged as `5f76207`. The earlier subscription/azd previews returned
success without inspectable changes. A **resource-group leaf what-if** against
`infra/modules/prerequisites.bicep`, with exact live ACA/project principals,
resolved this: precisely three role-assignment Creates and one registry
connection Create; every other resource was Ignore. Remaining `reference()`
expressions pointed to the existing project principal and registry login server,
independently verified through safe management-plane projections.

For this diagnostic use `az deployment group what-if --result-format
FullResourcePayloads --no-pretty-print` with the leaf's explicit parameters.
**Project the response before printing or persisting it**: retain change types,
resource IDs and allowlisted role/connection fields only. FullResourcePayloads
can include complete configuration for unrelated **Ignore** resources; never
dump the unfiltered response. Diagnostic ARM what-if does not replace azd/Bicep
for apply.

After confirming all three exact grants and the connection were absent, and
the registry used `LegacyRegistryPermissions`, the authorized dedicated
`python deploy.py provision --execute --approve-change` ran **once**. Actual
resource mutations, subsequently verified:

- Project MI → Foundry User, existing account scope.
- ACA system MI → Foundry Agent Consumer, existing project scope.
- Project MI → AcrPull, existing registry scope.
- Existing project → `<registry>-conn`, ContainerRegistry / ManagedIdentity.

The nested `card-orchestrator-dev-prerequisites` deployment **Succeeded**.
The parent `dev-1788866608` deployment nevertheless failed **after** these writes:
`DeploymentOutputEvaluationFailed`, because the old subscription-scope
`resourceId(resourceGroupName, ...)` output interpreted the RG name as a
subscription ID. This branch fixes the output to the existing `project.id`;
ARM subscription validation **Succeeded** with prerequisites off. After
independent review, the output fix was applied through isolated azd with both
resource-mutation flags disabled. Deployment `dev-1788867445` **Succeeded** and
returned the correct existing project ARM ID. Its operations were a registry
read and deployment-output evaluation; no additional resource changes were made.
Do not interpret the failed parent deployment as rollback or rerun blindly.

The new read-only [ACA identity probe](foundry-agent-invocation.md#actual-aca-managed-identity-access-probe)
ran inside the existing serving `web` container, explicitly acquired an MI
token for `https://ai.azure.com/.default`, and matched its `oid` to the live ACA
system principal before sending it. `GET /agents?api-version=2025-11-15-preview`
returned **HTTP 200, `data:[]`**. This proves target-MI project **access**, not
agent invocation or hosted-runtime/model authorization.

Safe ACA baseline comparison before apply and after the probe was identical:
image, container, latest/ready revision, traffic, ingress, revision mode and
identity. `FOUNDRY_PROJECT_ENDPOINT` remains **absent from ACA configuration**;
the probe received the verified endpoint only as an argument. There was no
serving image/file/secret/env/traffic mutation, registry upload, agent compute,
model call, production change or root provision/hook. No tools/dependencies
were installed. The isolated ignored azd opt-in booleans were reset to `false`.
Targeted offline probe/infrastructure validation: **100 tests passed**.

Further hosted-deployment gates are tracked in the approved-smoke checkpoint
below. The prerequisite proposal creates no compute or model
capacity; a later separately approved hosted deploy can incur 0.5 CPU / 1GiB
compute, registry storage and telemetry ingestion costs, and a remote smoke
request can incur model charges. This runbook is partial #99 operations scope,
not installed dashboards, alerts or production readiness.

### Earlier checkpoint: incorrect telemetry gate — 2026-09-08

At requester approval, the allowed live test was **one** synthetic ACA-MI
invocation, at most three existing-model calls (1800 output tokens per stage,
no retry), with 0.5 CPU / 1GiB hosting for at most 30 minutes and explicit
cleanup. Registry storage, compute, telemetry and model costs were approved.
**Missing spend approval is no longer the blocker.**

Read-only checks against source `f927b2c0bc23c615eecc95919a37a9c4686c0ff0`
completed at **2026-09-08T11:43:40Z**:

- Existing ACR `fcagdevqhg3qc4rlbt4gacr` uses
  `LegacyRegistryPermissions`; the publisher has an existing inherited Owner
  role. No publisher grant or registry/network change is necessary.
- Existing project `fantasy-cards-dev` is in `eastus2`, a currently supported
  hosted-agent region. The Cognitive Services location-usage response contained
  no hosted-agent quota entries; this is **unknown capacity**, not zero usage or
  verified available quota.
- The project and account connection inventories each returned only
  `fcagdevqhg3qc4rlbt4gacr-conn` (`ContainerRegistry`, `ManagedIdentity`).
  No Application Insights connection or project telemetry property was present.
  Existing Insights component `fcag-dev-appi` exists in `rg-fcag-dev`, but its
  existence is **not** a project binding.
- The earlier checkpoint incorrectly treated a missing Application Insights
  link as a deployment blocker. Gandalf verified installed help and upstream
  revision `16f49c2`: this is a tracing prerequisite only. The approved smoke
  proceeds without a telemetry connection or credential changes. The earlier
  checkpoint did not attempt deployment; no connection string was read.
- The platform creates the agent's runtime identity on deployment and provides
  project model access by default; absence of a pre-created runtime identity
  was **not** treated as a blocker. No extra identity grant was requested.
- `GET /agents?api-version=v1` returned zero agents. No image was built/pushed,
  no hosted version/session was created, and **zero invocation attempts / zero
  model calls** occurred. Deployment/start/stop timestamps and image digest /
  hosted version are **not applicable**, rather than a claimed successful stop.

Installed `azure.ai.agents` beta.13 has **no `azd ai agent stop` command**.
The supported command is `azd ai agent sessions stop <session-id>`, which
terminates session compute and retains its filesystem; a later invocation can
resume it. Current platform documentation describes per-session sandboxes,
not configurable replicas. There is no supported endpoint-disable field/command
in the verified installed toolchain; the prior disable claim is withdrawn.
Cleanup paginates sessions, selects exact `version_indicator.agent_version`,
stops/deletes those sessions, and force-deletes **only the new version created
this run**. Verify no active matching sessions and exact-version GET 404.
Timeout/403 is not cleanup evidence. Start the 30-minute clock at deployment
submission and use `finally`; a deploy timeout does not cancel server-side work.

Before/after ACA projections were identical: container `web`, image
`fcagdevqhg3qc4rlbt4gacr.azurecr.io/fantasy-cards-generator/web-nat-dev:azd-deploy-1788775195`,
latest/ready revision `fcag-dev-app--azd-1788775203`, `Single`, latest traffic
100%, principal `946d8701-48f2-4fa5-8efd-bf053c7b4e4c`.
No Azure mutation command ran during this checkpoint, and no root hook,
production change, evaluation or dependency installation ran. The previously
verified actual ACA-MI access evidence remains valid but **invocation remains
unproven**. #109 stays open; PR #120 is not merged. The ACA endpoint-injection
gap and production latency/privacy acceptance remain separate.

Verify only the expected assignments/connection via safe projections; allow RBAC
propagation. Then build/readiness/runtime tests, privacy review, region/quota
checks and explicit compute/model-spend approval are separate deployment gates:

```bash
AZURE_DEV_USER_AGENT=microsoft_foundry_skill python deploy.py deploy --execute --approve-change
```

This creates a **new immutable Foundry agent version** and can incur compute
costs. The resource booleans do not disable this explicit deploy action.
Do not assume 0.5 CPU is production sizing; review observed readiness/latency
before changing the allocation. No production deploy is supported by the launcher.

## ACA endpoint injection: a separate stop gate

### Executed approved DEV smoke — 2026-09-08

The approved run **did execute** against the existing resources, without an
Application Insights link, telemetry IaC, root hooks, evaluation, web activation,
new model capacity, network changes or additional role assignments.

| Evidence | Actual result |
| --- | --- |
| Application/image source | `0dfb3ef8e47c29f86d6936eaedcea17ab6871334` |
| Registry image | `fcagdevqhg3qc4rlbt4gacr.azurecr.io/card-orchestrator:0dfb3ef8e47c29f86d6936eaedcea17ab6871334` |
| Registry digest | `sha256:22c7b63b85ad90bf4a2513437660b2c9eccec6de5de3e06d22ed1a64847584c6` |
| Deployment submission / clock start | `2026-09-08T11:57:15.473661Z` |
| Dedicated azd deploy | Exit 0, `2026-09-08T11:58:35.608805Z` |
| New hosted version | `card-orchestrator`, version **`1`**, observed `active` |
| Version-ref session | `02ea88d182aae88700SxWkhpky6493MLlK0tvxy7EiFIyYXZUP`, observed `active` at `11:58:46.431496Z` |
| ACA invocation dispatches | **1**, at `11:58:46.437594Z`; **0 retries** |
| Invocation outcome | `exec_no_evidence`, at `11:58:56.224365Z`; no validated card/refusal/held response |
| Session stop / delete | Exit 0 at `11:59:01.561745Z` / `11:59:03.373857Z` |
| Exact new-version force deletion | Exit 0 at `11:59:07.138924Z` |
| Independent cleanup checks | Exact version GET **404**, exact session GET **404**, agent GET **404**, session-list endpoint **404**, each `not_found` |
| Web baseline | Unchanged image, container, latest/ready revision, Single/latest 100% traffic, ingress and identity |
| Endpoint persisted in ACA | **false** |

The control script's first cleanup summary conservatively reported failure
because session listing returned 404 after deleting the sole version (the agent
endpoint disappeared too). Independent exact-resource GETs then confirmed
version/session/agent absence. This is **not** an HTTP-200 empty-list result:
the absence evidence is successful explicit stop/delete plus exact-resource and
endpoint 404s, not an ignored 403/timeout. Compute/session termination completed
within two minutes of submission, well inside the approved 30-minute window.
No unrelated version or shared resource was deleted. The image remains in ACR
and can continue incurring storage charges.

**One ACA exec invocation was dispatched; whether its Responses POST reached
Foundry is unknown (0 or 1), and its allowance is consumed.** No retry, developer
inference call or model repair was attempted. Model-call/token usage is
unobserved, not zero; code limits remain three calls, 1800 output tokens/stage,
20 seconds/stage and 65 seconds overall. The earlier explicit-MI access probe
proved the expected ACA principal, but this invocation exported no new identity
or schema evidence and must not be called an end-to-end success.

Live preparation found and fixed two manifest defects: extension service paths
cannot escape the isolated project; container builds need `language: docker`
to avoid a host-side requirements.txt restore. The failed Python restore created
a local virtual environment but installed no project dependencies; that local
artifact was removed. Actual agent Dockerfile build, packaging and push then
succeeded through the dedicated azd project.

An offline reproduction produced an encoded invocation command over 5700
characters. Canonical PTY line limits are a plausible transport failure, not a
proven service/model diagnosis.
The subsequent source-only fix splits it into lines of at most 1024 characters
and reconstructs it in memory; a canonical-PTY regression test passes. This
fix was **not** retried live and is not part of the published image source.
Targeted offline probe/deployment validation after the fix: **107 tests passed**.

#109 remains open. Actual invocation/schema/MI evidence, runtime model
authorization, production latency/privacy acceptance and separately reviewed
ACA endpoint injection remain unproven or out of scope. Approval for this
single consumed smoke does not authorize another invocation.

### Newly authorized DEV smoke — 2026-09-08

Requester `@bmoussaud` explicitly approved a **new** temporary DEV runtime and
one new synthetic request after the preceding allowance was consumed. This run
used that new allowance, not a retry under the old one. Bounds were unchanged:
0.5 CPU / 1GiB, at most 30 minutes from deployment submission, one Responses
request, at most three existing `gpt-5-5` calls, no retry, immediate owned-resource
cleanup. Instrumentation remained disabled; no Insights link was required or added.

Before any deployment, the current whole-parser/chunk bundle completed
`--prepare-invocation --execute` inside the pinned serving ACA replica:
`invocation_prepared`, `parserImportReady:true`, `requestSchemaReady:true`,
`localFixtureParseReady:true`, `tokenAcquired:true`, `principalMatched:true`,
`accessVerified:true`, HTTP **200**, empty agents page, **zero** invocations.
Its labelled local fixture is not real hosted-card evidence. Offline validation
of this revision passed **149 targeted probe/deployment tests**, Ruff and Black.

Fresh isolated azd `dev` state was initialized using verified nonsecret bindings
only; no existing dotenv/state was copied or read. The agent manifest retained
`project: "."`, `language: docker`, the repository build context and dedicated
agent Dockerfile. No root hooks or provisioning ran. The immutable source
remained unchanged through build, package, push and invocation.

| Evidence | Actual result (UTC) |
| --- | --- |
| Application/image source | `2bdbf9967d8c397f7d88914bac06285b3b477297` |
| Registry image | `fcagdevqhg3qc4rlbt4gacr.azurecr.io/card-orchestrator:2bdbf9967d8c397f7d88914bac06285b3b477297` |
| Registry digest | `sha256:cd23ea7a8f782eb434659d3c774646599df93bcbbfdb3322c1313f158f1f3989` |
| Deployment submission / clock start | `2026-09-08T12:45:17.427155Z` |
| Dedicated azd deploy | Exit 0 at `12:46:40.547790Z` |
| Newly created hosted version | `card-orchestrator`, **`1`**, observed `active` |
| Exact version-ref session | `smoke-109-2bdbf9967d8c-1788871606`, observed `active` at `12:46:58.064908Z` |
| Actual ACA target | `fcag-dev-app--azd-1788775203-69f9dc897b-c97p9`, container `web` |
| Expected/actual principal match | `946d8701-48f2-4fa5-8efd-bf053c7b4e4c`; `tokenAcquired:true`, `principalMatched:true` |
| Invocation dispatch / result | **1** at `12:46:58.065475Z`; HTTP **403** at `12:47:16.550685Z`; **0 retries** |
| Sanitized invocation marker | `status:failed`, `reason:http_error`, `invocationsAttempted:1`, `accessVerified:false`, `invocationVerified:false`, `schemaValid:false` |
| Domain/application/hosted-version validation | No validated domain result; both version-match checks **unobserved**, not successful |
| Session stop / delete | Exit 0 at `12:47:26.266723Z` / `12:47:28.886497Z` |
| Exact new-version force deletion | Exit 0 at `12:47:32.826264Z` |
| Independent exact-resource checks | Version, session, agent and session-list GETs all HTTP **404**, completed `12:49:32.516169Z` |
| Web/endpoint | Allowlisted baseline identical; `endpointPersisted:false` |

The platform reused version number `1` after the prior sole version was deleted;
this is a newly created version with a different immutable application SHA, image
digest and session, not reuse of the previous runtime. Initial agent inventory
was explicitly empty. Ownership was recorded before invocation, including the
caller-assigned session ID before session creation.

The control script entered `finally` immediately after the response. Successful
stop/delete/version deletion completed **135.4 seconds** after submission. Its
first verification conservatively failed because Azure CLI omitted the HTTP
status from its `not_found` rendering and post-deletion azd lookups returned
errors without usable absence evidence. Two further bounded cleanup attempts
also returned errors; they did not recreate resources. Independent SDK GETs to the exact
version and documented `/agents/{name}/endpoint/sessions/{id}` paths then
observed HTTP 404 directly, without printing bodies, headers or credentials.
The session-list endpoint also returned 404: this is endpoint absence, **not**
a fabricated empty HTTP-200 page. Verification completed within 4m16s of
submission; no runtime was left running.

**403 diagnosis and limits:** the POST was made by the expected actual ACA MI,
not a developer credential fallback. Its successful preparation GET proves
project-list access only. Subsequent nonbillable ARM projections confirmed the
existing project-scoped **Foundry Agent Consumer** assignment
`ee5a9eb3-9011-50f0-851b-5ca98438c8b1` and inherited account-scoped Cognitive
Services User assignment. The consumer role's current data action is
`Microsoft.CognitiveServices/accounts/AIServices/endpoints/interact/action`.
The account reported `publicNetworkAccess:Enabled`, `networkAcls:null`.
No role or network setting was changed. The probe intentionally discarded the
HTTP error body/headers; no more specific service error code or request ID was
exported. Therefore neither a missing grant, session-owner restriction nor a
runtime/model failure is established. HTTP 403 is not a domain policy refusal.
Do not send another prompt to diagnose it without another explicit approval.

The allowance is now consumed. Model-call and token usage are unobserved, not
asserted zero; enforced source bounds remain three calls, 1800 output tokens per
stage, 20 seconds per stage and 65 seconds overall. No evaluations, image
generation, production changes, new capacity, manual roles, network relaxation,
secret changes, app persistence or endpoint injection occurred. Agent-image
dependencies remain governed by the existing frozen lockfile; no host dependency
installation ran.
Only this run's hosted version/session were deleted. Its new immutable ACR image
and the prior image remain and can incur storage cost, in addition to already
incurred build/compute and any platform telemetry/model charges.

Issue #109 remains open: actual completed domain/schema/version evidence and
runtime model authorization remain unproven; separately reviewed ACA endpoint
injection, web integration and production latency/privacy acceptance remain out
of scope. PR #121 publishes the reviewed probe and truthful run evidence only,
not a claim that the acceptance criteria are complete.

The latest actual ACA-MI probe returned an empty agent inventory; the project
endpoint remains absent from ACA environment configuration. The three
prerequisite grants now exist. The repair above does not write the ACA resource,
and endpoint injection is **not a prerequisite for the one-off MI probe**.

Before any endpoint injection, capture only this safe baseline:

```bash
az containerapp show --resource-group "<existing-dev-rg>" --name "<app-name>" \
  --query "{id:id,revision:properties.latestReadyRevisionName,mode:properties.configuration.activeRevisionsMode,traffic:properties.configuration.ingress.traffic,containers:properties.template.containers[].{name:name,image:image},projectEndpoint:properties.template.containers[].env[?name=='FOUNDRY_PROJECT_ENDPOINT'].value}" \
  --output json
```

Keep this nonsecret baseline outside tracked files. Preserve the exact deployed
image/digest, container names, ingress weights/labels, revision mode and all
existing environment/secret references. Changing an environment value creates
an ACA revision; preserving traffic policy does not mean retaining the same
revision ID. Do not send traffic to an unverified revision.

**Do not run root `azd provision`, including preview, as an endpoint patch.**
Its prehook can generate a session secret, its posthook appends an Entra secret,
and an empty image parameter selects a bootstrap image. Current root
`container-apps.bicep` fixes revision mode to `Single` and does not explicitly
model traffic weights, so it cannot safely replay arbitrary live traffic.

The concrete apply gate is a separately reviewed, hook-free Bicep/azd
configuration update that consumes the verified current image and explicitly
preserves the observed traffic/revision policy and existing secret references.
Review its what-if and reject anything beyond the intended endpoint env value
and unavoidable new revision. If the existing root module cannot represent the
baseline, coordinate that change with its owner **before** applying; this package
intentionally contains no generic whole-resource PUT, root provisioning command
or blind CLI env patch. Re-read image/traffic after apply and compare with the
baseline before approving web traffic. Do not activate agent-backed generation
as part of endpoint injection.

## Independent monitoring and rollback (#99)

Keep release dimensions separate:

- Web application version/revision.
- Agent application build SHA (`CARD_ORCHESTRATOR_VERSION`) and registry digest.
- Foundry agent name + immutable agent version (platform values).
- Existing model deployment name; model version changes are another release.

Before release, prove the integrated runtime emits sanitized request outcome
(success/error/timeout/rejection), elapsed milliseconds and readiness status.
Model use must include deployment name and token counts when the SDK supplies
them; absent usage is unknown, **not zero**. Correlate with random request/trace
IDs and the version dimensions above, never user IDs or raw card text.

Disable content capture in SDK/MAF/OTel instrumentation and verify exported
telemetry with synthetic nonpersonal inputs before enabling exporters. Do not
record prompts, completions, tool arguments/results, card descriptions, photos,
tokens or connection strings. Raw exception bodies can contain model output and
must not be emitted. Do not enable debug/body tracing to diagnose readiness.

Use existing project/Insights telemetry for on-demand outcome/latency/model-use
and startup inspection. Readiness is a local server check, not a model
completion. The container healthcheck uses only `/readiness`; it neither invokes
the model nor represents a Foundry platform probe configuration. No dashboards,
alert rules, recurring evaluations, billable synthetic model probes, ingestion
budget or production SLO have been provisioned by this change.

Keep a release record of commit, digest, Foundry version, configuration and the
last approved version. A rollback is **agent-only**: route an approved caller to
the recorded immutable Foundry version when version pinning is supported, or
deploy the recorded image/configuration as a new agent version through this
isolated azd service. Preserve the original image and app build SHA; record the
new Foundry version separately. Confirm the current extension's version/image
selection interface before executing—do not guess an `azd rollback` command.
The hosted agent endpoint serves one version with 100% traffic; agent-version
traffic splitting is not supported and is not a rollback strategy.
Stop idle/candidate sessions using the supported agent session lifecycle after
approval; retain versions needed for rollback. Do not redeploy the web image or
change its traffic to roll back an independently deployed agent.

An optional single remote smoke request needs explicit model-spend approval
after deployment and must check schema/outcome, not merely HTTP 200. No recurring
probe is authorized. Keep the web path disabled until its separate integration,
safety and rollback review is complete.
