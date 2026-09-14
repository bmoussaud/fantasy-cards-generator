# Session-rotation local preparation runbook

> **Status: local preparation only. No live drill has been run.** This document
> describes how to *prepare* the private, dev-only session-secret rotation drill
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
- It is invoked **directly**: `python scripts/session_rotation/control.py session-*`.
  There is no `azd`/`azure.yaml`/`deploy.sh`/`infra/main.bicep` wiring, and none
  should be added without a separate decision — see "Non-wiring guarantee" below.
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

## Prerequisites and approvals

1. **Separately approved RBAC provisioning.** `session-provision` creates a
   dedicated custom role (`DATA_ACTIONS`: `getSecret`/`setSecret`/`readMetadata`
   on the session secret only) and two role assignments (that role + `AcrPull`),
   scoped to exactly one Key Vault secret and one ACR registry. Do not run
   `session-provision` without this approval recorded separately from this
   runbook.
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

## Exact command sequence

Run every command from the repository root. `<run-id>` is a UUID you choose once
per drill attempt and reuse for every subsequent command in that attempt
(`python -c "import uuid; print(uuid.uuid4())"`).

```sh
# 1. Dry-run preview: confirms the deployment would create exactly the five
#    expected resources (job, identity, role, and its two assignments) and
#    nothing else. Never mutates anything; no approval flags needed.
python scripts/session_rotation/control.py session-preview \
  --subscription b8ff3e15-7e2d-4fac-a773-992fb59ccedd --run-id <run-id>

# 2. Provision: takes an inventory + observability baseline of the *current*
#    live app, previews again against that baseline's hash, and deploys the
#    five dev-only resources. Requires RBAC approval (see above).
python scripts/session_rotation/control.py session-provision \
  --subscription b8ff3e15-7e2d-4fac-a773-992fb59ccedd --run-id <run-id> \
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

# 5. Cleanup: only reachable from a terminal, proven state
#    (accepted_stopped / recovered / provisioned-with-no-execution /
#    rights_revoked). Revokes both role assignments and deletes the custom
#    role definition, each re-verified immediately before deletion. Always
#    run this once you are done with a drill attempt, successful or not.
python scripts/session_rotation/control.py session-cleanup \
  --subscription b8ff3e15-7e2d-4fac-a773-992fb59ccedd --run-id <run-id> \
  --approve-change --reviewed

# 5b. If step 5 is interrupted (Ctrl-C, network failure, a failed delete) partway
#     through, retry with the exact SAME <run-id> and reviewed source (harness.py /
#     control.py / process_inventory.py must be byte-identical to what was reviewed —
#     otherwise the saved state's source hash will not match and the controller
#     refuses to proceed with `reviewed_source_changed`). Re-running
#     `session-cleanup` is safe and resumable: it re-probes each role assignment and
#     the role definition individually, skips whichever of them a prior attempt
#     already confirmed deleted, and re-validates principal/role/scope on whatever
#     is left before deleting it. A resource is only ever treated as "already
#     deleted" via the exact, documented ARM not-found error for its type; any other
#     failure (drift, authorization, network, an unexpected/malformed error body)
#     still aborts the whole command rather than being silently accepted as clean.
#     Calling `session-cleanup` again once everything is already revoked
#     (phase already `rights_revoked`) is also a safe no-op.
```

Local per-run state lives at `<git-dir>/session-rotation-<run-id>.json` (mode
`0600`, never a symlink) plus a `session-rotation.lock` file that prevents two
controller invocations from running concurrently against the same checkout.
Never edit either file by hand; a `reviewed_source_changed` block is intentional
if `harness.py`/`control.py`/`process_inventory.py` change after a run's state
was saved — start a fresh `<run-id>` instead.

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
  live Azure.** `control.py`'s `inventory()`/`baseline()`/`observe()`
  functions (which reconcile process identity via
  `(revision, replica, pid, incarnation)` against `az containerapp
  exec`/`replica list`/observation-log output) now have direct unit coverage
  in `tests/test_session_rotation_control.py` — including a real PTY pair for
  `process_inventory()`'s exec wrapper and an unmocked `measure()` call inside
  an `observe()` end-to-end turnover-rejection test. What remains unproven is
  the same as everywhere else in this document: these are fakes, not a real
  `az containerapp exec`/Log Analytics ingestion/job-execution lifecycle. No
  live drill has exercised this reconciliation against the real
  `fcag-dev-app`.

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
