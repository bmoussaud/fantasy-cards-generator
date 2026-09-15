# Dev static-auth rollback migration

This fixed-scope helper transfers the current version-pinned Key Vault values
for `app-session-secret-key` and `entra-client-secret` into Azure Container
Apps native secrets without exposing their values to the operator. It targets
only subscription `b8ff3e15-7e2d-4fac-a773-992fb59ccedd`, resource group
`rg-fcag-dev`, app `fcag-dev-app`, vault `kvfcagdevqhg3qc4rlbt4g`, and the
`fantasy-cards-generator/web-nat-dev` repository in the fixed dev ACR.

The transfer parent resolves existing ACA native values with `listSecrets`
inside Azure and passes the full snapshot through secure child parameters.
Key Vault values enter ARM as version-pinned parameter references, never as
CLI arguments, files, outputs, diagnostics, or operator-visible values.

## Required provenance

Run this only from the clean checkout of the **new reviewed rollback commit**
that preserves later application features while restoring pre-#53 static-auth
semantics. The older `f9081f3...` auth baseline is explicitly rejected and is
not a valid rollback source.

The helper stores a mode-`0600`, non-secret receipt beneath the worktree's
private Git metadata. The receipt binds the fixed resources, schema version,
reviewed source SHA, exact deploy-context fingerprint, original writable Key
Vault representation and its hash, helper-owned azd package/deploy attempt,
fresh ACR tag and timestamp, resolved digest, published revision, and pinned
revision. It contains no secret values. If transfer access is already enabled
without this receipt, the helper refuses to invent a previous vault state.

Set the reviewed source once:

```bash
ROLLBACK_SOURCE="$(git rev-parse HEAD)"
test -z "$(git status --porcelain --untracked-files=all -- \
  Dockerfile azure.yaml app pyproject.toml uv.lock)"
```

## Canonical execution

Every Azure mutation has its own plan, fingerprint, review, immediate state
recheck, and apply invocation. Never reuse a fingerprint from another stage.

### 1. Temporarily enable ARM Key Vault resolution

`publicNetworkAccess` remains `Disabled`. The receipt captures the exact
original `networkAcls` representation before this write, including a missing
property, `null`, an empty object, or existing rule arrays.

```bash
python deployments/dev-static-auth/migrate.py access \
  --rollback-source "$ROLLBACK_SOURCE"

python deployments/dev-static-auth/migrate.py access \
  --rollback-source "$ROLLBACK_SOURCE" \
  --apply --approve-change --reviewed \
  --expect-fingerprint <fresh-access-fingerprint>
```

### 2. Transfer the two version-pinned values to ACA native secrets

```bash
python deployments/dev-static-auth/migrate.py transfer \
  --rollback-source "$ROLLBACK_SOURCE"

python deployments/dev-static-auth/migrate.py transfer \
  --rollback-source "$ROLLBACK_SOURCE" \
  --apply --approve-change --reviewed \
  --expect-fingerprint <fresh-transfer-fingerprint>
```

The secure Azure-side pass-through preserves Application Insights and every
other existing ACA native secret. Only the two target values are replaced from
their exact Key Vault versions and referenced by the application environment.

### 3. Build, publish, and deploy the reviewed web source

The migration helper owns the canonical web-only package and deploy commands.
It records the source fingerprint, current app revision/image, and ACR tag
inventory before running any build. Apply rechecks that state, persists a
started attempt, runs `azd package web-nat --no-prompt`, then runs `azd deploy
web-nat --no-prompt --timeout 1200`. It accepts only a new healthy revision
running a new `azd-deploy-*` tag whose registry timestamp is at or after the
owned attempt start. Source cleanliness and fingerprint are rechecked after
both commands.

```bash
python deployments/dev-static-auth/migrate.py deploy \
  --rollback-source "$ROLLBACK_SOURCE"

python deployments/dev-static-auth/migrate.py deploy \
  --rollback-source "$ROLLBACK_SOURCE" \
  --apply --approve-change --reviewed \
  --expect-fingerprint <fresh-deploy-fingerprint>
```

The apply result prints the immutable `image` value to use as
`ROLLBACK_IMAGE`. Do not infer it from an older tag or manually edit the
receipt:

```bash
ROLLBACK_IMAGE="fcagdevqhg3qc4rlbt4gacr.azurecr.io/fantasy-cards-generator/web-nat-dev@sha256:<deploy-result-digest>"
```

A failed or interrupted owned attempt remains non-successful in the private
receipt. Rerunning the stage requires a fresh reviewed fingerprint and another
actual package/deploy attempt; an existing tag or revision can never be
promoted into provenance.

### 4. Pin the running revision to the reviewed digest

This bounded stage changes only the web image and revision suffix through the
same secure ACA snapshot/listSecrets parent-child mechanism.

```bash
python deployments/dev-static-auth/migrate.py deploy-image \
  --rollback-source "$ROLLBACK_SOURCE" \
  --rollback-image "$ROLLBACK_IMAGE"

python deployments/dev-static-auth/migrate.py deploy-image \
  --rollback-source "$ROLLBACK_SOURCE" \
  --rollback-image "$ROLLBACK_IMAGE" \
  --apply --approve-change --reviewed \
  --expect-fingerprint <fresh-deploy-image-fingerprint>
```

Cleanup remains blocked unless the actual healthy latest/ready revision runs
this exact full-registry `@sha256` image and the receipt matches the reviewed
source and published artifact.

### 5. Independently clean the application, vault, and role

The legacy `cleanup` command is plan-only and reports remaining stages:

```bash
python deployments/dev-static-auth/migrate.py cleanup \
  --rollback-source "$ROLLBACK_SOURCE" \
  --rollback-image "$ROLLBACK_IMAGE"
```

Invoke each remaining mutation separately with its own newly reviewed
fingerprint:

```bash
python deployments/dev-static-auth/migrate.py cleanup-app \
  --rollback-source "$ROLLBACK_SOURCE" \
  --rollback-image "$ROLLBACK_IMAGE"
python deployments/dev-static-auth/migrate.py cleanup-app \
  --rollback-source "$ROLLBACK_SOURCE" \
  --rollback-image "$ROLLBACK_IMAGE" \
  --apply --approve-change --reviewed \
  --expect-fingerprint <fresh-cleanup-app-fingerprint>

python deployments/dev-static-auth/migrate.py cleanup-vault \
  --rollback-source "$ROLLBACK_SOURCE" \
  --rollback-image "$ROLLBACK_IMAGE"
python deployments/dev-static-auth/migrate.py cleanup-vault \
  --rollback-source "$ROLLBACK_SOURCE" \
  --rollback-image "$ROLLBACK_IMAGE" \
  --apply --approve-change --reviewed \
  --expect-fingerprint <fresh-cleanup-vault-fingerprint>

python deployments/dev-static-auth/migrate.py cleanup-role \
  --rollback-source "$ROLLBACK_SOURCE" \
  --rollback-image "$ROLLBACK_IMAGE"
python deployments/dev-static-auth/migrate.py cleanup-role \
  --rollback-source "$ROLLBACK_SOURCE" \
  --rollback-image "$ROLLBACK_IMAGE" \
  --apply --approve-change --reviewed \
  --expect-fingerprint <fresh-cleanup-role-fingerprint>
```

`cleanup-app` removes only `SECRET_PROVIDER_BACKEND`,
`SECRET_PROVIDER_CACHE_TTL_SECONDS`,
`SECRET_PROVIDER_REQUEST_TIMEOUT_SECONDS`, `SECRET_PROVIDER_MAX_RETRIES`,
`SECRET_PROVIDER_RETRY_BACKOFF_SECONDS`, and
`SECRET_PROVIDER_MAX_STALE_SECONDS`. It retains `KEY_VAULT_URI`, the two native
secret references, Application Insights, identity, ingress, traffic, scale,
volumes, registries, sidecars, and unrelated environment.

`cleanup-vault` restores the receipt's exact original writable snapshot. It
does not synthesize a `Deny` object when the original `networkAcls` was missing
or `null`, and it preserves existing rule arrays and flags byte-for-byte in the
projected JSON representation.

`cleanup-role` proceeds only after the digest-pinned app is healthy,
application cleanup is complete, and the vault exactly matches the receipt.
It requires the principal/scope/role-definition inventory to contain exactly
the one reviewed Key Vault Secrets User assignment, rechecks that complete
state immediately before deletion, and confirms explicit post-delete absence.
Azure errors, including `RoleAssignmentDoesNotExist`, are failures rather than
proof of absence.

## Interrupted cleanup

All cleanup phases are deterministic and independently retryable. If
`cleanup-app` succeeded, `cleanup-vault` restored the original state, and role
deletion failed, rerun only `cleanup-role` plan and apply with a fresh
fingerprint. Completed app or vault phases report `alreadyApplied`; they do not
require the vault to remain transfer-enabled, overwrite the approved image, or
repeat secret transfer.

## Validation

```bash
uv run pytest -q tests/test_static_auth_migration.py
uv run ruff check deployments/dev-static-auth/migrate.py tests/test_static_auth_migration.py
uv run black --check deployments/dev-static-auth/migrate.py tests/test_static_auth_migration.py
az bicep build --file deployments/dev-static-auth/infra/main.bicep --stdout >/dev/null
az bicep build --file deployments/dev-static-auth/infra/vault-transfer-access.bicep --stdout >/dev/null
```
