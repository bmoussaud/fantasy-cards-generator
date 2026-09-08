# Dev endpoint persistence: fail-closed candidate, **NOT approved to apply**

Refs #109. The endpoint is **not persisted** by this PR. Actual resource-provider
validation found a blocker that a GET-snapshot PUT cannot safely work around
under the no-secret-read/no-secret-change constraints. Do not merge this as
successful persistence evidence, bypass the gate, or start the model smoke.

## Actual leaf what-if evidence (2026-09-08)

Existing target:
`/subscriptions/b8ff3e15-7e2d-4fac-a773-992fb59ccedd/resourceGroups/rg-fcag-dev/providers/Microsoft.App/containerApps/fcag-dev-app`.
API: **2025-01-01**. Existing project ARM GET at API **2025-06-01**
confirmed exactly:
`https://aifcagdevqhg3qc4rlbt4g.services.ai.azure.com/api/projects/fantasy-cards-dev`.

| Diagnostic | Actual result | Gate |
| --- | --- | --- |
| Resource-group leaf with secure snapshot parameter | `Succeeded`, app `Modify`, 40 unrelated resources `Ignore`; secure parameter left `properties`, `identity`, and `tags` unevaluated | Rejected: uninspectable, not a clean plan |
| Equivalent literal leaf, current secret names retained without values | Provider validation rejected with `InvalidTemplateDeployment` / **`ContainerAppSecretInvalid`** | Rejected: actual PUT input invalid |
| Equivalent literal leaf with native `configuration.secrets` omitted | `Succeeded`, app `Modify`; **Delete `properties.configuration.secrets`**, plus web env addition and revision suffix modification | Rejected: secret deletion is outside scope |

These are actual ARM what-if calls, not local guesses or subscription-level
empty expanded modules. The failed literal request was repeated once to extract
only allowlisted error identifiers. A credential-free empty-template stdin
transport check also succeeded; it was **not** treated as app-plan evidence.
No further cloud plan experiments are needed to establish this specific blocker.

Final GET-only verification: app `Succeeded`/`Running`, latest == latest-ready,
revision still `fcag-dev-app--azd-1788775203`, image still
`fcagdevqhg3qc4rlbt4gacr.azurecr.io/fantasy-cards-generator/web-nat-dev:azd-deploy-1788775195`,
expected system principal matched, Single/100%-latest unchanged, endpoint absent.
Final GET fingerprint:
`ea77f0bf259648a25fd2171f61ab2354c1989f9301ae6d2984c187cf15e0b3de`.
This is a blocked-baseline record, **not an approved application fingerprint**.

The dev app currently has an ACA-native secret whose GET representation contains
only its name. Returning that object as a PUT fails validation; omitting it
produces the forbidden deletion plan. A successful omission validation does
**not** establish secret preservation. This code rejects both cases and now
blocks native-secret snapshots locally before preview/application.

Resolving that blocker requires a separately reviewed change of constraints or
design. This PR does **not** retrieve a secret, silently substitute an
Application Insights value, migrate secrets, add a deployment script/identity/
permission, use an imperative Container App PATCH, or assert that a predicted
secret deletion is harmless. **Independent coordinator review is required;
there is no approved application command for the current dev state.**

## Proposed operator path

The isolated resource-group Bicep leaf contains exactly one writable resource:
the existing `fcag-dev-app`. The Python helper verifies hardcoded ownership,
system MI, same-RG user-assigned identities, managed environment, Single mode,
100%-latest traffic, and the canonical endpoint returned by the real project.
It transforms the live current state rather than replaying `infra/main.bicep`.
Only `web`'s endpoint entry and a fresh revision suffix may change. Sidecars,
init containers, images/digests, all other env/secret refs, registry config,
networking/ingress, identity maps, scale, probes, mounts, and volumes are retained.
Unknown fields (even null) fail closed. Read-only fields are explicitly removed,
not guessed writable. Nonempty undocumented delegated identities also fail.

Native secret values cannot be reconstructed from metadata; their presence
blocks this candidate. Existing Key Vault reference metadata is structurally
replayable, but this is not authorization to migrate the live native secret.

```bash
# From the repository worktree. Read-only; currently returns the native-secret blocker.
python deployments/dev-endpoint/persist_endpoint.py

# FUTURE ONLY, after the blocker is independently resolved and a complete
# successful fresh preview fingerprint has been reviewed:
python deployments/dev-endpoint/persist_endpoint.py \
  --apply --expect-fingerprint <fresh-reviewed-sha256>
```

The helper uses the already-installed Azure CLI and Bicep compiler. This is a
deliberate operator leaf path, **not** `azd up`/root `azd provision`: root's
pre/post-provision hooks can generate/rotate secrets or bootstrap an image.
No azd state, `.env`, manifest inheritance, installer, model call, or root hook
is used. A separate azd manifest would not fix the ARM PUT secret semantics.

Apply uses the compiled Bicep template and a **secureObject** snapshot parameter,
transmitted through stdin to ARM, never a file, shell argument, or azd env value.
What-if cannot expand that secure parameter. For diagnostics only, the helper
verifies the compiled leaf consists of four direct parameter projections,
materializes those exact projections in memory, and submits the equivalent
literal leaf through stdin. Strings beginning `[` are escaped as ARM literals.
This preview is never submitted as a deployment. All raw tool stdout/stderr and
before/after JSON remain in memory; only the public endpoint, resource ID, safe
status, permitted path names, and fingerprint are printed. No secret-list or
Key Vault value operation exists. Do not use CLI debug output or dump requests.

The plan gate compares complete evaluated before/after configurations, not
merely a `Modify` resource count or loose delta prefix. Unrelated resource
operations, missing/unexpanded payloads, image/identity/network/secret changes,
and unknown output fields are rejected. Known presentation normalizations are
limited to location formatting, absent nulls, empty registry username/password
references, and empty identity settings.

## Concurrency, verification, rollback

The SHA-256 baseline includes complete GET metadata, including revision and
`systemData`, without persisting that GET. A fresh read must match after preview
and immediately before apply. API 2025-01-01 returned no ETag. These rereads are
**optimistic**, not an atomic lock: serialize the operator change window; do not
run another deploy concurrently. A moved fingerprint requires a new preview
and review, not an override.

A new revision is expected, not a failure. Future application must compare all
writable fields to the exact desired snapshot and require app
`Succeeded`/`Running` plus latest == latest-ready before reporting success.
Single mode and 100%-latest policy remain mandatory. No web `/generate` switch
or feature flag is changed. A timeout or mismatch means application is
unverified; it never authorizes a model call or blind deployment retry.

The pre-change endpoint was absent. Rollback therefore removes **only** that
entry using a new live snapshot and new revision, not an old full app template
or old traffic assignment:

```bash
# Preview only; the current native-secret blocker applies to rollback too.
python deployments/dev-endpoint/persist_endpoint.py --remove
# FUTURE ONLY after independent rollback plan review:
python deployments/dev-endpoint/persist_endpoint.py --remove \
  --apply --expect-fingerprint <fresh-reviewed-rollback-sha256>
```

Any secret/image/identity/network drift blocks rollback as it blocks apply.
Never dump secrets to prepare rollback. Revision IDs may change while the
image/digest, Single/100%-latest policy and other configuration remain equal.

## References and offline validation

- [Container Apps 2025-01-01 Bicep contract](https://learn.microsoft.com/azure/templates/microsoft.app/2025-01-01/containerapps)
- [Authoritative PUT/GET specification](https://github.com/Azure/azure-rest-api-specs/blob/main/specification/app/resource-manager/Microsoft.App/ContainerApps/stable/2025-01-01/ContainerApps.json)
- [Shared definitions: Secret, Template, read-only ephemeralStorage](https://github.com/Azure/azure-rest-api-specs/blob/main/specification/app/resource-manager/Microsoft.App/ContainerApps/stable/2025-01-01/CommonDefinitions.json)

The source specifies `PUT` for create-or-update and `PATCH` as a separate
operation; native Bicep emits the former. An `existing` declaration is only a
reference, not a property patch.

```bash
python -m pytest -q tests/test_dev_endpoint_persistence.py tests/test_deployment_config.py
az bicep build --file deployments/dev-endpoint/infra/main.bicep --stdout >/dev/null
```

Tests are credential-free transforms and mocked gates, **not live apply proof**.
They cover malformed/extra/duplicate env, sidecar/init preservation, multi-MI
maps, secret refs, unknown fields, native-secret refusal, full-plan rejection,
root-hook absence, concurrency, endpoint ownership, rollback, and literal
projection equivalence. No Azure resources or hosted runtimes were mutated and
no sessions or model invocations were created for this work.

Validation completed: **59 tests passed** (36 new endpoint-gate tests plus 23
existing deployment-config tests), targeted Ruff passed, Bicep **0.46.1**
compiled successfully. One pre-existing Starlette/httpx deprecation warning
was emitted; no dependency installation or upgrade was performed.
