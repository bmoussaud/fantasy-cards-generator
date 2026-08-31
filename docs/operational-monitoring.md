# Operational monitoring

Issue #42 adds environment-isolated Azure Monitor resources and OpenTelemetry
configuration for the Azure Container Apps deployment. Infrastructure is defined in
`infra/modules/monitoring.bicep` and `infra/modules/operational-monitoring.bicep`.
No Azure resources were deployed as part of this change.

## Defaults and configuration

| Setting | Default | Deployment variable |
|---|---:|---|
| Trace sampling | 100%, parent-consistent | `TELEMETRY_SAMPLING_RATIO` |
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

For local work, telemetry is off by default:

```dotenv
TELEMETRY_ENABLED=false
OTEL_SERVICE_NAME=fantasy-cards-generator
TELEMETRY_SAMPLING_RATIO=1.0
APPLICATIONINSIGHTS_CONNECTION_STRING=
```

Do not put a real connection string in source control. To exercise telemetry locally,
set it only in an ignored `.env` and start the telemetry-first entry point:

```powershell
uv run uvicorn app.entrypoint:app --reload
```

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

The availability test checks `GET /healthz` every five minutes from two configured
Azure test locations. Startup, liveness, and readiness probes use the same
dependency-free endpoint on port 8000. Probe success does not depend on telemetry
export or downstream Azure services.

## Privacy exclusions

Telemetry must never include prompts, generated text or images, request/response
bodies, query-string values, credentials, tokens, cookies, authorization headers,
email/user/tenant/card/blob identifiers, idempotency keys, raw client IPs, or
exception text that may echo those values.

Use normalized route templates, bounded outcome/error codes, and allowlisted
attributes only. Do not add request headers or arbitrary URLs as dimensions.
Application Insights IP masking remains enabled. `X-Request-ID` is diagnostic only:
it must be sanitized, length-bounded, and excluded from metrics.

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

The approved `dev` and `prod` default is 100% parent-consistent trace sampling.
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
3. Verify `/healthz`, all three probes, Application Insights role/environment/revision
   dimensions, W3C correlation, workbook queries, availability results, and Action
   Group routing.
4. Exercise successful, rejected, retried/throttled, timed-out, partial-result, and
   persistence-failure paths without sending prohibited data.
5. Confirm the new revision is healthy before treating the rollout as successful.

If serving health regresses, redeploy/reactivate the recorded known-good image or
revision. Telemetry initialization must fail open when configuration is absent or
invalid, and `/healthz` must remain independent of exporter availability. To stop
notifications without removing resources, set `MONITORING_ALERTS_ENABLED=false` and
reprovision. To disable application export, remove the connection string or set
`TELEMETRY_ENABLED=false`, then redeploy the application.
