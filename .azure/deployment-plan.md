# Azure Deployment Plan

> **Status:** Ready for Azure Validation (local validation complete)

Generated: 2026-08-31T10:58:12.993+02:00  
Issue: [#42 — Activate Application Insights telemetry and operational monitoring](https://github.com/bmoussaud/fantasy-cards-generator/issues/42)

---

## 1. Project Overview

**Goal:** Activate privacy-safe, end-to-end operational telemetry for the existing Python 3.12 FastAPI application on Azure Container Apps. Reuse the workspace-based Application Insights and Log Analytics resources already declared in Bicep, instrument application and Azure dependency paths with the supported Azure Monitor OpenTelemetry distribution, add health probes, and manage dashboards and alerts as code without changing generation, authentication, moderation, retry, or persistence behavior.

**Path:** Add Components

**Mode:** MODIFY

**Current state:**

- The app is a single synchronous FastAPI web/API container, started by Uvicorn as `app.main:app`.
- `app.main` imports FastAPI and `app.generation`; `app.generation` imports `httpx` before any telemetry initialization exists.
- `infra/modules/monitoring.bicep` already creates one Log Analytics workspace and one workspace-based Application Insights component.
- `infra/modules/container-apps.bicep` already stores and injects `APPLICATIONINSIGHTS_CONNECTION_STRING` through an ACA secret reference.
- `/healthz` already returns a dependency-free `200 {"status":"ok"}`, but ACA probes are not configured.
- Existing application logs describe generation, dependencies, retries/timeouts, partial results, and persistence failures, but they are string-formatted and are not exported by an Azure Monitor SDK.
- The application already generates/echoes `X-Request-ID`; it does not yet validate the inbound value or map it to OpenTelemetry diagnostics.

**Non-goals and behavior constraints:**

- Do not create a second Log Analytics workspace, Application Insights component, or connection-string path.
- Do not replace the existing generation audit record or change application outcomes.
- Do not change authentication, moderation policy, retry counts, timeouts, idempotency, persistence ordering, compensation, response schemas, or synchronous request behavior.
- Do not build a general analytics warehouse or record prompt/generated content.
- No Azure deployment, provisioning, portal change, or live-ingestion test is authorized by this plan-first phase.

---

## 2. Requirements

| Attribute | Value |
|-----------|-------|
| Classification | Production workload, based on issue wording and acceptance criteria |
| Scale | Low traffic: fewer than 100 requests/day; alerts are deployed disabled while dashboards establish a baseline |
| Budget | 100% parent-consistent trace sampling in dev/prod, configurable 0.25 GB/day workspace cap, warning at 80% |
| Compliance / privacy | Strict telemetry allowlist; no content, credentials, identity, or raw network identifiers |
| Availability | `/healthz` must drive ACA startup, liveness, and readiness probes and an external availability view |
| Rollback | A bad revision must fail probes before serving traffic; current Single revision behavior must remain recoverable through a known-good image/revision |
| **Subscription** | Deferred to deployment handoff; no Azure context lookup or deployment is authorized now |
| **Location** | Deferred to deployment handoff; confirm with the subscription before validation/deployment |

### Acceptance coverage

1. Correlate an inbound FastAPI request through moderation, Foundry text/image calls, Blob Storage, and Cosmos DB by W3C `traceparent`/Application Insights `operation_Id`, while retaining a sanitized `X-Request-ID` diagnostic.
2. Query normalized requests, dependencies, exceptions, structured logs, retries, timeouts, throttles, partial results, moderation outcomes, and persistence failures.
3. Emit bounded business telemetry and aggregate latency/token metrics without sensitive payloads or high-cardinality business identifiers.
4. Provide an IaC-managed workbook for request, dependency, exception, ACA, generation, and ingestion health.
5. Provide IaC-managed alerts routed to an IaC-managed Action Group.
6. Configure and document sampling, retention, a daily cap, and an ingestion-cost estimate.
7. Configure ACA startup/liveness/readiness probes on `/healthz` and preserve rollback safety.
8. Test instrumentation, propagation, redaction, and IaC locally without live Azure ingestion.

### Explicit telemetry privacy contract

Telemetry MUST NOT contain:

- prompts, derived prompts, generated text, generated images, or any generated card fields;
- request or response bodies, form bodies, query-string values, or model payloads/responses;
- credentials, connection strings, access/ID tokens, API keys, authorization headers, cookies, CSRF values, or idempotency keys;
- user subjects, tenant/object/owner identifiers, display names, email addresses, or other user identifiers;
- card IDs, blob names/IDs, storage paths containing identifiers, or hashes derived from user/card/content values;
- raw client IPs, `X-Forwarded-For`, peer/network addresses, or unfiltered request headers;
- exception messages or stack attributes that can echo any prohibited value.

Controls planned:

- Use a central allowlist for custom span/log/event/metric attributes and bounded error codes.
- Keep HTTP body and request/response header capture disabled.
- Normalize routes and remove query strings before export, including the Entra callback authorization code.
- Never add `client_ip`, owner/card/blob/idempotency values, or auth/session data to telemetry.
- Keep Application Insights IP masking enabled and remove raw network attributes before export.
- Add automated sentinel redaction tests across spans, logs, events, and metrics.

---

## 3. Components Detected

| Component | Type | Technology | Path |
|-----------|------|------------|------|
| Web/API entry point | API + server-rendered web app | Python 3.12, FastAPI, Uvicorn | `app/main.py`, `Dockerfile` |
| Authentication | OIDC session auth | Authlib, Entra ID, Starlette sessions | `app/auth.py` |
| Generation orchestration | Synchronous workflow | asyncio, Pydantic, bounded retries/timeouts | `app/generation.py` |
| Foundry dependencies | Outbound HTTP | `httpx.AsyncClient`, managed identity | `app/generation.py` |
| Persistence | Azure SDK dependencies | Cosmos DB async SDK, Blob Storage async SDK | `app/generation.py` |
| Runtime settings | Environment configuration | frozen dataclasses | `app/settings.py`, `.env.example` |
| Monitoring foundation | Existing Azure resources | workspace-based Application Insights + Log Analytics | `infra/modules/monitoring.bicep` |
| Container hosting | Azure Container Apps | Single revision, min 1/max 2 replicas | `infra/modules/container-apps.bicep` |
| Infrastructure composition | AZD + Bicep | subscription-scope orchestration | `azure.yaml`, `infra/main.bicep`, `infra/main.parameters.json` |
| Validation | CI and local tests | pytest, Ruff, Black, Bicep CLI, Docker | `tests/`, `.github/workflows/pr-validation.yml` |
| Operational documentation | Run/deploy docs | Markdown | `README.md`, `infra/README.md`, `docs/card-generation-api.md` |

### Dependency and startup findings

- Before implementation, `azure-monitor-opentelemetry` and explicit HTTPX instrumentation were absent from `pyproject.toml` and `uv.lock`.
- `app/main.py` imports FastAPI at line 11 and generation code at line 33; generation imports `httpx` at line 16.
- The production command imports `app.main:app` directly, so adding configuration inside `create_app()` would be too late for reliable FastAPI/HTTP client auto-instrumentation.
- Azure Cosmos and Blob clients are imported lazily inside constructors, but their instances are created during `create_app()` in live mode.
- Tests intentionally set environment variables before importing `app.main`; telemetry must remain disabled and network-free by default in tests.
- Existing CI already runs dependency sync, lint, format, the full pytest suite, Bicep restore/build, and a Docker build. A workflow change is not expected unless implementation proves an additional gate is necessary.

### Existing operational paths to instrument

- Request middleware and exception handlers in `app/main.py`.
- `CardGenerationService.generate_card`, `retry_artwork`, `_run_generation`, `_retry_upstream`, `_persist_partial`, `_persist_completed`, compensation, and audit-failure paths.
- `AzureFoundryAIClient` HTTP calls, Azure Cosmos operations, Azure Blob operations, and Entra HTTP dependencies.
- Moderation stages: `pre_prompt`, `post_text`, `post_art_prompt`, and `post_image`.
- Outcomes: completed, partial/awaiting artwork retry, rejected, throttled, timed out, dependency failed, persistence failed, replayed/idempotency conflict.

---

## 4. Recipe Selection

**Selected:** AZD (Bicep), modifying the existing Bicep modules

**Rationale:**

- `azure.yaml` already selects Bicep and Azure Container Apps.
- Team decision requires all shippable Azure resources to be Bicep-managed and prefers AVM where it supports the required resource.
- Existing monitoring and ACA modules must be extended rather than replaced.
- Azure CLI/Portal-created workbooks, alerts, Action Groups, or probes would violate the repository's IaC policy.
- `azd init` and template re-initialization are explicitly out of scope for this existing project.

**Duplicate prevention:**

- Keep `infra/modules/monitoring.bicep` as the sole owner of the existing workspace and Application Insights component.
- Keep `infra/modules/container-apps.bicep` as the sole owner of the existing `APPLICATIONINSIGHTS_CONNECTION_STRING` ACA secret and environment variable.
- Reference existing resource IDs/outputs when creating workbook, alert, availability, and Action Group resources.
- If a separate post-app operational-monitoring module is needed to avoid a dependency cycle, pass the existing workspace/App Insights IDs and the ACA ID/FQDN into it; do not redeclare the foundational resources.

---

## 5. Architecture

**Stack:** Existing Azure Container Apps deployment with Azure Monitor OpenTelemetry and Bicep-managed Azure Monitor resources

### Service Mapping

| Component | Azure Service | SKU / mode |
|-----------|---------------|------------|
| FastAPI web/API | Existing Azure Container App | Existing Consumption workload profile, Single revision, min 1/max 2 |
| Request/dependency/log/exception telemetry | Existing workspace-based Application Insights | Existing web component |
| Central logs and KQL | Existing Log Analytics workspace | Existing `PerGB2018` |
| Operational dashboard | Azure Monitor Workbook | One per independently monitored environment |
| Notification routing | Azure Monitor Action Group | One per independently monitored environment unless sharing is approved |
| Operational alerts | Scheduled-query and metric alerts | Eight planned rules per independently monitored environment |
| External health availability | Application Insights standard availability test + metric alert | One `/healthz` test per independently monitored environment |

### Supporting Services

| Service | Purpose |
|---------|---------|
| Existing Log Analytics | Application Insights and ACA console/system telemetry |
| Existing Application Insights | Requests, dependencies, exceptions, traces, logs, custom spans/events/metrics |
| Existing managed identity | Foundry, Cosmos, and Blob authentication; no telemetry credential added |
| Existing ACA secret wiring | Supplies the existing Application Insights connection string |
| Azure Monitor Workbook | Review request volume, success/failure, p50/p95/p99 latency, dependency health, exceptions, ACA replicas/restarts, generation outcomes, and ingestion |
| Azure Monitor Action Group | Route all issue-required operational alerts to approved recipients |

### Application initialization and resource identity

1. Add a dedicated bootstrap module (planned as `app/entrypoint.py`) that:
   - imports a small telemetry configuration module;
   - calls the supported `azure.monitor.opentelemetry.configure_azure_monitor(...)`;
   - only then imports `app.main`, FastAPI, `httpx`, Authlib, and Azure SDK clients.
2. Point production Uvicorn and documented local telemetry startup at `app.entrypoint:app`.
3. Keep telemetry disabled/no-op when no connection string is present and explicitly disabled in tests; no exporter may attempt network I/O in unit tests.
4. Use supported distro parameters/environment configuration for connection string, sampling ratio, logger namespace, and OpenTelemetry `Resource`.
5. Set stable resource dimensions:
   - `service.name` / Application Insights cloud role: `fantasy-cards-generator`;
   - `service.namespace`: `fantasy-cards-generator`;
   - `deployment.environment.name`: normalized `development` or `production` (resource names remain `dev`/`prod`);
   - `cloud.platform`: Azure Container Apps;
   - `service.version` or a dedicated bounded deployment dimension from the image/release;
   - ACA revision and replica from platform-provided runtime variables.
6. Do not use user, request, card, blob, IP, prompt, or arbitrary URL values as resource or metric dimensions.

### W3C correlation and `X-Request-ID`

- Retain W3C Trace Context as the source of distributed trace identity; do not replace the OpenTelemetry trace ID with `X-Request-ID`.
- Accept an inbound `X-Request-ID` only when it matches a strict ASCII allowlist and maximum length; otherwise generate a new opaque value.
- Continue returning the sanitized/generated value in the response header.
- Attach it as `app.request_id` to the inbound request span, privacy-safe structured logs, and explicit generation/stage spans.
- Do not use `app.request_id` as a metric dimension.
- Let supported HTTP/Azure SDK instrumentation propagate `traceparent`/`tracestate`. Explicit custom spans around moderation and persistence retain the current context so Foundry, Cosmos, Blob, and Entra dependency spans share `operation_Id`.
- Validate that outbound dependency spans redact query strings and headers. Never propagate the inbound request ID to third parties unless implementation demonstrates a required, allowlisted Azure endpoint contract.

### Bounded custom telemetry model

All attributes below use enums/allowlists or numbers. `app.request_id` is diagnostic-only and excluded from metrics.

| Signal | Planned name | Bounded attributes |
|--------|--------------|--------------------|
| Lifecycle span/events | `fcg.generation`; `generation.started/completed/failed/partial` | operation, stage, outcome, normalized error code, retryable |
| Generation count | `fcg.generation.requests` counter | operation, outcome |
| Generation latency | `fcg.generation.duration` histogram | operation, outcome |
| Partial results | `fcg.generation.partial_results` counter | reason (`image_timeout`, `image_failure`, `moderation_rejection`) |
| Artwork retry | `fcg.artwork.retries` counter | outcome, normalized reason |
| Dependency attempts | `fcg.dependency.attempts` counter | dependency (`foundry_text`, `foundry_image`, `cosmos`, `blob`, `entra`), attempt bucket, outcome |
| Dependency latency | `fcg.dependency.duration` histogram | dependency, operation, outcome |
| Throttling/timeouts | `fcg.dependency.throttles`, `fcg.dependency.timeouts` counters | dependency, operation |
| Moderation | `fcg.moderation.decisions` counter and span event | stage, outcome (`allowed`, `blocked`), allowlisted reason code, policy version |
| Persistence | `fcg.persistence.operations` counter and stage span | store (`cosmos`, `blob`, `audit`), operation, outcome, normalized error code |
| Aggregate token usage | `fcg.ai.tokens` counter | operation (`text`, `image`), token type (`input`, `output`, `total`), bounded model/deployment alias |

Additional rules:

- Auto-instrumented request names use FastAPI route templates, never raw card IDs.
- Azure status codes are numeric; Azure error codes are sanitized, length-bounded, and mapped to a controlled fallback when unknown.
- Metrics are unsampled and preserve aggregate rare-outcome counts. Trace sampling is parent-consistent; operational failures remain visible through unsampled counters/alerts even when an individual trace is not retained.
- Structured logs use a dedicated application logger namespace and `extra` fields from the same allowlist rather than embedding arbitrary values in messages.

### Sampling, retention, cap, and cost

- Expose trace sampling ratio as validated configuration (`0 < ratio <= 1`) and use the Azure Monitor distro's supported parent-consistent sampling option. Default to `1.0` (100%) in both dev and prod; operators may override it at deploy time.
- Do not apply trace sampling to metric aggregation. Keep failure, rejection, throttle, timeout, partial-result, and persistence-failure counters available for alerting.
- Parameterize Log Analytics retention and default it to the approved 30 days.
- Parameterize the workspace-based Application Insights daily ingestion cap at the shared workspace layer. Default to 0.25 GB/day with a warning at 80% (0.2 GB); operators may override both values at deploy time.
- Keep IP masking enabled and local authentication disabled where supported without breaking connection-string ingestion.
- Document the EUR estimate for the existing East US 2 deployment guidance from the Azure Retail Prices API: at €2.425/GB, a fully used 0.25 GB/day cap is €18.19 per environment for 30 days before allowances, or about €6.06 if the first 5 GB/month allowance applies. Recheck the official rate if region or billing offer changes.
- The workbook will show recent ingestion (`Usage`/billable data), trend, projected daily volume, configured cap, and cap utilization.

### Workbook and KQL

Manage a workbook definition in Bicep, scoped to the existing Application Insights/workspace, with environment/role/revision filters and panels for:

- request volume, success/failure ratio, status classes, and p50/p95/p99 duration;
- dependency volume, success rate, p50/p95/p99 duration, throttles, timeouts, and normalized dependency/error codes;
- exceptions and bounded structured application errors;
- generation lifecycle outcomes, moderation outcomes, retries, partial results, persistence outcomes, and aggregate tokens;
- ACA replicas, restart count, revision health, and console/system errors using the actual workspace table schema;
- telemetry ingestion volume, projected daily usage, and cap utilization.

KQL must support both Application Insights workspace tables and the repository's configured ACA Log Analytics destination. During implementation, confirm whether ACA tables are `ContainerApp*Logs` or legacy `ContainerApp*Logs_CL` and encode the deployed schema rather than guessing.

### Alert and Action Group wiring

Create Bicep-managed rules routed to the configured Action Group. The Action Group is created with empty receiver arrays, and all alert rules deploy disabled for the initial dashboard-only rollout. Later activation requires both approved receiver configuration and the explicit alert master switch:

1. `/healthz` availability test failure.
2. Elevated request 5xx/failure ratio with a minimum traffic floor.
3. Sustained request p95 latency.
4. Dependency failure/throttle/timeout burst.
5. Exception burst.
6. Generation failure/partial-result/persistence-failure burst.
7. Repeated ACA container restarts/unhealthy revision signal.
8. Telemetry ingestion approaching the configured daily cap.

Approved conservative thresholds are: at least 2 failed availability checks in 15 minutes; request failure ratio at least 5% over 15 minutes with at least 20 requests; request p95 latency above 10 seconds over 15 minutes with at least 20 requests; at least 5 failed/throttled/timed-out dependencies in 15 minutes; at least 5 exceptions in 15 minutes; at least 3 failed/partial/persistence generation outcomes in 15 minutes; at least 3 ACA restart/unhealthy events in 15 minutes; and 24-hour billable ingestion at or above 80% of the configured cap. These remain deploy-time parameters.

Alert queries must filter by stable service/environment dimensions, use normalized routes and bounded fields, and avoid evaluating ratios below the approved traffic floor. Action Group receivers are configuration only; no placeholder recipient will be guessed.

### ACA health probes and rollback safety

- Reuse the existing dependency-free `/healthz` endpoint for startup, liveness, and readiness HTTP probes on port 8000.
- Set a startup window long enough for settings and Azure client construction; liveness should detect a wedged process without checking volatile downstream services; readiness should prevent traffic before app initialization completes.
- Keep probe timing parameterized or explicitly documented and test its Bicep shape.
- Keep the current known-good revision/image available during rollout. A new revision must become healthy before being considered successful.
- Because the current app uses `activeRevisionsMode: 'Single'`, deployment handoff must record the current active revision/image and verify the new revision directly; rollback is redeployment/reactivation of the known-good image/revision. Do not change revision mode as an incidental telemetry change.
- Telemetry initialization failure must fail open for application serving when configuration is absent/invalid in local use, but must produce a bounded startup diagnostic in configured Azure environments. Probe health must not depend on successful telemetry export.

---

## 6. Provisioning Limit Checklist

**Purpose:** Inventory the Azure Monitor resources affected by the proposed change. Live quota/capacity checks are intentionally deferred because the user explicitly authorized planning only and required subscription/location confirmation at deployment handoff.

> This plan contains no unknown placeholder cells. Counts are per environment because the approved topology isolates monitoring resources for `dev` and `prod`.

### Phase 1: Prepare Resource Inventory

| Resource Type | Number to Deploy | Total After Deployment | Limit/Quota | Notes |
|---------------|------------------|------------------------|-------------|-------|
| `Microsoft.OperationalInsights/workspaces` | 0 new; 1 existing modified | 1 per independent environment | Live service/policy check deferred to deployment handoff | Reuse existing workspace; configure retention/cap |
| `Microsoft.Insights/components` | 0 new; 1 existing modified | 1 per independent environment | Live service/policy check deferred to deployment handoff | Reuse existing workspace-based component |
| `Microsoft.Insights/workbooks` | 1 per independent environment | 1 per independent environment | Live service/policy check deferred to deployment handoff | New IaC-managed operational workbook |
| `Microsoft.Insights/actionGroups` | 1 per independent environment | 1 per independent environment | Live service/policy check deferred to deployment handoff | Recipient configuration requires approval |
| `Microsoft.Insights/scheduledQueryRules` / metric alert rules | 8 per independent environment | 8 per independent environment | Live service/policy check deferred to deployment handoff | Exact split by alert type follows supported resource schemas |
| `Microsoft.Insights/webtests` | 1 per independent environment | 1 per independent environment | Live service/policy check deferred to deployment handoff | Standard `/healthz` availability test |
| `Microsoft.App/containerApps` | 0 new; 1 existing modified | 1 per application environment | No new ACA capacity requested | Add probes and telemetry resource env dimensions only |

### Phase 2: Fetch Quotas and Validate Capacity

**Status:** Deferred by explicit task scope.

- No subscription or location may be assumed, queried, or confirmed in this plan-only turn.
- No Azure deployment is authorized.
- At deployment handoff, invoke `azure-quotas` first, confirm subscription/location, inspect policy assignments/provider registrations, and validate Azure Monitor/workbook/alert/web-test service limits one resource type at a time.
- Also verify the chosen region supports the required availability-test locations and that alert/query APIs match the selected Bicep API versions.
- Keep each environment's monitoring resources and alert routing isolated.
- Record actual quota/limit sources and capacity proof before `azure-validate`/`azure-deploy`.

**Current status:** ⚠️ Capacity validation is not authorized in this phase; no deployment may proceed from this plan alone.

---

## 7. Execution Checklist

### Phase 1: Planning

- [x] Analyze workspace; mode is MODIFY
- [x] Read team roster, routing, Gimli charter, relevant decisions, issue body, and all comments
- [x] Gather requirements available from issue/repository
- [x] Scan Python/import order, dependencies/lock, middleware/logging/correlation, generation/moderation/retry/persistence paths, tests, Bicep, parameters, CI, and docs
- [x] Select existing AZD (Bicep) recipe
- [x] Research Azure Prepare, Bicep, Application Insights, Azure Monitor OpenTelemetry Python, and ACA references
- [x] Plan architecture, telemetry contract, probes, dashboard, alerts, cost controls, tests, and rollback
- [x] Prepare per-environment resource inventory
- [x] Record why live quota validation and Azure context confirmation are deferred
- [x] Benoit approved the conservative defaults recorded in Section 11
- [x] **User approved this plan on 2026-08-31**

### Phase 2: Execution — approved; in progress

- [x] Add `azure-monitor-opentelemetry` and explicit HTTPX instrumentation through `uv`; update `uv.lock`
- [x] Add early bootstrap and privacy-safe telemetry configuration before FastAPI/HTTP client imports
- [x] Add validated `X-Request-ID` mapping and W3C-correlated spans/logs
- [x] Add bounded lifecycle/dependency/moderation/persistence/token signals without behavior changes
- [x] Extend existing settings/environment configuration
- [x] Extend existing monitoring Bicep resources; do not duplicate workspace/App Insights
- [x] Add operational-monitoring Bicep resources and wire existing resource IDs
- [x] Extend existing ACA module with role/environment/revision dimensions and `/healthz` probes
- [x] Add/adjust unit and IaC contract tests
- [x] Update operational and cost documentation
- [x] Run targeted dependency, Bicep, configuration, test, container build, and health validation
- [x] Run existing CI-equivalent validation commands
- [x] Perform local `/healthz` container verification; no live ingestion
- [x] Update this plan with infrastructure implementation results
- [x] **Only after implementation and before handoff:** change status to `Ready for Validation`

### Phase 3: Validation

- [x] Prerequisite: plan status is `Ready for Validation`
- [x] Invoke `azure-validate`
- [ ] Confirm subscription/location and Azure policy at deployment handoff
- [ ] Invoke `azure-quotas` and populate live capacity proof
- [ ] Validate Bicep what-if only after explicit Azure authorization
- [ ] All validation checks pass
  - [x] Bicep compilation
  - [ ] Azure template validation
  - [ ] What-if preview
  - [ ] Azure authentication confirmation
  - [x] Bicep linting
  - [ ] Azure Policy validation
- [ ] Update status to `Validated` and record proof below

### Phase 4: Deployment

- [ ] Invoke `azure-deploy` only after separate deployment authorization
- [ ] Capture known-good ACA revision/image before rollout
- [ ] Verify new revision startup/liveness/readiness
- [ ] Verify successful, rejected, retried/throttled, timed-out, partial, and persistence-failure scenarios
- [ ] Verify workbook, queries, alerts, Action Group routing, sampling, ingestion, and cap
- [ ] Roll back to known-good revision/image if health or acceptance checks fail
- [ ] Update status to `Deployed`

---

## 8. Validation Proof

> The azure-validate skill must populate this section before status becomes `Validated`. No live Azure ingestion or deployment validation is authorized now.

| Check | Command Run | Result | Timestamp |
|-------|-------------|--------|-----------|
| Azure Validate workflow | Invoke `azure-validate`; load the approved plan and Bicep recipe | ✅ Invoked; live subscription/location, policy, template validation, and what-if remain deferred because Azure deployment/preflight was not authorized | 2026-08-31T13:55+02:00 |
| Dependency lock | `python -m uv lock --check` | ✅ 84 packages resolved; lock current | 2026-08-31T13:55+02:00 |
| Bicep restore/build/lint | `az bicep restore --file infra/main.bicep`; `az bicep build --file infra/main.bicep --stdout`; `az bicep lint --file infra/main.bicep` | ✅ Passed; compiled template preserves the decimal `0.25` GB cap through `json()` | 2026-08-31T13:55+02:00 |
| Parameter/config contracts | Parse `infra/main.parameters.json`; deployment contract tests assert one workspace/App Insights/connection-string env, three probes, eight disabled-by-default alert definitions | ✅ JSON and contracts passed | 2026-08-31T13:55+02:00 |
| Targeted telemetry/deployment tests | `python -m uv run pytest -q tests/test_telemetry.py tests/test_app.py tests/test_generation.py tests/test_deployment_config.py` | ✅ 75 passed; one upstream Starlette deprecation warning | 2026-08-31T13:55+02:00 |
| Ruff and Black | `python -m uv run ruff check .`; `python -m uv run black --check .` | ✅ Passed; 16 files correctly formatted | 2026-08-31T13:55+02:00 |
| Full regression suite | `python -m uv run pytest -q` | ✅ 89 passed; one upstream Starlette deprecation warning | 2026-08-31T13:55+02:00 |
| Container build/runtime health | `docker build --quiet -f Dockerfile -t fantasy-cards-generator:issue-42 .`; run `app.entrypoint:app` locally and request `/healthz` | ✅ Image built; telemetry-first runtime returned `200 {"status":"ok"}` | 2026-08-31T13:55+02:00 |
| Diff integrity | `git --no-pager diff --check` | ✅ Passed | 2026-08-31T13:55+02:00 |

### Static role-assignment verification

- **Status:** Verified; no monitoring-specific role assignment is required because the existing Application Insights connection string is reused.
- **Container App system identity:** Cognitive Services User on the Foundry account, Cosmos DB Built-in Data Contributor at the application container, and Storage Blob Data Contributor on the storage account.
- **ACR pull identity:** AcrPull on the registry; the existing Key Vault Secrets User assignment remains unchanged.
- **Deployment principal:** Foundry User at the Foundry project scope.
- **Result:** Existing data-plane roles remain resource-scoped and cover the application operations; issue #42 adds no broader RBAC.

**Validated by:** Gimli — local/static validation only  
**Validation timestamp:** 2026-08-31T13:55:43+02:00  
**Azure validation status:** Deferred until subscription/location confirmation and separate authorization for template validation, what-if, quota, and policy checks.

---

## 9. Files to Modify and Validation Strategy

### Expected files

| File | Purpose | Status |
|------|---------|--------|
| `.azure/deployment-plan.md` | Source-of-truth plan | ✅ Ready for Azure validation; local checks complete |
| `pyproject.toml` | Add supported Azure Monitor OpenTelemetry distribution and HTTPX dependency instrumentation | ✅ `azure-monitor-opentelemetry>=1.8.9`; `opentelemetry-instrumentation-httpx>=0.64b0` |
| `uv.lock` | Lock the resolved telemetry/OpenTelemetry dependency graph | ✅ Current; existing artifact entries preserved |
| `app/entrypoint.py` | Configure telemetry before importing FastAPI/HTTP clients | ✅ Integrated by application owner |
| `app/telemetry.py` | Supported distro setup, resource attributes, allowlist/redaction, spans/events/metrics | ✅ Integrated by application owner |
| `app/settings.py` | Validated telemetry enablement/sampling/service configuration | ✅ Integrated by application owner |
| `app/main.py` | Sanitized request ID mapping, request-span enrichment, bounded error telemetry | ✅ Integrated by application owner |
| `app/generation.py` | Bounded lifecycle, retry/throttle/timeout, moderation, partial, persistence, and token telemetry | ✅ Integrated by application owner |
| `Dockerfile` | Start Uvicorn through the telemetry-first entry point | ✅ Complete |
| `.env.example` | Document safe local telemetry configuration without a connection string | ✅ Complete |
| `infra/modules/monitoring.bicep` | Parameterize existing workspace/App Insights sampling, retention, cap/privacy settings; no duplicate resources | ✅ Complete |
| `infra/modules/container-apps.bicep` | Keep existing connection-string wiring; add stable dimensions and `/healthz` probes | ✅ Complete |
| `infra/modules/operational-monitoring.bicep` | New workbook, availability test, alerts, and Action Group using existing resource IDs | ✅ Complete |
| `infra/main.bicep` | Wire parameters, existing monitoring outputs, ACA URL/ID, and post-app monitoring module | ✅ Complete |
| `infra/main.parameters.json` | Wire approved monitoring/alert parameters from deployment configuration | ✅ Complete |
| `tests/conftest.py` | Force network-free telemetry behavior in tests and provide test exporters/fakes | ✅ Integrated by test owner |
| `tests/test_telemetry.py` | New span/log/metric, W3C propagation, route normalization, correlation, and redaction tests | ✅ Integrated by test owner |
| `tests/test_app.py` | Validate `X-Request-ID` acceptance/rejection and `/healthz` behavior | ✅ Integrated by test owner |
| `tests/test_generation.py` | Validate bounded signals for success/retry/throttle/timeout/partial/moderation/persistence paths | ✅ Integrated by test owner |
| `tests/test_deployment_config.py` | Validate no duplicate App Insights/wiring, parameters, probes, workbook, alerts, and Action Group | ✅ Integrated by test owner |
| `README.md` | Update startup and telemetry configuration guidance | ✅ Complete |
| `infra/README.md` | Add monitoring operations, cost estimate method, alert setup, validation, and rollback | ✅ Complete |
| `docs/card-generation-api.md` | Document correlation and privacy-safe operational signals | ✅ Integrated by application owner |
| `docs/operational-monitoring.md` | New runbook/query/alert/cost and deployment verification guide | ✅ Complete |
| `.github/workflows/pr-validation.yml` | No change expected; existing generic gates cover implementation | No change planned |
| `app/auth.py` | No behavior change expected; used by redaction tests only | No change planned |

The exact implementation diff may be smaller if the supported SDK can centralize enrichment/redaction without touching a listed component. No unrelated files will be changed.

### Unit/contract validation without Azure ingestion

- Use OpenTelemetry in-memory span/log/metric exporters or injected fakes; never use a real Application Insights connection string.
- Import the production bootstrap under a patched/no-op exporter and assert configuration occurs before `fastapi`, `httpx`, Authlib, and Azure clients.
- Assert W3C `traceparent` continuity and a single shared trace ID across request, generation, moderation, Foundry, persistence, and retry spans.
- Assert valid `X-Request-ID` is mapped diagnostically; invalid/oversized/non-ASCII values are replaced; it never becomes a trace ID or metric dimension.
- Exercise existing deterministic mock markers for success, 429 retry, timeout, partial image, moderation rejection, invalid output, and persistence/compensation failure.
- Send unique forbidden sentinel values in every sensitive field and assert they appear nowhere in exported spans, events, logs, metrics, URLs, exception diagnostics, or resource attributes.
- Assert only allowlisted dimensions and normalized routes/error codes are emitted.
- Assert telemetry disabled/missing configuration never performs network I/O and does not break app startup or `/healthz`.
- Assert Bicep continues to declare exactly one workspace, one App Insights component, and one ACA connection-string secret/env reference.
- Assert all three `/healthz` probes target port 8000 and the workbook/eight alerts reference the existing resources and configured Action Group.

### Smallest existing validation commands covering the change

```powershell
uv lock --check
uv run ruff check app tests
uv run black --check app tests
uv run pytest -q tests/test_telemetry.py tests/test_app.py tests/test_generation.py tests/test_deployment_config.py
az bicep restore --file infra/main.bicep
az bicep build --file infra/main.bicep --stdout
docker build -f Dockerfile -t fantasy-cards-generator:test .
```

### Existing CI-equivalent regression gates

```powershell
uv sync --group dev
uv run ruff check .
uv run black --check .
uv run pytest
az bicep restore --file infra/main.bicep
az bicep build --file infra/main.bicep --stdout
docker build -f Dockerfile -t fantasy-cards-generator:test .
```

No live Azure ingestion is required for implementation tests. Live query/alert/Action Group checks belong only to a separately authorized deployment handoff.

---

## 10. Research Summary

| Reference | Applied conclusion |
|-----------|--------------------|
| Azure Prepare global/analyze/requirements/scan/recipe/architecture/plan guidance | MODIFY mode, plan first, preserve existing AZD+Bicep architecture, defer Azure context/deployment |
| Azure Prepare Bicep recipe/patterns | Reuse modular Bicep, secure values, existing monitoring resources, and source-controlled operational resources |
| Azure Prepare Application Insights reference | Use workspace-based App Insights and a connection string; invoke supported instrumentation |
| Azure Prepare ACA Bicep/health/revision/day-2 references | Add all three probes, validate revision health, use platform metrics/logs, and retain rollback path |
| App Insights Python SDK references | Call `configure_azure_monitor()` early, use resource/cloud-role attributes, structured logs, supported sampling, and instrumented libraries |
| App Insights ACA reference | Existing connection-string secret is correct; W3C/`operation_Id` links instrumented HTTP dependencies; ACA platform metrics/logs complement app telemetry |
| `.squad/decisions.md` | Use ACA, Bicep/AVM-first IaC, App Insights correlation, lifecycle events, dashboards, and preserve synchronous partial-result/retry behavior |
| Issue #42 body/comments | Implement every listed acceptance criterion; only comment is automated assignment to Gimli |

---

## 11. Approved Conservative Defaults

Benoit explicitly approved implementation and conservative recommended defaults on 2026-08-31:

1. **Alert recipients:** email and webhook receiver arrays are deploy-time configuration and default empty. No destination is invented. The Action Group is created without receivers and every alert rule deploys disabled for the initial dashboard-only rollout. Later activation requires configured receivers and `enableAlerts=true`.
2. **SLO/alert thresholds:** 2 availability failures/15m; 5% request failures/15m; 10-second request p95/15m; 5 dependency failures, throttles, or timeouts/15m; 5 exceptions/15m; 3 failed, partial, or persistence-failure generation outcomes/15m; 3 ACA restart/unhealthy events/15m; ingestion warning at 80% of cap.
3. **Traffic:** expected volume is below 100 requests/day. Request ratio and latency thresholds remain configurable and inactive initially while dashboards establish a representative baseline.
4. **Sampling:** parent-consistent 100% trace sampling in both dev and prod. Metrics remain unsampled. Deployments may override the ratio.
5. **Cost controls:** 30-day workspace retention; configurable 0.25 GB/day workspace cap; warning at 0.2 GB/day. East US 2 EUR list-price estimates and their assumptions are documented and must be refreshed if deployment context changes.
6. **Environment topology:** dev and prod use independently named workspace-based App Insights, workspace, workbook, availability test, Action Group, and alerts, matching the existing environment-scoped deployments.

Subscription name/ID and Azure location are not design guesses: they are intentionally deferred to a deployment handoff after approval and separate deployment authorization.

---

## 12. Next Steps

> Current phase: Implementation complete; local validation passed; live Azure preflight and deployment are not authorized

1. Confirm subscription and location before any live Azure preflight.
2. Run Azure template validation, what-if, quota, and policy checks only after authorization.
3. Keep alert rules disabled until dashboard baselines are reviewed and receivers are approved.
4. Deploy only under separate explicit authorization.
