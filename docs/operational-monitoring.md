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

## Privacy exclusions and approved #162 r1 contract

Baseline spans, metrics, logs, SDK payload instrumentation and arbitrary
request/response bodies remain content-free. #159 r1 authorizes only a narrow
application-owned, closed-schema diagnostic exception for bounded, sanitized
stage input, effective application instruction, validated output and closed
execution result. Approved #162 r1 changes the release authority and attribute
location for the three original HOSTED specialist spans only. The contract below
applies to new #162-capable versions, not retroactively to #159/#160 deployments.
[Requester approval](https://github.com/bmoussaud/fantasy-cards-generator/issues/162#issuecomment-5761213933)
authorizes implementation, not deployment or merge; live portal acceptance remains
independently pending. The setting remains default ON in **dev and prod**, not a
development-only debug policy. It does not authorize blanket SDK logging, a trace UI, application
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

| WEB | HOSTED | New #162 application-owned detail behavior |
|---|---|---|
| true / unset | true / unset | HOSTED releases eligible specialist content at HOSTED acceptance; WEB releases its own detail only after terminal full WEB success |
| false | true | HOSTED can export eligible content, including standalone calls; WEB performs no capture-only processing or content export |
| true | false | WEB-only eligible detail; HOSTED performs no new detail spans/capture-only processing |
| false | false | No new detail spans/events, capture-only sanitization, serialization or buffers in either process |

For old HOSTED versions, ON still means private candidate transport, not original-span
content; the legacy WEB gate below applies. There is no extra flag or custom
`AGENT_*` / `FOUNDRY_*` setting for #162.

OFF preserves baseline generation/dependency spans and metrics, safe errors,
functional validation and moderation, `TELEMETRY_ENABLED`, and the mandatory
HOSTED `configure_telemetry()` startup gate. It neither bypasses monitoring
initialization failure nor controls SDK/platform `invoke_agent` spans. The existing
`AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING` opt-in semantics are unchanged.
Turning WEB OFF does not remotely turn HOSTED OFF. Turning both OFF does not delete
already-exported telemetry. A configuration edit does not stop capture by an
in-flight request or an older still-running version; verify the effective settings
of every version/replica still serving traffic before declaring capture disabled.

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

New HOSTED attaches a canonical, strictly validated JSON string, `fcg.detail.record`,
directly to each eligible original `card_concept`, `card_lore`, and
`card_art_direction` span in Application Insights attributes/custom properties.
This does **not** promise built-in Foundry input/output panel rendering. No replacement
or parallel detail spans stand in for these originals.

**HOSTED is the release authority for these three records.** Release requires
`completed`, successful specialist/resource closure, allowed evidence for
`pre_prompt`, `concept`, `lore`, `final_text`, and `final_art_prompt`, independent
validation/privacy/moderation acceptance of every candidate, and
serialization/revalidation of the actual business response. The complete eligible batch is
preflighted before attaching any content or ending any content-bearing span.
Before this boundary, a HOSTED refusal, held/routing-deferred outcome, missing or
invalid evidence/candidate, failure, timeout or cancellation suppresses all pending
content, including earlier successful stages. Budget omissions may retain explicitly
marked eligible views, never unchecked content.

**Changed privacy guarantee:** after HOSTED acceptance, later WEB text/art/image
refusal, partial success, persistence failure, disconnect or response-delivery
failure cannot retract HOSTED content. Release also precedes HOSTED HTTP/platform
delivery: a later HOSTED send failure cannot retract it. HOSTED ON / WEB OFF and
standalone HOSTED calls can therefore export content. Business moderation/refusal,
safe partial-card behavior and artwork retry are unchanged; a successful retry does
not release an earlier rejected attempt.

### Original timing and later moderation

At most three pending source span handles are retained request-locally. Each
original starts with its actual parent and is active only during its stage. At
stage exit it is detached, with actual end timestamp, monotonic duration and
stage-local result/validation/moderation frozen. After HOSTED acceptance it receives
the record and ends exactly once with the saved stage completion timestamp.
IDs, parentage, start/end and execution duration are preserved; delayed export
does not add later stages or acceptance waiting to the duration. Later stages are
not children of a retained earlier stage. No past trace backfill is possible:
records are not retroactively attached to already-ended spans.

Structural `fcg.detail.moderation` describes stage-time truth. In particular,
`card_art_direction` may remain `unvalidated` at its actual end while the eventual
record has `moderation=allowed`, representing the later HOSTED release decision.
That is not backdated moderation. Current HOSTED evidence reports
`hosted_guardrails=unavailable` and `post_image=not_applicable`; neither is a claim
that those checks ran.

Pre-release denial/error finalizes retained spans content-free, restores context
and clears references/capacity. OFF or nonrecording/unsampled execution skips
capture-only work and retention; sampled-out spans are not resurrected. Process
death can lose pending spans; there is no durable recovery. Exporter delivery is
best-effort and non-atomic: some of the three spans may arrive before an export
failure, queue loss or shutdown. Missing content is not proof of refusal, and
instrumentation/export failure must not fail otherwise valid business generation.

Admission is lazy: the actual specialist span must be recording and sampled before
a capture buffer or capacity permit is acquired. The sampler still decides each
stage normally, even when its parent is nonrecording; parent flags alone do not
predict custom sampler decisions. Ineligible spans end at stage exit without
candidate snapshots, instruction capture or retained handles. If capacity is full,
the first eligible span ends content-free without retaining a buffer, and further
detail is skipped for that operation. Stage setup, structural attribute assignment
and detachment all share unconditional cleanup; cancellation propagates only after
context restoration and registration or ending of the owned span.

Only the dedicated application instrumentation scope and exact original HOSTED
names may export these records after closed-schema revalidation and binding to
the exporting span's actual source IDs, stage, agent, operation, attempt,
deployment/instruction versions and duration. SDK/platform `invoke_agent` spans,
events and payload-bearing logs remain excluded from this content exception;
their baseline monitoring rules are unchanged. Use only validated
instrumentation-owned correlation handles, never baggage or business identifiers.
Preflight compares **all live retained spans**, including their scope, name,
context/parent, start time and structural attributes, against frozen snapshots
before any record is attached. Original-span export additionally requires the
exact owned span object, the exact preflighted record and its candidate-approval
hash, and the saved end timestamp. Matching names or self-declared source IDs
alone cannot authorize content. This authorization exists only during synchronous
span ending and is cleared even on failure; no persistent span/content registry
or repeated exporter-side moderation is used.
Local W3C tests cannot prove Foundry gateway forwarding or a continuous trace tree.

### Workflow task contexts and #164 acceptance

The approved [#164 graph](architecture-agents-foundry.md#approved-workflow-engine-contract-164-r1)
does not replace the three original specialist spans with Workflow, executor or
provider instrumentation. Preserve the exact owned span objects, trace/span IDs,
parentage and stage start/end timestamps, including parsing, whole-card validation
and applicable moderation. `StageBoundary` supplies the captured request parent
context explicitly; span activation/cleanup stay in the invoking executor task,
without an attach/detach token shared across sibling tasks.

HOSTED still delays the whole eligible batch until resource/task closure,
business-response serialization/revalidation and complete candidate/original
preflight. An unsafe intermediate, invalid final candidate, closure/serialization
failure, cancellation or expired deadline releases zero pending HOSTED content.
Mandatory terminal serialization/adapter validation failures remain business
failures; a diagnostic-only serialization/preflight fault suppresses detail
without replacing an otherwise safe business result.
The original absolute operation deadline also covers Workflow scheduling;
the synchronous blocking-work overrun caveat below remains unchanged.

All existing default-ON/OFF, sampled/nonrecording, lazy-admission, 16-buffer,
three-original and byte/span bounds remain binding, as do legacy WEB terminal
gating and late WEB-failure semantics. Inspect actual framework span
names/attributes/events/links/statuses, logs and workflow/wire outputs using
synthetic privacy canaries; enabling Workflow is not permission for blanket
payload capture or broad telemetry suppression. The privacy processor normalizes
recognized MAF instrumentation span names to `agent_framework.operation` and OTel
log bodies to `agent_framework.diagnostic`, retaining existing payload
sanitization rather than suppressing unrelated application telemetry.
These are offline acceptance
requirements for the new graph. Historical #162 dev-v9 telemetry and offline
exporter conversion do not prove its deployment, forwarding or portal acceptance.

The [requester waiver](https://github.com/bmoussaud/fantasy-cards-generator/issues/164#issuecomment-5778665499)
removes #164 performance acceptance, not the original-span identity/timing,
single-deadline lifecycle or privacy/capture requirements. The offline first-ready
wave of 11.71 seconds / 9.54 MiB exceeded the former 5 seconds / 8 MiB study
thresholds; it is a nonproduction, nonblocking observation, not production
telemetry or a passing benchmark. No timeout, buffer, byte, sampling or admission
limit is changed by the waiver.

### Mixed-version compatibility and legacy detail

Here, **new** means #162-capable and **old** means the #159/#160 carrier contract
(with #161 naming where present), not a claim about which artifact is deployed.

| Producer / consumer | Required behavior |
|---|---|
| New HOSTED / new WEB | Eligible content on originals; no `metadata.agentDetail` carrier; no duplicate WEB specialist records |
| New HOSTED / old WEB | Missing optional carrier is valid; business response and WEB-owned details still work; old WEB cannot retract HOSTED exports |
| Old HOSTED / new WEB | Bounded legacy parser and independent candidate checks remain; linked detail uses the existing WEB-terminal full-success gate; no synthesized original-span content |
| Old HOSTED / old WEB | Existing behavior unchanged: content-free HOSTED originals, private candidates and WEB-gated linked detail |

The complete version/flag matrix below describes **eligible application-owned
content**, not guaranteed delivery. `Originals` means HOSTED-accepted records on
the three originals; `WEB` means WEB-owned invocation/image detail; `Legacy`
means WEB-linked specialist records from the old private carrier. Both `WEB` and
`Legacy` require terminal full WEB success. `Carrier only` means private transport
still occurs but neither runtime releases specialist diagnostic content to telemetry.

| HOSTED / WEB versions | Both ON | HOSTED ON / WEB OFF | HOSTED OFF / WEB ON | Both OFF |
|---|---|---|---|---|
| New / new | Originals + WEB; no carrier or specialist copies | Originals | WEB | None |
| New / old | Originals + WEB; optional carrier absent | Originals | WEB | None |
| Old / new | Legacy + WEB | Carrier only | WEB | None |
| Old / old | Legacy + WEB | Carrier only | WEB | None |

ON/OFF means the **effective process setting**, not merely azd state. A pre-#161
artifact may not recognize `FCG_AGENT_TRACE_ENABLED`; verify its supported
configuration before relying on an OFF or rollback claim. This does not authorize
reintroducing reserved custom environment names. Standalone new HOSTED ON can
release originals without any WEB consumer; standalone old HOSTED ON only carries
private candidates, with no WEB release gate available. Carrier absence is also
possible with OFF or omitted diagnostics, so it is not proof of a producer version
or successful original-span export.

WEB-owned invocation/image attempt and retry detail still releases only after
terminal **full WEB success**, including text/art/image moderation, validation,
persistence, response serialization and successful final HTTP send. The same gate
applies to old HOSTED carriers: any partial/refused/held/deferred/unknown-safety,
failed/timed-out/cancelled or delivery-failed WEB outcome suppresses their linked
content. Replays/single-flight followers do not fabricate executions or duplicate
release. Earlier linked `fcg.agent.detail` queries remain relevant to historical
records, legacy carriers and WEB's own detail, not new HOSTED original attributes.

Carrier absence selects the no-legacy-import path; no capability negotiation or
migration framework is added. Missing N-1 metadata is supported. Unknown/malformed
legacy carriers, versions or source links fail closed for diagnostics with bounded
content-free reasons, without invalidating otherwise valid business responses.
Business `schemaVersion=1`, application-version matching and browser/API shapes
stay unchanged. Diagnostic/redaction/instruction/source versions remain separate.
Malformed business responses still fail normally.

**Provider limitation:** new HOSTED omits the private carrier, but this is not a
provider/system-metadata non-retention guarantee. Old candidates still cross
Foundry transport before WEB's decision. `store:false` / `NoResponseStore` do not
prove provider non-retention or eliminate provider-controlled system metadata.
Suppression does not guarantee content exists nowhere. SDK suppression, actual
platform correlation and non-persistence evidence remain separately authorized
delivery gates; failure returns to intake, not a new store or SDK payload unmute.

### Content and capacity bounds

| Bound, including retries and serialized context/status/envelope overhead | Ceiling |
|---|---|
| Text field | 2 KiB UTF-8 |
| Serialized record | 8 KiB |
| Serialized detail per generation | 48 KiB: fixed 24 KiB HOSTED-source + 24 KiB WEB-source, no borrowing |
| Content records | 8: at most 3 HOSTED-source + 5 WEB-source |
| New execution/detail spans, including release spans | 32: fixed 16 HOSTED + 16 WEB |
| Pending original specialist span handles | At most 3 HOSTED, within the existing 16-span ceiling |
| Active request-scoped buffers per process | 16, no waiting, disk spill or cross-request reuse |

New HOSTED has at most 24 KiB and three records; new WEB retains its separate
24 KiB/five-record budget. With an old HOSTED carrier, WEB candidates may have at
most 48 KiB serialized-equivalent data per active request across the fixed buckets.
Bound traversal, transient memory and lifetime against the operation deadline too,
not an unbounded stringify followed by truncation. Budget exhaustion omits further
detail, never generation work or baseline signals; measure peak memory and
ingestion under concurrency and retries before rollout.

HOSTED uses one absolute operation deadline through resource closure, response
serialization/revalidation, complete-batch preflight and original-span finalization.
Synchronous acceptance steps check the same clock on return and before release;
there is no restarted acceptance budget. Expiry returns the existing business
timeout failure and drains remaining originals without content. Python cannot
preempt a blocking synchronous serializer or exporter: measured elapsed time can
exceed the deadline by that call and mandatory cleanup, but expired work cannot
authorize a subsequent content export. A record already exported before expiry
cannot be retracted; finalization remains best-effort, not an atomic transaction.

Sanitize before deterministic UTF-8 truncation of instruction text. Oversized
structured input/output projections omit the entire payload, never partial JSON.
Preserve metadata/status over content, using stable field priority: instruction,
input, output. Explicit
`redacted`, `truncated`, `suppressed_policy`, `unvalidated`, and `omitted_budget`
indicators may coexist; suppressed records never echo rejected values.
Redacted or omitted input/output projections carry `validation='modified'`, not
`validation='validated'`. Modified data is not an exact reproduction.

### Exact record fields and modification meaning

`fcg.detail.record` is JSON encoded as one string attribute. Its `input.text` and
`output.text` are themselves canonical JSON strings of closed typed projections
(or empty with explicit omission flags), not raw provider envelopes:

| Original span | `input.text` before that specialist call | `output.text` after typed validation | `output_kind` |
|---|---|---|---|
| `card_concept` | Object containing only the actual `query` | The typed concept card | `card` |
| `card_lore` | The preceding validated concept card snapshot | Only `name` and `flavorText` from the lore refinement | `refinement` |
| `card_art_direction` | The preceding card snapshot with lore already applied | Only `artBrief` from the art refinement | `refinement` |

The closed card projection contains `schemaVersion`, `name`, `cardType`, `rarity`,
`manaCost`, `attack`, `health`, `rulesText`, `flavorText` and `artBrief`. The snapshot
is taken before the call, not reconstructed from the final card. For lore/art it
projects the card inside the actual `{"card": ...}` application payload; it does
not claim to reproduce transport wrapping. Outputs represent typed results, not
the original response bytes or a substituted merged final card.

`instruction.text` is the bounded safe view of the effective application
instructions actually supplied: shared rules + stage task + that stage's schema.
`instruction_version=application-v1` and `instruction_digest` bind it to the
trusted full instruction; the digest is not a promise that the visible shortened
view is complete. Hidden provider instructions and reasoning are excluded.
Image detail contains only safe textual
art instructions, closed mode/quality and outcome metadata, never bytes or URLs.
Execution result separates model-call outcome from validation/moderation acceptance.
The strict record exposes `input.text` / `input.flags`, `instruction.text` /
`instruction.flags`, `output.text` / `output.flags` and aggregate `flags`.
`source`, `source_runtime`, `stage`, `agent_name`, `operation`, `attempt`,
`deployment_version`, `instruction_version`/`instruction_digest`, `duration_ms`,
`result`, `reason`, `validation`, `moderation` and `output_kind` preserve the
trusted identity, measured timing and closed acceptance meaning.

Record `result=completed`, `reason=allowed` and `moderation=allowed` describe an
accepted diagnostic candidate, not final WEB success. The source-stage structural
result/reason remain separate. Capture/redaction versions are currently integer
`1`, independently of business `schemaVersion=1`; unknown fields, malformed types
and incompatible versions cannot authorize content.

Per-view `flags` explain each transformation; record `flags` is their aggregate.
`redacted` identifies removed values, `truncated` a shortened view, and
`omitted_budget` an empty budget-omitted view. `suppressed_policy` and `unvalidated`
indicate suppression, not permission to include rejected or unchecked text.
Instruction-only truncation can coexist with `validation=validated`: that status
describes typed input/output projections, not completeness of the instruction.
Any modified input/output projection uses `validation=modified`. Inspect all
three view flags even when validation is `validated`; none of these labels
guarantees universal safety or PII detection.

Synthetic operator example: a successful `card_lore` candidate for "a silver
woodland guardian" can carry a sanitized input, bounded application instruction,
validated lore refinement and a closed result. If its instruction is shortened,
`truncated` must be visible. A later HOSTED refusal before acceptance suppresses
all three records. After new HOSTED acceptance, a subsequent WEB image refusal
cannot retract the lore content; WEB-owned details remain suppressed. For an old
HOSTED carrier, that image refusal still suppresses linked specialist content.
Do not paste real requests or trace identifiers into runbooks or tickets.

### Rollout access, retention and deletion gates

Before enabling traffic on a new detail-capable version, record approved readers
including inherited RBAC, environment boundaries, downstream exports, retention,
and deletion procedures with the privacy/operations owners. Existing 30-day
retention, 0.25 GB/day cap and 100% sampling are observed baseline defaults, **not
approval to retain content**. Card/account deletion does not automatically delete
operational telemetry or downstream copies; establish those procedures explicitly.
Assess retry-peak ingestion headroom so detail does not starve mandatory baseline
signals. The shared daily cap is not an instantaneous hard stop.

No new resource, access grant, retention or sampling change is authorized by #159
or #162. Flag rollback is prospective only, not deletion of already-exported data.
Missing platform evidence or unapproved access/retention/headroom blocks rollout;
it is not permission to deploy. Separately authorized synthetic live acceptance
must verify original properties, completeness, IDs/relationships, timing,
role/version attribution and portal discoverability. Offline production-processor/
Azure-exporter conversion is necessary evidence, not proof of live ingestion or
portal rendering. This documentation update performs no live validation.

Offline delivery evidence must cover the production `PrivacySpanProcessor` and
Azure exporter conversion, including its pinned `_on_ending` SDK-hook dependency,
processor order/batching and parent-based sampling. Require exact identity/timing
checks; pre-release refusal/serialization/cancellation and post-release transport
failures; all version/flag combinations; byte thresholds; the seventeenth buffer
admission; and cleanup under individual instrumentation/end/export failures.
Record measured peak transient/retained memory and lifetime against the existing
operation deadline, not an inference from serialized byte ceilings. The required
contract includes skipping capture-only allocation/retention when nonrecording.
These are acceptance requirements, not test results asserted by this runbook.

## Workspace tables and KQL

This deployment configures ACA with `destination: 'log-analytics'`; therefore its
supported tables are `ContainerAppConsoleLogs_CL` and
`ContainerAppSystemLogs_CL`, with `_s` string columns. Workspace-based Application
Insights uses `AppRequests`, `AppDependencies`, `AppExceptions`, `AppTraces`,
`AppMetrics`, and `AppAvailabilityResults`.

### Original HOSTED content (#162)

For a separately authorized synthetic run, use the selected environment's workspace
and a narrow time window. This query reads the original spans directly, not a
linked detail copy. It needs no prompt or user-provided trace ID:

```kusto
AppDependencies
| where TimeGenerated > ago(1h)
| where AppRoleName == "card-orchestrator"
| where Name in ("card_concept", "card_lore", "card_art_direction")
| where isnotempty(tostring(Properties["fcg.detail.record"]))
| extend Detail = parse_json(tostring(Properties["fcg.detail.record"]))
| project TimeGenerated, Name, OperationId, Id, ParentId, DurationMs,
    SourceTraceId=tostring(Detail.source.trace_id),
    SourceSpanId=tostring(Detail.source.span_id),
    SourceDurationMs=toint(Detail.duration_ms),
    Runtime=tostring(Detail.source_runtime), Stage=tostring(Detail.stage),
    Version=tostring(Detail.deployment_version),
    Input=tostring(Detail.input.text), InputFlags=Detail.input.flags,
    Instruction=tostring(Detail.instruction.text), InstructionFlags=Detail.instruction.flags,
    Output=tostring(Detail.output.text), OutputFlags=Detail.output.flags,
    OutputKind=tostring(Detail.output_kind), Flags=Detail.flags,
    Result=tostring(Detail.result), Reason=tostring(Detail.reason),
    Validation=tostring(Detail.validation), ReleaseModeration=tostring(Detail.moderation),
    StageModeration=tostring(Properties["fcg.detail.moderation"])
| order by TimeGenerated asc
```

In **Application Insights application-scoped Logs**, use `dependencies`, not
workspace `AppDependencies`; corresponding columns are `timestamp`, `cloud_RoleName`,
`name`, `customDimensions`, `operation_Id`, `id`, `operation_ParentId` and `duration`.
For example:

```kusto
dependencies
| where timestamp > ago(1h)
| where cloud_RoleName == "card-orchestrator"
| where name in ("card_concept", "card_lore", "card_art_direction")
| where isnotempty(tostring(customDimensions["fcg.detail.record"]))
| extend Detail = parse_json(tostring(customDimensions["fcg.detail.record"]))
| project timestamp, name, operation_Id, id, operation_ParentId, duration,
    Input=tostring(Detail.input.text), InputFlags=Detail.input.flags,
    Instruction=tostring(Detail.instruction.text), InstructionFlags=Detail.instruction.flags,
    Output=tostring(Detail.output.text), OutputFlags=Detail.output.flags,
    Flags=Detail.flags, Validation=tostring(Detail.validation),
    ReleaseModeration=tostring(Detail.moderation),
    StageModeration=tostring(customDimensions["fcg.detail.moderation"])
| order by timestamp asc
```

Both queries return content: restrict execution/results to approved readers and
synthetic acceptance traffic. Do not paste results containing real content or IDs
into tickets. Provider-formatted dependency IDs need not be bare OTel hex strings;
verify their mapping in authorized acceptance rather than assuming string equality.
Absent rows can reflect an old version, OFF, sampling, suppression, budget/capacity
omission, delayed export or telemetry loss, not necessarily failed generation.
Check content-free `agent.detail.omitted` diagnostics / `fcg.detail.omission`;
never enable raw SDK payload logs to investigate.

For routine triage, start without content or correlation IDs:

```kusto
AppDependencies
| where TimeGenerated > ago(1h)
| where AppRoleName == "card-orchestrator"
| where Name in ("card_concept", "card_lore", "card_art_direction")
| summarize Spans=count(),
    WithRecord=countif(isnotempty(tostring(Properties["fcg.detail.record"]))),
    p95DurationMs=percentile(DurationMs, 95)
    by Name, bin(TimeGenerated, 5m)
```

Check the approved artifact/version and effective flags first, then parent sampling,
bounded omission reasons, ingestion headroom and exporter/queue health. Reasons
such as `capture_capacity`, `omitted_budget`, `unvalidated_instruction`,
`suppressed_policy`, `response_unvalidated`, `invalid_export`,
`instrumentation_failure` and `export_failure` classify diagnostic loss; they are
not raw exceptions or proof of a business failure. Missing rows cannot distinguish
pending export from process/queue loss. Keep incident evidence to bounded counts,
statuses and approved version/role attribution; inspect real content only under
explicit reader authorization, and never copy it into tickets to troubleshoot.

### Legacy linked records and WEB-owned detail

The earlier linked-record query remains useful for old records and current WEB
invocation/image detail. Its source IDs identify the execution, not the linked
detail span's own ID. It will not locate new HOSTED content on original spans:

```kusto
AppDependencies
| where TimeGenerated > ago(1h)
| where AppRoleName == "fantasy-cards-generator" and Name == "fcg.agent.detail"
| extend Detail = parse_json(tostring(Properties["fcg.detail.record"]))
| project TimeGenerated, Name, Runtime=tostring(Detail.source_runtime),
    Stage=tostring(Detail.stage), SourceTraceId=tostring(Detail.source.trace_id),
    SourceSpanId=tostring(Detail.source.span_id), Detail
| order by TimeGenerated asc
```

### Content-free operational queries

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
