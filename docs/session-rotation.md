# Session-rotation local preparation runbook

> **Status: dev provisioning succeeded; live rotation acceptance remains unproven.**
> The 2026-09-14 execution failed, and its temporary permissions were revoked.
> This document describes the private, dev-only session-secret rotation drill
> introduced for #89/#53 and exactly what has (and has not) been proven so far.
> It does not authorize, and must not be read as authorizing, an actual rotation
> against the live `fcag-dev-app`.

See [`rotation-observability.md`](rotation-observability.md) for the
`secret.rotation_observation` event contract this drill's controller consumes as
evidence. This document is about *running the drill machinery itself* (harness,
process inventory, controller, infra); the observability doc is about the
telemetry contract it reads.

## What this drill is (and is not)

- It rotates **only** `APP_SESSION_SECRET_KEY` in the dev Key Vault. That key
  signs the app's session cookie (`RotatingSessionMiddleware` /
  `itsdangerous.TimestampSigner`) for **every** session, not just anonymous
  prelogin state — it also signs the cookie holding an already-authenticated
  user's session data (`request.session[...]` after Entra login in
  `app/main.py`). Rotating it therefore has authenticated-session impact, not
  only anonymous-session impact. The drill's own *cookie-adoption probes*
  (`cookie_early`/`cookie_near`/`cookie_boundary` in `harness.py`) are
  themselves anonymous-only HTTP requests — they prove the new key signs/verifies
  correctly and that the overlap window works, but they do not exercise an
  authenticated session specifically. It never touches `ENTRA_CLIENT_SECRET`
  and never implements or tests an outage scenario. Those are separately
  authorized, unimplemented scopes.
- The drill is invoked **directly**:
  `python scripts/session_rotation/control.py session-*`. Its five temporary
  resources remain absent from `azure.yaml`, `deploy.sh`, and
  `infra/main.bicep`. The separately reviewed `web-pinned*` root deployment
  path changes only the existing dev app's image reference and required new
  revision identifier before a fresh baseline; it does not provision or run
  the drill.
- The actual rotation logic (`scripts/session_rotation/harness.py`) runs **only**
  inside a dedicated, single-purpose Container App Job execution, with its own
  identity that can read/write **only** the `app-session-secret-key` secret's
  metadata+value and pull from the ACR registry. It has no ingress, no other Key
  Vault permissions, and no access to any other app configuration or secret.

## Non-wiring guarantee

`infra/session-rotation.bicep` deploys nothing unless `enableSessionRotation=true`
is passed explicitly (default `false`), and it is never referenced from
`azure.yaml`, `deploy.sh`, or `infra/main.bicep`. This is enforced by
`tests/test_session_rotation_control.py::test_default_deploy_flows_do_not_reference_session_rotation`
and `::test_session_rotation_bicep_is_gated_behind_disabled_by_default_flag`.

## Immutable dev app image preparation

The reviewed app and runner image is:

`fcagdevqhg3qc4rlbt4gacr.azurecr.io/fantasy-cards-generator/web-nat-dev@sha256:bb7c5c4e49b9f3860d0f5aca5ccf2ff66e43921f512726551de7fc8c60ee8a11`

Read-only inspection proved that digest exists as a Linux/amd64 OCI image,
contains the same tracked `app/` bytes, `pyproject.toml`, and `uv.lock` as the
current checkout, and imports the exact harness dependencies from
`/app/.venv`: Azure Identity, Key Vault Secrets, Azure Core requests transport,
HTTPX, and itsdangerous. The helper validates only this approved target
manifest. It has no rollback, restart, build, push, or mutable-tag path.

Run the fail-closed read-only preflight before deployment:

```sh
./deploy.sh web-pinned-preview --environment dev \
  --subscription b8ff3e15-7e2d-4fac-a773-992fb59ccedd
```

It validates the fixed subscription, resource group, app and ACR resource IDs,
`azd-service-name=web-nat` tag, current revision, target manifest, expected ACR
login server, and the fixed case-insensitive ACR-pull identity resource ID.
It never performs a Container App GET. Instead, authenticated HTTPS requests to
Azure Resource Graph use server-side Kusto projections, so the HTTP response
contains only the selected metadata:

- all nine mutable 2025-01-01 configuration fields, including
  `maxInactiveRevisions`;
- secret field names plus only `name`, `identity`, and `keyVaultUrl` metadata,
  never `value`;
- container/environment field names, environment-variable names, and
  `secretRef`, never environment values.

Every configuration object is checked against the published 2025-01-01 schema;
an unknown top-level or nested field fails closed. Direct-value environment
variables must be in the Bicep-derived non-secret allowlist, the Application
Insights connection string must use its exact secret reference, and
`APP_SESSION_SECRET_KEY` or `ENTRA_CLIENT_SECRET` is forbidden in the template.

Only after that server-side guard is bound to the exact immutable revision name
does the helper fetch the revision. The official
[`ContainerAppsRevisions.json`](https://github.com/Azure/azure-rest-api-specs/blob/main/specification/app/resource-manager/Microsoft.App/ContainerApps/stable/2025-01-01/ContainerAppsRevisions.json)
response schema contains the versioned `Template` but no non-versioned
`Configuration` or configuration secret values. The returned environment
metadata must exactly match the preceding projection. Tokens remain only in
process memory; snapshots and token values are never printed or stored. Record
the emitted `baseline_fingerprint` for the independently reviewed apply
command.

The reviewed deployment command is plan-only without both gates:

```sh
./deploy.sh web-pinned --environment dev \
  --subscription b8ff3e15-7e2d-4fac-a773-992fb59ccedd \
  --expect-fingerprint <baseline_fingerprint> --reviewed
```

After review, execute the same command with `--approve-change`. Its only
mutating control-plane request is an authenticated HTTPS request whose token is
captured in memory and never logged:

```text
PATCH https://management.azure.com/subscriptions/b8ff3e15-7e2d-4fac-a773-992fb59ccedd/resourceGroups/rg-fcag-dev/providers/Microsoft.App/containerApps/fcag-dev-app?api-version=2025-01-01
Content-Type: application/json
```

The JSON Merge Patch body contains only the existing `location` (required by the
API), `properties.template.containers`, and the reviewed deterministic
`properties.template.revisionSuffix`. The complete current containers array is
preserved at the JSON data-model level except for the single `web.image` value
and omission of service-generated, read-only `resources.ephemeralStorage`.
The suffix is derived from the immutable digest and approved rollout run ID,
and the helper proves that the corresponding revision name is absent by direct
ARM revision inventory immediately before PATCH. It does not transmit
`properties.configuration`, identities, tags, environment IDs, scale, Dapr,
init containers, volumes, service binds, or any other template field. It never
calls `listSecrets`, reads an azd environment file, writes a parameter file,
invokes an azd hook, builds or pushes an image, provisions a resource, changes
RBAC, or changes networking. The PATCH response body is deliberately not read.

Immediately before PATCH, the helper re-reads the allowlisted live snapshot and
requires an exact match with the reviewed fingerprint. A narrowly bounded
recovery preflight also accepts the known failed control-plane state caused by
the inherited suffix collision, but only while the sole direct active revision
remains healthy and the original reviewed preservation, configuration, and
registry hashes match exactly. Any other failed state or drift remains blocked.
The Container Apps
2025-01-01 response does not expose a usable ETag, so this is an optimistic
concurrency gate rather than an atomic lock; serialize the short operator
window. Because the request is a partial JSON Merge Patch, a concurrent change outside
`template.containers` and `template.revisionSuffix` is neither transmitted nor
overwritten. After
PATCH, the helper polls documented transient app/revision provisioning and
readiness states. It fails immediately on a terminal state, an unknown state,
revision churn, wrong image, or preservation drift. Success is reported only
when Azure exposes a different active, provisioned, running, healthy revision
with at least one replica, the exact immutable image, `/healthz` returns 200,
and the full preservation, configuration-metadata, and ACR metadata hashes
still match. A failed or unproven rollout remains failed; no rollback, restart,
secret replay, or recovery mutation is attempted.

The expected blast radius is one new application revision of
`fcag-dev-app`. Bicep remains the infrastructure source of truth. If
post-deployment health or preservation proof fails, stop before the fresh
baseline and separately review any recovery action; do not restart during the
drill and do not fall back to a mutable tag.

A **fresh** `session-provision` baseline must be taken only after the digest
revision is healthy. Baseline proof before this redeployment is not reusable.

## Prerequisites and approvals

1. **Separately approved RBAC provisioning.** `session-provision` creates a
   dedicated custom role (`DATA_ACTIONS`: `getSecret`/`setSecret`/`readMetadata`
   on the session secret only) and two role assignments (that role + `AcrPull`),
   scoped to exactly one Key Vault secret and one ACR registry. Do not run
   `session-provision` without this approval recorded separately from this
   runbook. Azure may return the custom role resource under its historical
   resource-group alias, but assignment properties and controller comparisons
   use the exact subscription-scoped role-definition ID. Only those two exact
   forms, for the fixed subscription and deterministic role GUID, are accepted.
2. **Separately approved rotation scope + recovery plan.** The rotation itself
   (`session-run`) and any compensating recovery (`session-recover`) mutate a
   live secret version. Do not run `session-run` without this approval.
3. A clean git checkout with `az` logged in against subscription
   `b8ff3e15-7e2d-4fac-a773-992fb59ccedd` only (the controller's `--subscription`
   flag is a strict allowlist of exactly this one value; anything else exits
   non-zero before any Azure call).
4. Every mutating action requires **both** `--approve-change` and `--reviewed` on
   the command line; without both, the controller prints `PLAN ONLY: ...` and
   exits `0` without calling Azure. `session-preview` never requires either flag.

The live app revision must also prove the same reviewed image identity as the
digest-pinned rotation runner. An app revision that directly references the
pinned digest is accepted. An `azd-deploy-*` tag is accepted only when ACR
metadata resolves that exact registry/repository/tag to the pinned digest, the
tag has read/list enabled and write/delete disabled, and the tag's last update
(including its lock) predates the Container App revision creation time. Wrong
registries, repositories, tags, digests, mutable tags, late locks, and malformed
ACR responses all fail closed as `worker_image_or_command_drift` or
`worker_image_metadata_invalid`; the current tag-to-digest mapping alone is
never treated as proof of what a running replica pulled.

## Exact command sequence

Run every command from the repository root. `<run-id>` is a UUID you choose once
per drill attempt and reuse for every subsequent command in that attempt
(`python -c "import uuid; print(uuid.uuid4())"`).

```sh
# 1. Dry-run preview: confirms the deployment would create the five expected
#    resources (job, identity, role, and its two assignments) and nothing else.
#    If the exact idle identity from an abandoned attempt remains with zero
#    grants and no job, it must be the sole NoChange and the other four
#    resources must be Create. Never mutates anything; no approval flags needed.
python scripts/session_rotation/control.py session-preview \
  --subscription b8ff3e15-7e2d-4fac-a773-992fb59ccedd --run-id <run-id>

# 2. Provision: takes an inventory + observability baseline of the *current*
#    live app, previews again against that baseline's hash, and deploys the
#    five dev-only resources. Requires RBAC approval (see above).
python scripts/session_rotation/control.py session-provision \
  --subscription b8ff3e15-7e2d-4fac-a773-992fb59ccedd --run-id <run-id> \
  --approve-change --reviewed

# If provisioning returns an error or is interrupted after Azure may have
# accepted some resources, wait for Azure control-plane propagation and rerun
# this exact command with the SAME run ID and unchanged reviewed source. The
# controller reloads the saved provision_intent. A separate
# provision_attempted flag becomes true only after fresh-resource, current-app,
# baseline, ownership, and exact what-if validation all pass, immediately
# before the first deployment create call. Until then, every retry remains a
# fresh attempt and may see only no resources or the exact idle zero-grant
# identity. Once attempted, the controller validates every existing resource,
# requires Create only for missing resources and NoChange for exact matches,
# and completes the same deployment. A contract-valid job left in
# provisioningState Failed is not runnable: the same approved deployment is
# retried and must return Succeeded before provisioning completes. It never
# starts the job.
python scripts/session_rotation/control.py session-provision \
  --subscription b8ff3e15-7e2d-4fac-a773-992fb59ccedd --run-id <same-run-id> \
  --approve-change --reviewed

# 3. Run: re-validates the job/app configuration and baseline are unchanged
#    since provisioning, starts the job execution, then blocks in an observation
#    loop (polling app inventory + rotation-observation logs + job-status
#    events every ~10s) until the drill either proves its 60s-adoption/overlap
#    claims and stops the job, or the loop itself is interrupted (Ctrl-C) or
#    fails. Requires rotation-scope approval (see above). THIS IS THE STEP THAT
#    ACTUALLY MUTATES THE LIVE SECRET.
python scripts/session_rotation/control.py session-run \
  --subscription b8ff3e15-7e2d-4fac-a773-992fb59ccedd --run-id <run-id> \
  --approve-change --reviewed

# 3b. If `session-run`'s observation loop was interrupted (not failed) before
#     reaching a terminal phase, reattach to it rather than starting a new run:
python scripts/session_rotation/control.py session-observe \
  --subscription b8ff3e15-7e2d-4fac-a773-992fb59ccedd --run-id <run-id> \
  --approve-change --reviewed

# 4. Recover (only if the drill did not reach "accepted_stopped" cleanly, or if
#    you must abort early): starts a fresh job execution whose harness detects
#    the existing run tag and performs recovery-only, restoring the pre-rotation
#    value as a *new* secret version (never disabling the rotated one). This
#    command itself only starts that job and then requires the execution to
#    reach a terminal state before you can retry it; it is not idempotent
#    "success" by itself.
python scripts/session_rotation/control.py session-recover \
  --subscription b8ff3e15-7e2d-4fac-a773-992fb59ccedd --run-id <run-id> \
  --approve-change --reviewed

# 5. Cleanup: reachable from a terminal, proven state, a saved partial
#    provision_intent/aborted state, or a still-"running" state whose one
#    state-recorded execution is positively and exclusively terminal Failed.
#    It revokes whichever exact assignments exist and deletes the exact custom
#    role definition, with a fresh safe app-configuration/pinned-image check,
#    source/resource validation, exact assignment-GET-to-identity-grant-list
#    agreement, and a no-active-execution gate before each deletion.
#    The dedicated identity and an exact, idle job (if creation reached it) are
#    deliberately retained; an identity left with zero grants is safe. An exact
#    same-run job whose provisioningState is Failed may be cleaned up only when
#    it has no executions at every deletion boundary. Unknown, in-progress,
#    drifted, or foreign-run jobs remain blocked.
python scripts/session_rotation/control.py session-cleanup \
  --subscription b8ff3e15-7e2d-4fac-a773-992fb59ccedd --run-id <run-id> \
  --approve-change --reviewed

# 5b. If provisioning must be abandoned before all five resources exist, use
#     the same cleanup command and same run ID. The state is first recorded as
#     aborted, then advances to rights_revoked only after both assignments and
#     the custom role are verified absent.
#
#     If cleanup is interrupted (Ctrl-C, network failure, a failed delete) partway
#     through, retry with the exact SAME <run-id> and unchanged reviewed source
#     (the three controller scripts and both session-rotation Bicep files must be
#     byte-identical to what was reviewed — otherwise the saved state's source hash
#     will not match and the controller refuses with `reviewed_source_changed`). Re-running
#     `session-cleanup` is safe and resumable: it re-probes each role assignment and
#     the role definition individually, skips whichever of them a prior attempt
#     already confirmed deleted, and re-validates principal/role/scope on whatever
#     is left before deleting it. Before every delete it also re-runs the bounded
#     app configuration and pinned-image guards used by provisioning/run; it does
#     not inspect secret values or require process inventory to remain unchanged.
#     The identity's live assignment list must exactly match the assignment
#     resources returned by the bounded GET probes throughout partial cleanup and
#     must be empty before rights_revoked is recorded. A resource is only ever
#     treated as "already deleted" via the exact, documented ARM not-found error
#     for its type; any other failure (drift, authorization, network, inconsistent
#     assignment evidence, or an unexpected/malformed error body) still aborts the
#     whole command rather than being silently accepted as clean.
#     Calling `session-cleanup` again once everything is already revoked
#     (phase already `rights_revoked`) is also a safe no-op.
#
#     For the narrowly accepted failed-execution case, cleanup does not claim
#     that rotation or recovery was proved: `phase` remains `running` and the
#     separate `permission_phase` becomes `rights_revoked`. A missing, active,
#     additional, or foreign execution remains blocked. The successful CLI JSON
#     includes both fields, making permission cleanup distinct from rotation or
#     recovery acceptance.
```

### Region/location representation and a pending-fix recovery gap

ARM is inconsistent about how it renders a resource's `location`: some
resource types/API versions return the compact form (e.g. `eastus2`), others
the display form (e.g. `East US 2`). `validate_identity_item()` and
`validate_job_item()` compare this field exactly, so a run whose job resource
happens to come back in display form fails closed with `job_identity_drift`
even though every other field matches. Because `session-cleanup` calls the
same validators via `resource_snapshot()` before every deletion, this also
blocks the reviewed, resumable revocation path for that run specifically.

The fix is a narrow, whitespace/case-only equivalence (`same_location()`) —
any other difference (wrong region, punctuation, non-string value) still
fails closed exactly as before. Once that fix is merged and re-reviewed, the
in-repo `session-cleanup --approve-change --reviewed` command above remains
the primary, supported way to revoke a run's temporary rights, and no
separate tooling is needed. If a specific already-approved run is blocked by
this exact defect before the fix lands, a transient, non-repository reviewer
artifact may be used instead: it loads the byte-for-byte original,
commit-pinned `control.py`, applies only this same narrow location fold at
the boundary before calling its unmodified validators, and re-runs the full
guard (state/source-hash, role/identity/job contract, zero executions, and
the grants still expected to remain) before every single delete rather than
once up front. Such an artifact must never be committed to this repository
and must never touch the managed identity or the container app job, which
remain intentionally retained, reusable resources.

Local per-run state lives at `<git-dir>/session-rotation-<run-id>.json` (mode
`0600`, never a symlink) plus a `session-rotation.lock` file that prevents two
controller invocations from running concurrently against the same checkout.
Never edit either file by hand, including its phase or hashes. The reviewed
source hash covers `harness.py`, `control.py`, `process_inventory.py`,
`infra/session-rotation.bicep`, and
`infra/modules/session-rotation-runner.bicep`. A `reviewed_source_changed`
block is intentional if any of them changes after a run's state was saved:
start a fresh `<run-id>` instead. An older immutable forensic state must not be
migrated or claimed as resumed after source changes.

A schema-1 state also records the boolean `provision_attempted`.
`provision_intent` with `provision_attempted: false` proves only that the local
baseline was saved; it grants no ownership of Azure resources. The marker is
persisted immediately before the first deployment create call, never after a
failed validation or preview.

A fresh run ID may reuse only the exact named managed identity left by an
aborted attempt when the job is absent and the identity has zero role
assignments. Any job, assignment, foreign permission, principal mismatch, or
resource drift blocks reuse. Control-plane RBAC visibility does not prove Key
Vault or ACR data-plane readiness; if creation succeeds but propagation is not
ready, wait and rerun `session-provision` with the same run ID rather than
adding permissions or editing state.

## What local preparation *has* proven

All of the following are exercised in `tests/test_session_rotation_harness.py`,
`tests/test_session_rotation_inventory.py`, and
`tests/test_session_rotation_control.py` using an in-memory fake Key Vault
client, a real `RotatingSessionMiddleware` behind an in-process ASGI test
client (no network), and fully controllable clocks — never a live call:

- The harness's Key Vault history/version-integrity guards (duplicate versions,
  hash collisions, ambiguous ordering, disabled/expired/not-yet-active
  versions, tag-based idempotency and write-conflict detection).
- The exact-once-write contract: a rotation or recovery write is attempted at
  most once per process; a prior partial/ambiguous write is reconciled by
  re-reading tagged history, never retried blindly.
- Recovery restores the pre-rotation value as a **new** secret version and
  never disables the rotated ("bad") version; recovery is itself idempotent
  and requires a provably-single, provably-latest predecessor before running.
- The real cookie-overlap behavior against production
  `RotatingSessionMiddleware` code: an anonymous prelogin cookie signed before
  rotation is still accepted throughout the configured 3600s overlap window
  (anchored on the new version's `created_on`) and is rejected once that window
  elapses, at which point a fresh cookie still authenticates cleanly.
- `process_inventory.py`'s exact worker classification against a synthetic
  `/proc` tree: a single healthy `uvicorn app.entrypoint:app` worker is
  reported with `{pid, start_ticks}` only; `--workers`/`--reload` invocations,
  unrecognized command lines, and processes that vanish mid-scan are all
  bucketed as `unknown` (never silently miscounted as a legitimate worker);
  unrelated non-Python processes are ignored entirely; no argv/environment
  content is ever emitted.
- `control.py`'s existing suite (what-if resource-set exactness, approval
  gating, lock-based concurrency exclusion, redacted failure output,
  state-file symlink/permission/source-hash checks, RBAC role/assignment/
  scope-drift detection, and the `measure()` boundary-condition guards around
  the 60s adoption window, write-signal timing, and blocked/rotated/recovered
  event handling), **plus** direct coverage of `inventory()`, the
  `process_inventory()` `az containerapp exec`/PTY wrapper, `baseline()`, and
  the `observe()` loop itself:
  - `inventory()`: rejects active-revision-set/health drift, a changed
    container set per replica, an unhealthy replica (not ready / not running /
    invalid restart count), and an empty replica list; builds a correct
    `{replica: {pid, start_ticks, restarts}}` snapshot across multiple healthy
    replicas.
  - `process_inventory()`: rejects a non-zero exec exit, a missing/garbled
    `SESSION_PROCESS_INVENTORY=` marker line, any `unknown` process count > 0,
    any worker count other than exactly one, invalid `pid`/`start_ticks`
    types or values, and a wall-clock bracket that no longer proves the
    conservative time bound — tested against a real PTY pair
    (`pty.openpty()`) with only the process spawn faked, so the real
    `select()`/`os.read()` drain loop runs unmocked.
  - `baseline()`: rejects a duplicate/restarted process for a replica, a
    missing log stream (session/entra `provider`, entra `entra_binding`), an
    unhealthy or malformed row, a missing "unchanged" session row, hash
    instability across replicas, and an unaccounted worker in the recent
    window; builds a correct multi-replica snapshot when healthy.
  - `observe()`: rejects running without `--approve-change`, running from a
    non-`"running"` phase, app-configuration turnover, job-execution-identity
    turnover, and a wall/monotonic clock step; propagates a genuine
    worker-turnover rejection from a real (unmocked) `measure()` call end to
    end; reaches `accepted_stopped` after the job confirms `Stopped`; raises
    `stop_unconfirmed_watchdog_may_recover` when a stop is never confirmed;
    and loops (`sleep(10)`) across multiple not-yet-finished iterations before
    completing.
  - `measure()`: an added boundary-condition pair pinning that adoption
    evidence at `elapsed + CLOCK_MARGIN > 60s` is dropped (not counted, not
    fatal) while evidence just inside the 60s bound is recorded.

## What remains unproven — do not assume otherwise

- **Live ≤60s adoption.** The `measure()` conservative upper-bound logic and the
  harness's own cookie-probe timing are unit/integration-tested against fakes;
  no real Container App replica, real Key Vault propagation delay, or real
  ACA log-ingestion latency has been observed. The 60-second bound is a
  contractual assertion the *controller* will refuse to accept without
  evidence — it is not yet evidence that live adoption meets it.
- **Live session-cookie overlap / rejection against the deployed app.** Proven
  against the real middleware code in-process; not proven against the actual
  deployed `fcag-dev-app` container, its real Key Vault refresh cadence, or
  concurrent live traffic.
- **Entra credential rotation and overlap.** Explicitly out of scope for this
  drill's implementation; the harness only ever touches the session secret.
- **Outage/failure-injection scenarios.** Not implemented, not tested here.
- **Worker-turnover reconciliation logic is unit-tested against fakes, not
  successful live acceptance.** `control.py`'s `inventory()`/`baseline()`/`observe()`
  functions (which reconcile process identity via
  `(revision, replica, pid, incarnation)` against `az containerapp
  exec`/`replica list`/observation-log output) now have direct unit coverage
  in `tests/test_session_rotation_control.py` — including a real PTY pair for
  `process_inventory()`'s exec wrapper and an unmocked `measure()` call inside
  an `observe()` end-to-end turnover-rejection test. What remains unproven is
  the same as everywhere else in this document: these are fakes, not a real
  `az containerapp exec`/Log Analytics ingestion/job-execution lifecycle. The
  2026-09-14 live attempt stopped with `azure_transport_failed` and a failed
  job execution; it did not establish adoption, overlap, or recovery acceptance.

## Private network constraints

The job's identity resolves the Key Vault via a private DNS zone pinned to
exactly one address (`10.42.2.7` for `kvfcagdevqhg3qc4rlbt4g.vault.azure.net`);
`harness.py` refuses to proceed (`private_dns_mismatch`) if that resolution
ever returns anything else, including additional addresses. The job has no
ingress and communicates outbound only to Key Vault and to the app's public
FQDN (`fcag-dev-app.<...>.azurecontainerapps.io`) for the anonymous cookie
probes; both hosts are validated against strict patterns before use.

## Recovery plan summary

- If `session-run`'s observation loop errors out (`stop_unconfirmed_watchdog_may_recover`,
  a Ctrl-C, or any other exception) **after** a rotation was written
  (`"rotated"` appears in the job's own event log), run `session-recover`. It
  refuses to run (`rotation_unprovable`) if no rotation can be proven, and
  refuses (`ambiguous_recovery_blocked`) if a prior recovery attempt already
  left an ambiguous outcome — in either case this is a deliberate fail-closed
  stop, not a bug to route around.
- If the job's own soft watchdog fires first (`harness.py`'s `WATCHDOG=4200`s
  monotonic clock inside `experiment()`), the harness does **not** simply exit
  and leave the rotated version untouched: its top-level `except Exception`
  handler in `main()` sees that a rotation was attempted and calls
  `rotation.recover()` **itself, in the same process**, restoring the
  pre-rotation value as a new version before exiting. Only if that in-process
  recovery attempt *also* fails does it emit `manual_recovery_required` and
  exit non-zero without having restored anything — that is the case where an
  external `session-recover` run is actually required. As a hard backstop
  independent of the harness's own clock/sleep loop, `signal.alarm(4400)`
  raises a `TimeoutError` after 4400s regardless of what the harness is doing;
  depending on exactly where that interrupts execution it may still be caught
  by the same in-process recovery path, or (if it fires before that inner
  `try` is even entered) fall through to a bare `unexpected_failure` exit with
  no recovery attempted. The job's `replicaTimeout=4500`s is a further,
  outermost Azure-level backstop. In every case, run `session-recover` next:
  it is safe and idempotent, and refuses to run (`rotation_unprovable` /
  `ambiguous_recovery_blocked`) if recovery cannot be proven safe rather than
  guessing.
- `session-cleanup` never runs while any job execution is non-terminal, and
  re-validates every role/assignment's principal/role/scope immediately before
  each delete — it is safe (and required) to run after either a successful or
  a failed/recovered attempt. It is also resumable: if it is interrupted after
  deleting some but not all of the two role assignments and the role
  definition, retry it with the same `<run-id>` (reviewed source unchanged —
  see step 5b above). The retry independently re-probes each of the three
  resources, skips any that are already confirmed gone (the exact, documented
  ARM not-found error only), and revalidates + deletes whatever remains; a
  second retry after everything is already revoked is a safe no-op. Any
  unrelated failure while probing or deleting (drift, authorization, network,
  malformed response) still aborts the command instead of ever being treated
  as "already deleted".

## Escalation

If, at execution time, live telemetry or process inventory cannot rigorously
prove one of the guarantees above (adoption bound, worker identity, overlap,
recovery outcome), the harness and controller are designed to **fail closed**
(`blocked` / `manual_recovery_required` / a fixed `ControlError` code) rather
than accept a plausible-looking but unproven result. Do not work around such a
block; escalate for an architecture/telemetry fix instead.
