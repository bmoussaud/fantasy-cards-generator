# Dev endpoint persistence — guarded scope-only deployment

Refs #109; PR #121 merged at `0cf7acc`. Application requires independent execution
review, a fresh fingerprint, and successful real resource-free guard diagnostics.

## Revised secret preservation contract

The old name-only PUT failed `ContainerAppSecretInvalid`; omitting native
secrets planned deletion. The revised authorization permits **Azure-side**
`listSecrets(resourceId(...), '2025-01-01').value`, never an operator/CLI secret
lookup. This is a secret read inside Azure, not a claim that no secret is read.
Native values pass directly from the existing app back to that same app.
Key Vault metadata passes unchanged, without resolved values being added.
No new role, identity, network, capacity, root provisioning hook or secret
rotation is introduced.

The Bicep resource input is guarded by identical unique name inventories,
exactly one matching name/reference/classification per entry, known fields,
and native string values. Observable unknown fields fail closed. Unobservable
future provider fields cannot be promised preserved. A mismatched inventory
selects a deliberately invalid JSON expression containing only a constant
marker and a zero-length resource-group-ID slice. It is evaluated in
`resource.properties`, **before the app PUT**, never in a deployment output.
The literal resource-ID lookup does not add a self-dependency. There are no
outputs in the app template, deployment scripts, native-secret parameters, or
local secret-value files.

The helper pins the entire compiled executable ARM contract by SHA-256,
excluding only compiler provenance, schema URL and content version. A compiler
or expression change that changes that contract requires review and a new pin.
The snapshot remains a `secureObject` of GET configuration/secret-reference
metadata; no native credential value is reconstructed locally.

## Actual read-only evidence, 2026-09-08

Target: existing `fcag-dev-app` in `rg-fcag-dev`, subscription
`b8ff3e15-7e2d-4fac-a773-992fb59ccedd`.

**Current corrected-gate evidence:** the real preview returned the exact app
`Deploy` plus 40 `Ignore`, without payloads or diagnostics. Azure CLI also
includes documented optional `deploymentId`, `identifiers`, and `symbolicName`
fields set to null; these are accepted only when null. Both resource-free ARM
diagnostic deployments succeeded: valid inventory **true**, synthetic duplicate
inventory **false**. The full baseline fingerprint below remained identical
before and after each diagnostic. Azure-side list evaluation is now empirically
confirmed without returning secrets to the operator or issuing an app PUT.

The following paragraphs retain the historical failed-gate evidence:

Two `ResourceIdOnly` what-if calls returned `Succeeded`: **one existing target
`Deploy`, 40 unrelated `Ignore` resources**. They did not return `Modify`.
The helper therefore stopped before application. This is not an unevaluated
full-payload comparison masquerading as a clean diff.

This was an incorrect historical gate, now corrected under explicit scope-only
authorization: Microsoft's REST `Deploy` definition says the resource exists
in current and desired state and will be redeployed; its properties may or may
not change. The helper accepts `Deploy` or `Modify` only for the exact existing
app, returns the actual classification, and rejects all other active changes,
payloads and diagnostics. **Neither accepted classification proves a cloud
property diff in a `ResourceIdOnly` response.**

A third read-only attempt submitted deliberately mismatched secret metadata
to deployment validation. The CLI exited unsuccessfully, but neither an
allowlisted ARM error code nor the guard's constant marker was observed.
**This does not prove Azure evaluated the guard.** No further attempts or
deployment writes followed. The current permission to perform Azure-side
list evaluation has not been confirmed by this inconclusive validation.

Final GET matched the original fingerprint exactly:
`ea77f0bf259648a25fd2171f61ab2354c1989f9301ae6d2984c187cf15e0b3de`.
App remained healthy (`Succeeded`/`Running`, latest == latest-ready), endpoint
absent, original principal `946d8701-48f2-4fa5-8efd-bf053c7b4e4c`. This is
blocked-baseline evidence, **not an approved application fingerprint**.

### Independent review

Samwise's actual read-only `code-review` task inspected the whole PR against
`origin/main` at `4f4293a307ad8deba003f4b9cd21b890a2263ea8` and returned:
**APPROVE — code only; no significant issues found.** This is explicitly **not
approval to apply**: that historical review preceded the corrected scope gate
and resource-free diagnostic implementation. New independent review is required.

### Resource-free Azure guard proof

`--validate-guard` derives two diagnostics from the verified compiled app
template. Bicep inlines runtime expressions, so the helper extracts the exact
first argument of the resource-input `if`, preserves parameters and variables,
removes **all resources**, and permits only one boolean `inventoryValid` output.
The snapshot remains `secureObject`. A current metadata snapshot must return
`true`; a deliberately duplicated metadata name must return `false`. Neither
diagnostic can issue a Container App PUT. Only deployment records are created.
The original invalid-JSON branch remains in the actual app resource input.

Azure evaluates `listSecrets` internally; no values, names, arrays, raw errors,
or arbitrary outputs are exported. Strict boolean checks and unchanged baseline
fingerprints are required around each diagnostic. `--apply` repeats these
checks before its final fresh GET and app deployment. These runtime booleans
plus the pinned input guard prove the guard path without an unsafe trial PUT.

## Proof boundaries and operator path

```bash
# Read-only scope preview.
python deployments/dev-endpoint/persist_endpoint.py
# Writes resource-free deployment records, never application resources.
python deployments/dev-endpoint/persist_endpoint.py --validate-guard
```

Preview transmits the compiled template through stdin with metadata as a
secure parameter default. ARM-looking literal metadata strings are escaped.
Runtime secret expressions remain unevaluated locally. Preview requests
`ResourceIdOnly` exclusively: no raw diagnostics or full-payload preview,
expanded template, deployment-operation request bodies, or debug logging.

The cloud plan proves scope only. Exact local metadata comparison independently
checks that the transform changes only `web.FOUNDRY_PROJECT_ENDPOINT` and the
revision suffix. Pinned compiled expressions provide the Azure pass-through
shape, not empirical operator verification of native value equality.
Images/digests, other env and secret references, sidecars/init containers,
identity maps, configuration, traffic, ingress, scale, mounts and volumes must
remain equal. Unknown GET fields fail closed.

Once independent review and all gates are genuinely satisfied,
the existing `--apply --expect-fingerprint <reviewed-sha256>` path uses the
secure compiled Bicep deployment, never root `azd` hooks. It checks a fresh GET
after preview and immediately before application. API 2025-01-01 returned no
ETag; there is no supported If-Match serialization in this helper. These are
optimistic checks, **not atomicity**. Serialize the operator window. Concurrent
config or native-secret changes between GET/list/PUT remain a residual risk.
Drift requires replanning, not forcing.

Post-apply checks require exact writable metadata equality, unchanged system
principal, healthy new latest revision, and HTTP 200 `/healthz` within a bounded
window. No `/generate`, model call or feature switch is used. Failure does not
prove rollback and must not trigger blind reapplication.

Endpoint-only rollback uses `--remove` with a **new current snapshot, fresh
preview, and reviewed fingerprint**, not stale full configuration or old
traffic. The same secret and scope gates apply. No rollback was needed here.

## Validation

```bash
python -m pytest -q tests/test_dev_endpoint_persistence.py tests/test_deployment_config.py
python -m ruff check deployments/dev-endpoint/persist_endpoint.py tests/test_dev_endpoint_persistence.py
python -m black --check deployments/dev-endpoint/persist_endpoint.py tests/test_dev_endpoint_persistence.py
az bicep build --file deployments/dev-endpoint/infra/main.bicep --stdout >/dev/null
```

Bicep 0.46.1 compiles. The symbol-reference
linter recommendation is deliberately not followed because it would create
a self-dependency. The concat recommendation does not affect correctness.
Tests interpret the actual compiled expression against synthetic native/KV
fixtures and malformed inventories, assert guard failure before resource-input
construction, and reject hidden outputs/dependencies/extra resources. That
small offline interpreter is **not a substitute for actual Azure guard proof**.

## Authoritative references

- [What-if change types and result formats](https://learn.microsoft.com/azure/azure-resource-manager/templates/deploy-what-if#change-types)
- [REST what-if Deploy definition](https://learn.microsoft.com/rest/api/resources/deployments/what-if?view=rest-resources-2025-04-01#changetype)
- [Resource-ID list functions and dependencies](https://learn.microsoft.com/azure/azure-resource-manager/templates/resource-dependency#reference-and-list-functions)
- [Container Apps List Secrets 2025-01-01](https://learn.microsoft.com/rest/api/resource-manager/containerapps/container-apps/list-secrets?view=rest-resource-manager-containerapps-2025-01-01)
- [Container Apps writable contract](https://learn.microsoft.com/azure/templates/microsoft.app/2025-01-01/containerapps)
