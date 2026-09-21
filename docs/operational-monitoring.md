# Operational monitoring

Issue #42 adds environment-isolated Azure Monitor resources and OpenTelemetry
configuration for the Azure Container Apps deployment. Infrastructure is defined in
`infra/modules/monitoring.bicep` and `infra/modules/operational-monitoring.bicep`.
No Azure resources were deployed as part of this change.

For hosted-agent (`card-orchestrator`) operational ownership, alert definitions, and
rollout/rollback procedure, see [docs/agent-operational-ownership.md](agent-operational-ownership.md).
Agent monitoring is deployed by
`infra/modules/agent-monitoring.bicep` through the authoritative root stack and is separate
from the web app monitoring stack. Root Bicep links the shared workspace-based
Application Insights resource to the Foundry account and project; Foundry then
injects its reserved connection setting into hosted versions. The connection string
is not copied through azd state or declared in the hosted manifest.

## Defaults and configuration

| Setting | Default | Deployment variable |
|---|---:|---|
| Trace sampling | 100%, parent-consistent | `TELEMETRY_SAMPLING_RATIO` |
| Application-owned agent detail | On in dev and prod | `FCG_AGENT_TRACE_ENABLED` |
| Workspace retention | 30 days | `MONITORING_RETENTION_DAYS` |
| Workspace daily cap | 0.25 GB/day | `MONITORING_DAILY_QUOTA_GB` |
| Cap warning | 80% | `MONITORING_INGESTION_WARNING_PERCENT` |
| Alert master switch | Off | `MONITORING_ALERTS_ENABLED` |
| Email receivers | `[]` | `MONITORING_EMAIL_RECEIVERS` |
| Webhook receivers | `[]` | `MONITORING_WEBHOOK_RECEIVERS` |

`dev` and `prod` receive independently named workspaces, Application Insights
components, workbooks, availability tests, Action Groups, and alerts. The existing
workspace-based Application Insights component and
`APPLICATIONINSIGHTS_CONNECTION_STRING` secret reference are reused.

The application receives a stable `OTEL_SERVICE_NAME` (and therefore the
`fantasy-cards-generator` Application Insights role), plus
`deployment.environment.name` and `cloud.platform` resource dimensions. Azure
Container Apps supplies `CONTAINER_APP_NAME`, `CONTAINER_APP_REVISION`, and
`CONTAINER_APP_REPLICA_NAME` at runtime; application instrumentation should map the
revision and replica values into spans/logs, not metric dimensions.

For local work, the baseline exporter is off by default; the independent detail
setting defaults ON, but does not create or enable an exporter:

```dotenv
TELEMETRY_ENABLED=false
FCG_AGENT_TRACE_ENABLED=true
OTEL_SERVICE_NAME=fantasy-cards-generator
TELEMETRY_SAMPLING_RATIO=1.0
AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING=
APPLICATIONINSIGHTS_CONNECTION_STRING=
```

Do not put a real connection string in source control. To exercise telemetry locally,
set it only in an ignored `.env`, set
`AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING=true` to opt in to Azure SDK GenAI tracing
while it remains experimental, and start the telemetry-first entry point:

```powershell
uv run uvicorn app.entrypoint:app --reload
```

This opt-in only enables GenAI span emission to the configured telemetry destination;
it does not enable raw prompt, response, header, or body capture.

## Alert routing

The initial rollout is dashboard-only. Alert rules deploy disabled; the Action Group
exists but is disabled because both receiver arrays are empty. Adding an approved
receiver enables the Action Group, while alert rules become active only when
`MONITORING_ALERTS_ENABLED=true` **and** at least one receiver array is non-empty.
No recipient is invented. Configure receiver arrays as JSON before a separately
approved alert activation:

```powershell
azd env set MONITORING_EMAIL_RECEIVERS '[{"name":"operations","emailAddress":"approved@example.com","useCommonAlertSchema":true}]'
azd env set MONITORING_WEBHOOK_RECEIVERS '[]'
```

Webhook `serviceUri` values must not contain credentials or secret query parameters.
Use an approved, authenticated routing service. After deployment, use the Azure
Monitor Action Group test function and confirm receipt before relying on alerts.

## SLO baseline and alerts

These conservative defaults are parameters in Bicep:

| Signal | Window | Default trigger |
|---|---:|---|
| `/healthz` availability | 15 min | 2 failed checks |
| Request failures/5xx | 15 min | 5% with at least 5 requests |
| Request latency | 15 min | p95 at least 10 seconds with at least 5 requests |
| Dependency failure/throttle/timeout | 15 min | 5 events |
| Exceptions | 15 min | 5 events |
| Generation failure/partial/persistence failure | 15 min | 3 outcomes |
| ACA restart/unhealthy/probe failure | 15 min | 3 events |
| Ingestion cap risk | rolling 24 h | 80% of daily cap |

These are initial alert thresholds, not a contractual availability SLO. Revisit them
after at least two weeks of representative traffic. Expected traffic is below 100
requests/day; rules therefore remain disabled initially while dashboards establish a
baseline. Ratio and latency rules retain a configurable traffic floor to avoid noisy
low-volume alerts.

The availability test checks dependency-aware `GET /healthz` every five minutes
from two configured Azure test locations. ACA startup and liveness probes use
dependency-free `/livez` on port 8000; readiness uses `/healthz`. Telemetry export
is not part of either probe response, while readiness fails closed on required
Azure dependencies.

## Privacy exclusions and approved #159 r1 exception

Baseline spans, metrics, logs, SDK payload instrumentation and arbitrary
request/response bodies remain content-free. #159 r1 authorizes only a narrow
application-owned, closed-schema diagnostic exception for bounded, sanitized
stage input, effective application instruction, validated output and closed
execution result. It is default ON in **dev and prod**, not a development-only
debug policy. It does not authorize blanket SDK logging, a trace UI, application
endpoints, browser/API fields, or card/audit persistence.

Never intentionally capture credentials, tokens, cookies, authorization data,
email/user/tenant/card/blob/session or business identifiers, idempotency keys,
raw client IPs, binary/base64/images, image or signed URLs, hidden reasoning, or
arbitrary exception text. Reject unknown fields before capture and withhold
uninspectable values. Conservative sanitization is not a universal PII guarantee:
undetected personal data in otherwise permitted prose remains a rollout risk.

Use normalized route templates, bounded outcome/error codes, and allowlisted
attributes only. Do not add request headers or arbitrary URLs as dimensions.
Application Insights IP masking remains enabled. `X-Request-ID` is diagnostic only:
it must be sanitized, length-bounded, and excluded from metrics.

### Startup setting and process-local OFF

The application setting is `FCG_AGENT_TRACE_ENABLED`. Foundry reserves all
`AGENT_*` and `FOUNDRY_*` names for platform use, so the previous custom name
`AGENT_TRACE_ENABLED` cannot be supplied to the hosted container and is no longer
read by either runtime or the deployment guard. Existing azd values under that
old name do not control this feature. Before redeploying, copy any explicit
choice to `FCG_AGENT_TRACE_ENABLED` in the selected azd environment, especially
an earlier `false` opt-out; leaving the new setting unset defaults to ON.
Redeploy both runtimes through the root workflow so their configuration agrees.

Both runtimes use the same parser: unset means `true`; trimmed, case-insensitive
`true`/`false` are accepted; invalid values, **including empty**, reject startup
with a content-free configuration error. This is read at process startup, never
enabled by a request or dynamically reloaded.

| WEB | HOSTED | New application-owned detail behavior |
|---|---|---|
| true / unset | true / unset | Both capture within bounds; only WEB may release content after terminal full success |
| false | true | HOSTED may capture/transport candidates and export content-free execution spans; WEB performs no capture-only processing or content export |
| true | false | WEB-only eligible detail; HOSTED performs no new detail spans/capture-only processing |
| false | false | No new detail spans/events, capture-only sanitization, serialization or buffers in either process |

OFF preserves baseline generation/dependency spans and metrics, safe errors,
functional validation and moderation, `TELEMETRY_ENABLED`, and the mandatory
HOSTED `configure_telemetry()` startup gate. It neither bypasses monitoring
initialization failure nor controls SDK/platform `invoke_agent` spans. The existing
`AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING` opt-in semantics are unchanged.
Turning WEB OFF does not remotely turn HOSTED OFF. Turning both OFF does not delete
already-exported telemetry.

Root `azure.yaml` passes `${FCG_AGENT_TRACE_ENABLED=true}` to HOSTED. The same azd
setting passes through `infra/main.parameters.json`, `infra/main.bicep` and
`infra/modules/container-apps.bicep` to WEB. After azd substitution, Bicep passes
the resolved string unchanged for strict runtime validation.
The Bicep parameter is a string intentionally, not an ARM boolean conversion.
Because azd's default-expression substitution treats empty as unset, root
preprovision, prepackage, prepublish and predeploy hooks validate the **raw**
azd-injected `FCG_AGENT_TRACE_ENABLED` using `hooks/validate_agent_trace.py`.
The raw hook environment preserves explicit empty values: unset is allowed to
default ON, trimmed case-insensitive true/false pass unchanged, and empty,
whitespace-only or invalid settings stop the command with a content-free error
before deployment can consume the substituted default. The guard reads only that
key, never environment files or bulk azd output. It covers both targeted services,
`azd up` and the root `deploy.sh` wrapper; bypassing hooks or using the deprecated
nested manifest is unsupported. Use explicit `false`, not empty, to opt out.
There is no production-specific override to OFF. Changing azd state alone has no
effect on running processes: apply a new hosted version and the respective WEB
configuration revision/restart through the approved root rollout. A web image-only
deploy does not apply changed Bicep environment parameters. See
[the operational runbook](agent-operational-ownership.md#detail-configuration-rollout).

### Release ownership, source links and compatibility

HOSTED execution spans `card_concept`, `card_lore`, `card_art_direction` remain
content-free. After hosted validation/moderation succeeds, immutable sanitized
candidates may travel privately in optional, independently versioned response
metadata. WEB is the sole release authority: every candidate must independently
pass privacy and applicable safety checks, and the entire business operation must
reach terminal **full success**, after all text/art/image moderation, validation,
persistence and business-response validation/serialization gates.

Any partial, refused, held, routing-deferred, unknown-safety, unvalidated, failed,
timed-out or cancelled outcome discards all candidate content, including earlier
successful stages. A later successful retry never releases a failed/rejected
attempt. Safe partial-card/artwork-retry behavior is unchanged. Replays and
single-flight followers do not fabricate executions or duplicate content release.

Eligible records are **WEB-exported detail linked to source execution spans**,
labelled `source_runtime=hosted` or `web` with measured source timing. They are
not retroactively attached to finished original HOSTED spans. WEB owns its hosted
invocation and image attempt/retry spans. Use only validated instrumentation-owned
trace/span correlation handles; no baggage or user/resource identifiers. Local
W3C injection/extraction tests cannot establish actual Foundry gateway forwarding
or a continuous platform trace tree.

Missing N-1 metadata is supported. Unknown/malformed versions, source links,
overflow or diagnostic processing/export failures suppress detail with bounded
content-free reasons while valid business responses remain usable. Business
`schemaVersion=1` and application-version matching stay unchanged; diagnostic,
redaction, trusted instruction/schema and hosted source versions are separate.
Malformed business responses still fail normally.

**Transport limitation:** candidates cross the Foundry response transport before
WEB's terminal decision. The provider may process or retain response metadata
despite `store:false` / `NoResponseStore`. Application export suppression is not
a guarantee that later-refused content exists nowhere. Supported non-persisting
transport, SDK payload suppression and actual platform correlation remain
unverified delivery gates requiring separately authorized validation; failure
returns to intake rather than authorizing a store or blanket SDK logging.

### Content and capacity bounds

| Bound, including retries and serialized context/status/envelope overhead | Ceiling |
|---|---|
| Text field | 2 KiB UTF-8 |
| Serialized record | 8 KiB |
| Serialized detail per generation | 48 KiB: fixed 24 KiB HOSTED-source + 24 KiB WEB-source, no borrowing |
| Content records | 8: at most 3 HOSTED-source + 5 WEB-source |
| New execution/detail spans, including release spans | 32: fixed 16 HOSTED + 16 WEB |
| Active request-scoped buffers per process | 16, no waiting, disk spill or cross-request reuse |

WEB candidates have at most 48 KiB serialized-equivalent data per active request;
HOSTED has at most 24 KiB. Bound traversal and transient memory too, not an
unbounded stringify followed by truncation. Budget exhaustion omits further
detail, never generation work or baseline signals; measure peak memory and
ingestion under concurrency and retries before rollout.

Sanitize before deterministic UTF-8 truncation of instruction text. Oversized
structured input/output projections omit the entire payload, never partial JSON.
Preserve metadata/status over content, using stable field priority: instruction,
input, output. Explicit
`redacted`, `truncated`, `suppressed_policy`, `unvalidated`, and `omitted_budget`
indicators may coexist; suppressed records never echo rejected values.
Redacted or omitted input/output projections carry `validation='modified'`, not
`validation='validated'`. Modified data is not an exact reproduction.

Input represents the actual stage query/card snapshot, not a reconstruction from
the final card. Instruction means trusted application shared rules + stage task +
schema, not unknown SDK/model instructions. Output distinguishes validated
specialist refinement from merged card. Image detail contains only safe textual
art instructions, closed mode/quality and outcome metadata, never bytes or URLs.
Execution result separates model-call outcome from validation/moderation acceptance.

Synthetic operator example: a successful `card_lore` candidate for "a silver
woodland guardian" can carry a sanitized input, bounded application instruction,
validated lore refinement and a closed result. If its instruction is shortened,
`truncated` must be visible. If subsequent image moderation refuses the request,
**none** of that candidate content is exported; only safe structural outcomes
remain. Do not paste real requests or trace identifiers into runbooks or tickets.

### Rollout access, retention and deletion gates

Before enabling traffic on a new detail-capable version, record approved readers
including inherited RBAC, environment boundaries, downstream exports, retention,
and deletion procedures with the privacy/operations owners. Existing 30-day
retention, 0.25 GB/day cap and 100% sampling are observed baseline defaults, **not
approval to retain content**. Card/account deletion does not automatically delete
operational telemetry or downstream copies; establish those procedures explicitly.
Assess retry-peak ingestion headroom so detail does not starve mandatory baseline
signals. The shared daily cap is not an instantaneous hard stop.

No new resource, access grant, retention or sampling change is authorized by #159.
Missing platform evidence or unapproved access/retention/headroom blocks rollout;
it is not permission to deploy. This implementation performs no live validation.

## Workspace tables and KQL

This deployment configures ACA with `destination: 'log-analytics'`; therefore its
supported tables are `ContainerAppConsoleLogs_CL` and
`ContainerAppSystemLogs_CL`, with `_s` string columns. Workspace-based Application
Insights uses `AppRequests`, `AppDependencies`, `AppExceptions`, `AppTraces`,
`AppMetrics`, and `AppAvailabilityResults`.

Request latency and failure ratio:

```kusto
AppRequests
| where AppRoleName == "fantasy-cards-generator"
| summarize Requests=count(),
    Failures=countif(Success == false),
    p50=percentile(DurationMs, 50),
    p95=percentile(DurationMs, 95),
    p99=percentile(DurationMs, 99)
  by bin(TimeGenerated, 1h)
```

Dependency health:

```kusto
AppDependencies
| where AppRoleName == "fantasy-cards-generator"
| summarize Calls=count(),
    Failures=countif(Success == false),
    Throttles=countif(ResultCode == "429"),
    p95=percentile(DurationMs, 95)
  by DependencyType, bin(TimeGenerated, 1h)
```

ACA restarts and unhealthy revisions:

```kusto
ContainerAppSystemLogs_CL
| where ContainerAppName_s == "fcg-prod-app-nat"
| where Reason_s in ("Restarting", "Unhealthy", "HealthProbeFailed")
    or Log_s has_any ("restart", "unhealthy", "probe failed")
| project TimeGenerated, RevisionName_s, Reason_s, Log_s
| order by TimeGenerated desc
```

Billable ingestion:

```kusto
Usage
| where IsBillable == true
| summarize BillableGB=sum(Quantity) / 1000.0 by bin(TimeGenerated, 1d)
| extend DailyCapGB=0.25, CapUtilizationPercent=100.0 * BillableGB / DailyCapGB
```

Each environment has an isolated workspace, so workbook and alert KQL does not
filter on `deployment.environment.name`; Azure Monitor does not copy that resource
attribute into every span's `Properties`. The deployed workbook includes service
and revision controls plus request, dependency, exception, generation, ACA, and
ingestion views. Exception events retain only a normalized exception type; message,
stack trace, and other event attributes are removed before export. The generation
view splits its series by bounded outcome, moderation, retry, persistence, and
token dimensions.

## Sampling and cost

The baseline `dev` and `prod` default is 100% parent-consistent trace sampling;
this is not approval of diagnostic-content retention or ingestion headroom.
Metrics remain unsampled so rare operational outcomes can still be counted. Review
ingestion after deployment and lower sampling only through an approved configuration
change.

At deployment time, retrieve the current Analytics Logs ingestion rate in **EUR** for
the actual Azure region and billing offer from the
[Azure Retail Prices API](https://prices.azure.com/api/retail/prices) or Azure Pricing
Calculator. Do not copy a historical or assumed unit price into configuration. At the
0.25 GB/day cap, the maximum nominal monthly volume for a 30-day month is 7.5 GB per
environment. Estimate each isolated environment with:

```text
min(expected billable GB/day, 0.25) × current regional EUR/GB rate × billing days
```

Subtract only allowances confirmed for the deployed billing offer, then add any
retention charges beyond the included retention period. Record the source URL, region,
currency, meter, and retrieval date with the deployment evidence. Review `Usage`
after deployment; the workspace daily cap is a cost guardrail, not an instantaneous
cutoff, and ingestion can slightly exceed it before enforcement.

## Deployment verification and rollback

No deployment was performed for this implementation. During an authorized rollout:

1. Record the active ACA revision and image.
2. Provision the monitoring resources and deploy the telemetry-first image.
3. Verify dependency readiness on `/healthz`, process liveness on `/livez`, all
   three platform probes, Application Insights role/environment/revision
   dimensions, W3C correlation, workbook queries, availability results, and Action
   Group routing.
4. Exercise successful, rejected, retried/throttled, timed-out, partial-result, and
   persistence-failure paths without sending prohibited data.
5. Confirm the new revision is healthy before treating the rollout as successful.

If serving health regresses, redeploy/reactivate the recorded known-good image or
revision. Baseline WEB exporter initialization retains its existing fail-open
behavior; invalid `FCG_AGENT_TRACE_ENABLED` instead rejects startup in both runtimes,
and HOSTED monitoring initialization remains mandatory. `/healthz` must remain
independent of exporter availability. To stop
notifications without removing resources, set `MONITORING_ALERTS_ENABLED=false` and
reprovision. To disable application export, remove the connection string or set
`TELEMETRY_ENABLED=false`, then redeploy the WEB application. Do not disable mandatory
HOSTED monitoring as a detail rollback: use `FCG_AGENT_TRACE_ENABLED=false` in both
processes and apply their respective configuration revision/version instead.
