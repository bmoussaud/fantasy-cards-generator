# Secret adoption observations

`secret.rotation_observation` is a content-free log event, not an authentication
probe or a public diagnostic endpoint. Each successful Azure provider refresh
emits an observation, including unchanged versions. Initial lifespan preload and
idle refresh workers use the same emission path. Entra emits a separate observation
only after the OAuth manager has assigned its actual current client binding.

This implementation supplies local emission evidence. It does **not** establish
that the deployed revision exports these events, that every live process adopts
within 60 seconds, or that Entra accepts a credential. Those require separate,
authorized live observations; deploying or rotating secrets is not part of this change.

## Event contract (schema version 1)

Both sinks receive the same deterministic, single-line JSON object. All fields are
required; extra fields are removed at the console and SDK privacy boundaries.

| JSON field | Value |
| --- | --- |
| `event` | `secret.rotation_observation` |
| `schema_version` | Integer `1` |
| `observed_at` | UTC emission time, `YYYY-MM-DDTHH:MM:SS.ffffffZ` |
| `sequence` | Increasing process-local integer, starting at 1, maximum signed 64-bit |
| `pid` | Original emitting OS process ID, positive signed 32-bit |
| `incarnation` | Random UUID as 32 lowercase hexadecimal characters |
| `revision` | Validated `CONTAINER_APP_REVISION`, otherwise `unknown` |
| `replica` | Validated `CONTAINER_APP_REPLICA_NAME`, otherwise `unknown` |
| `logical_secret` | `session` or `entra`; never a vault or environment secret name |
| `source` | `azure`, `env`, or `unknown` |
| `stage` | `provider` or `entra_binding` (the latter only for `entra`) |
| `result` | `adopted`, `unchanged`, `stale`, or `failed` |
| `version_hash` | `SHA256(version_identifier_utf8)[:12]`, lowercase hexadecimal; `unknown` for no version |
| `error_category` | `none`, `unknown`, `timeout`, `not_found`, `access_denied`, `network_error`, `unavailable`, or `binding_error` |

Revision and replica IDs must match `[A-Za-z0-9][A-Za-z0-9_-]{0,127}` exactly.
Invalid values are replaced, not truncated. These fields never come from request
headers. Missing metadata stays `unknown`: do not infer a replica from a hostname,
user input, or a successful request. The deployment owner must confirm both named
environment variables are present and match the live platform inventory, or arrange
trusted platform metadata injection before accepting per-replica evidence.

The incarnation is initialized at the first process observation. A fork resets the
identity, sequence, and lock, so a child cannot inherit the parent's identity or a
locked mutex. Use `(revision, replica, pid, incarnation)` as the process key; neither
PID alone nor replica alone identifies an individual worker across restarts.
Concurrent emitters share one sequence. There are no process/version metric labels.

### Interpretation

- `provider/adopted`: the provider published a successfully refreshed current
  snapshot with a different version, or its first current snapshot.
- `provider/unchanged`: a successful refresh completed for the same version. A
  cache hit is not a successful refresh and does not create an Azure provider heartbeat.
- `entra_binding/adopted`: the manager assigned a new current OAuth client binding.
  `unchanged` means it assigned refreshed metadata while reusing the current client.
  A provider observation alone cannot establish either condition.
- `stale`: the refresh was degraded but an eligible cached value/binding remains.
  It is not evidence of a successful refresh, even if its hash matches a target.
- `failed`: the provider could not supply an eligible snapshot, or the binding
  refresh failed. Its hash may describe a retained version, or be `unknown`; it
  never proves that version is usable.

Successful observations have `error_category=none`; degraded observations have a
bounded non-`none` category. No exception message, token, value, identity, vault URI,
secret name, or raw version is part of the event. Environment-backed providers have
no version identifiers and always report `version_hash=unknown`; these observations
cannot prove adoption of a particular vault version. Only the two logical application
rotation targets are observed; arbitrary provider references are outside this contract.

Workers currently poll independently at 20-second start-to-start intervals, with
the existing 25-second refresh budget and 5-second failure cooldown. These are
operating parameters, **not a hard real-time bound**: scheduling, retries, network
failure, process restarts, clock skew, and ingestion delay still matter. There is
no binding refresh heartbeat when a worker's provider refresh fails before invoking
the binding callback; treat that absence as missing evidence, not successful adoption.
Binding failures during an attempted refresh emit their own failed observation.

## Export paths

The isolated INFO logger `fantasy_cards_generator.telemetry.rotation` always emits
sanitized JSON to stderr, which ACA collects as console logs. It does not propagate
to root/framework handlers. This route is independent of `TELEMETRY_ENABLED` and
Application Insights initialization/ingestion. Repeated configuration does not add
duplicate console or export handlers. A console I/O error reports only the fixed
`rotation.observability_delivery_failed` diagnostic, without dumping an exception;
if stderr itself is unavailable, the I/O error surfaces rather than claiming delivery.

When Azure Monitor is configured, the logger shares the SDK handler registered on
the existing `fantasy_cards_generator.telemetry` logger. The SDK privacy processor
revalidates only this dedicated log scope and JSON contract, reconstructs the body,
and exports each field as `rotation.<field>` in log attributes/custom dimensions.
Malformed contract events become `rotation.observation_rejected`, with no attributes.
General attribute/metric privacy allowlists are not widened.

Console collection and Application Insights ingestion are independent operational
dependencies. Either can be delayed or unavailable. Local tests use the actual
OpenTelemetry logging handler, privacy processor, and in-memory exporter; they do
not contact Azure or prove live collection.

## Queries

For Application Insights' `traces` table, start with:

```kusto
let observations =
    traces
    | where timestamp > ago(30m)
    | where message has "secret.rotation_observation"
    | extend o = parse_json(message)
    | where o.event == "secret.rotation_observation" and toint(o.schema_version) == 1
    | project o;
observations
| project observed_at=todatetime(o.observed_at), revision=tostring(o.revision),
          replica=tostring(o.replica), pid=toint(o.pid), incarnation=tostring(o.incarnation),
          sequence=tolong(o.sequence), logical_secret=tostring(o.logical_secret),
          stage=tostring(o.stage), result=tostring(o.result),
          version_hash=tostring(o.version_hash), error_category=tostring(o.error_category)
| order by observed_at asc
```

Workspace-based queries may use `AppTraces`, `TimeGenerated`, `Message`, and
`Properties` instead of `traces`, `timestamp`, `message`, and `customDimensions`.
For ACA console collection using the custom table, substitute this source:

```kusto
let observations =
    ContainerAppConsoleLogs_CL
    | where TimeGenerated > ago(30m)
    | where Log_s has "secret.rotation_observation"
    | extend o = parse_json(Log_s)
    | where o.event == "secret.rotation_observation" and toint(o.schema_version) == 1
    | project o;
observations
| summarize count() by revision=tostring(o.revision), replica=tostring(o.replica),
                       pid=toint(o.pid), incarnation=tostring(o.incarnation),
                       logical_secret=tostring(o.logical_secret), stage=tostring(o.stage)
```

For resource-specific console tables, use the corresponding
`ContainerAppConsoleLogs` / `Log` columns. Live ACA console streaming also carries
the same JSON lines; retain the complete events, not a single matching log line.
Choose one complete sink per analysis or deduplicate combined sinks by process key
and `sequence`. Do not treat ingestion timestamps as adoption timestamps.

### Before/after membership and the 60-second acceptance gate

Before an authorized rotation, independently inventory every active revision,
replica, and configured/running application worker. Require recent successful
baseline observations for all expected processes: `session/provider`,
`entra/provider`, and `entra/entra_binding` when Entra is configured. Reconcile the
worker count against platform/process inventory, not just the set of processes
that happened to log. Any `unknown` metadata, missing stream, unexplained restart,
or missing process blocks an "every existing process" conclusion.

Save that baseline and the intended target hashes. Compute hashes from the exact
provider **version identifier**, never from the secret value. Record the independent
activation time for each secret. The query below illustrates one rotated logical
secret; substitute the source above and actual activation time/hash only after
authorization. Repeat for each target with its own activation time.

```kusto
// Replace these synthetic parameters; do not enter a secret value or raw version.
let rotation_started = datetime(2026-01-01T00:00:00Z);
let target_secret = "entra";
let target_hash = "0123456789ab";
// Define observations from one of the sources above with an adequate time window.
let evidence = observations
    | extend observed_at=todatetime(o.observed_at),
             process_key=strcat(o.revision, "/", o.replica, "/", o.pid, "/", o.incarnation),
             logical_secret=tostring(o.logical_secret), stage=tostring(o.stage),
             result=tostring(o.result), version_hash=tostring(o.version_hash),
             error_category=tostring(o.error_category)
    | where logical_secret == target_secret;
let baseline = evidence
    | where observed_at between ((rotation_started - 2m) .. rotation_started)
    | summarize arg_max(observed_at, *) by process_key, logical_secret, stage
    | project process_key, logical_secret, stage,
              baseline_healthy=(result in ("adopted", "unchanged") and error_category == "none");
let after = evidence
    | where observed_at > rotation_started and observed_at <= rotation_started + 5m
    | summarize first_target=minif(observed_at,
                                  version_hash == target_hash
                                  and result in ("adopted", "unchanged")
                                  and error_category == "none"),
                last_observed=max(observed_at),
                degraded=countif(result in ("stale", "failed"))
                by process_key, logical_secret, stage;
baseline
| join kind=leftouter after on process_key, logical_secret, stage
| extend adoption_seconds=datetime_diff("millisecond", first_target, rotation_started) / 1000.0
| extend observed_within_60s=baseline_healthy and isnotnull(first_target)
                            and adoption_seconds >= 0 and adoption_seconds <= 60
| project process_key, logical_secret, stage, baseline_healthy,
          first_target, adoption_seconds, observed_within_60s, last_observed, degraded
```

The baseline must first pass inventory reconciliation; this query alone cannot
discover a worker that never emitted anything. Review all rows, missing targets,
sequence gaps, degraded events, and post-rotation process membership. Reconcile
new/removed incarnations separately rather than replacing old missing members with
healthy new workers. Successful target observations bound *observed* adoption from
above; they do not measure the exact credential mutation instant or prove continuous
availability between observations.

Finally, validate real identity authentication separately through the authorized
Entra flow. Provider publication, current OAuth binding assignment, and successful
Entra credential acceptance are three different claims. None may substitute for
the others, and a version hash is not proof of credential validity.
