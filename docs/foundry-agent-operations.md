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

The container expects the integrated `hosted-agent` optional extra and runtime
module. Packaging-only tests do not prove the server starts. The container build,
readiness test, and adapter/runtime integration must pass after combining the
runtime and deployment commits; do not create placeholder modules to pass them.

The platform supplies `FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_AGENT_NAME`,
`FOUNDRY_AGENT_VERSION`, and the Application Insights connection configuration.
Do **not** redeclare reserved values in service `environmentVariables`.
Only `AZURE_AI_MODEL_DEPLOYMENT_NAME` and `CARD_ORCHESTRATOR_VERSION` are supplied
by this manifest. The latter is the full immutable application Git commit, not a
Foundry version number. Runtime timeout/policy defaults belong to the runtime;
inspect their bounded values during integration rather than adding guessed SDK
settings here.

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
The installed CLI help and upstream source were inspected; a live deployment
against this package has **not** been verified.
The hosted-agent observability documentation confirms platform injection of the
Application Insights connection string; no speculative connection resource is
added by this package.

## Offline integration gate

From the repository root, after integrating the real runtime and lockfile:

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

azd's service `project: ../..` resolves to the repository root from the dedicated
folder. Docker `path` and `context` are relative to that service root, not to the
manifest folder. `remoteBuild: false` avoids uploading a broad ACR build archive.
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

Confirm the existing Foundry project is connected to the intended Application
Insights resource and the platform will inject its connection configuration.
This package neither creates an Insights resource nor guesses an undocumented
connection shape. Missing telemetry binding is a deployment-readiness blocker;
fix it through reviewed existing-resource IaC before declaring monitoring ready.

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

The prior preflight reported an empty agent inventory and missing dev ACA
project endpoint plus two roles. Treat that as historical evidence, not a
current live observation. The repair above does not write the ACA resource.

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
