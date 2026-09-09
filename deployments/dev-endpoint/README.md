# Dev endpoint persistence — guarded scope-only deployment

Refs #109; PR #121 merged at `0cf7acc`. This reusable dev-only leaf deploys
the canonical Foundry project endpoint without root provisioning hooks,
image changes, credential rotation, or model/hosted-compute calls.

## Verified persistence outcome, 2026-09-09

Samwise's actual independent whole-PR `code-review` invocation returned:
**APPROVE execution under gates** and **No significant issues found in the
reviewed changes**, for executable SHA
`0f9e33a7ec77a5db9b7cdb5cdc972c354ea820bd`. The reviewer independently reran
145 targeted tests, Ruff, and compiled-contract verification. The
[review record](https://github.com/bmoussaud/fantasy-cards-generator/pull/122#issuecomment-5596864920)
preceded the authorized application.

The helper then repeated the exact scope preview, real true/false diagnostics,
successful resource-bearing ARM validation, project verification, and unchanged
fingerprint checks. **One application deployment succeeded.** Both parent
`dev-endpoint-ea77f0bf2596` and child `dev-endpoint-app` report `Succeeded`.
Their full IDs share this prefix:
`/subscriptions/b8ff3e15-7e2d-4fac-a773-992fb59ccedd/resourceGroups/rg-fcag-dev/providers/Microsoft.Resources/deployments/`.

The new revision `fcag-dev-app--endpoint-ea77f0bf2596` is **Healthy / Running**,
latest equals latest-ready, and `/healthz` returned **HTTP 200**. The exact
web endpoint is now persistently configured:
`https://aifcagdevqhg3qc4rlbt4g.services.ai.azure.com/api/projects/fantasy-cards-dev`.

All reviewed writable metadata matched the expected endpoint-and-suffix-only
transform, including other env/secret references, both identity types and
identity map, ingress, scale, and Single/100%-latest traffic. The system
principal matches. Old/new revision container image strings match exactly:
web remains
`fcagdevqhg3qc4rlbt4gacr.azurecr.io/fantasy-cards-generator/web-nat-dev:azd-deploy-1788775195`.
This is the existing tagged image, not a newly built image or a claim of
independently inspected runtime digest bytes. Native secret values passed
through the guarded Azure boundary and were never operator-read.

Post-application GET fingerprint:
`bebc01d94367133127acbccb2a0913f796cf4ee441306692665f4d3b70d7ce86`.
Resource writes: one existing Container App PUT through the child, plus ARM
deployment records (including the two resource-free guard records). No other
infrastructure was targeted. No rollback was needed. **The separately
authorized live E2E remains pending; no hosted compute or model call ran here.**

## Secure deployment boundary

`infra/main.bicep` owns **only** the fixed nested deployment `dev-endpoint-app`.
The operator supplies a `secureObject` snapshot containing validated GET
configuration and secret-reference metadata, never native secret values.
The parent evaluates `listSecrets` for the existing `fcag-dev-app` **in Azure**.
It validates identical unique inventories, exactly one matching
name/reference/classification per entry, known fields, and native string values.
Key Vault references remain metadata only; native values pass through unchanged.

The guarded result is the **entire resolved snapshot**, passed to
`infra/app.bicep` through its explicit `@secure()` object parameter. A mismatch
selects the existing invalid-JSON constant before child deployment submission.
Bicep compiles the module input to an `if` returning the parameter's `value`
object; the guard does not become a post-deployment output check.

The child owns only the fixed existing app PUT and reads its location, tags,
identity, and properties directly from that secure parameter. There are no
child lookups, self-dependencies, variables, outputs, or additional resources.
Compiled `expressionEvaluationOptions.scope` is **inner**. Both parameter
declarations are `secureObject` without defaults. ARM redacts secure parameters
from deployment history; resolved native values are not embedded in templates
or sent to the operator. Parent debug is explicitly `none`, child debug is
absent (disabled). Never enable deployment-operation request/response capture.

The helper pins the entire executable compiled contract by SHA-256 and also
checks this graph structurally. Changing the pin requires independent review.
Tests reject insecure parameter types/defaults, linked templates, outer scope,
debug capture, outputs, unguarded parameter propagation, altered targets,
self-dependencies, and additional infrastructure.

## Real Azure evidence before application, 2026-09-09

The revised graph passed actual resource-bearing ARM `/validate` with
`provisioningState: Succeeded`: **no circular dependency**. Its exact-app
`ResourceIdOnly` preview returned `Deploy` plus 40 `Ignore`, no module record
in the preview, and no payloads or diagnostics. Real resource-free guard
deployments returned strict **true** for the current inventory and **false**
for a deliberately duplicated inventory. No app write occurred during these gates.

The full GET fingerprint remained
`ea77f0bf259648a25fd2171f61ab2354c1989f9301ae6d2984c187cf15e0b3de`.
This records the pre-application baseline. The independently approved
application and resulting new fingerprint are recorded above.

Diagnostic deployment names in `rg-fcag-dev`:
`dev-endpoint-guard-valid-ea77f0bf2596` and
`dev-endpoint-guard-invalid-ea77f0bf2596`, both `Succeeded`, zero resources.

### Historical failure and root cause

At `836dc8c7365734b04283b219ea5092b94c3ac104`, Samwise approved execution under
gates. The authorized direct-resource attempt failed `InvalidTemplate`
(circular dependency) before any app write, even though the real guard booleans
and scope preview passed. Its full app fingerprint remained unchanged, endpoint
absent, revision `fcag-dev-app--azd-1788775203`, `/healthz` HTTP 200.
No rollback or model call followed.

The resource-ID lookup lacking an explicit compiled `dependsOn` did **not**
prevent ARM discovering a self-listing dependency at the same deployment scope.
The parent/child boundary removes that graph relationship instead of weakening
the guard. The revised graph's real validation above confirms the distinction.

## Operator path and proof boundaries

```bash
# Read-only scope preview.
python deployments/dev-endpoint/persist_endpoint.py
# Resource-free guard records plus non-mutating resource-bearing ARM validation.
python deployments/dev-endpoint/persist_endpoint.py --validate-guard
# Only after independent execution approval, using its fresh baseline:
python deployments/dev-endpoint/persist_endpoint.py --apply --expect-fingerprint <sha256>
```

Preview transmits the compiled template through stdin with **metadata only**
as a secure parent parameter default; ARM-looking literal strings are escaped.
The child has no default. Neither runtime values nor resolved secret arrays
enter local files, model context, CLI output, or template defaults.
`ResourceIdOnly` is mandatory: no full-payload what-if or raw errors.
Only fixed allowlisted error classifications can be printed.

Cloud preview proves scope **only**: `Deploy` and `Modify` are accepted for
the exact existing app. An optional active deployment record is allowed only
at the fixed `Microsoft.Resources/deployments/dev-endpoint-app` ID, never any
other infrastructure. A module-only preview fails closed.

The local transform checks that only `web.FOUNDRY_PROJECT_ENDPOINT` and the
revision suffix change. Images/digests, other env and secret references,
sidecars/init containers, identity maps, ingress, traffic, scale, mounts,
volumes, and other reviewed writable metadata must remain equal. Unknown
GET fields fail closed. Native values are preserved by the pinned guarded
Azure-only pass-through, **not** operator-read byte comparison.

Guard diagnostics derive the exact parent module-input predicate, retaining
variables and secure metadata parameters but replacing all resources with
one boolean output. Apply repeats both diagnostics, the actual resource-graph
validation, project verification, and fresh fingerprint checks before its
single deployment PUT. API 2025-01-01 supplies no ETag: fingerprint checks
are optimistic, not atomic. Serialize the operator window. Concurrent changes
between GET/list/PUT remain a residual risk.

Post-apply checks require deployment success, exact writable metadata equality,
the same system principal, a healthy new latest/ready revision, and `/healthz`
HTTP 200 within a bounded window. Failure is not rollback: inspect actual state,
never blindly retry. Endpoint-only rollback uses `--remove` with a **fresh**
snapshot, preview, validation, guard proof, and reviewed fingerprint—not a
stale full configuration or previous traffic policy.

## Validation

```bash
python -m pytest -q tests/test_dev_endpoint_persistence.py tests/test_deployment_config.py
python -m ruff check deployments/dev-endpoint/persist_endpoint.py tests/test_dev_endpoint_persistence.py
python -m black --check deployments/dev-endpoint/persist_endpoint.py tests/test_dev_endpoint_persistence.py
az bicep build --file deployments/dev-endpoint/infra/main.bicep --stdout >/dev/null
```

Bicep 0.46.1 compiles. The concat linter recommendation is non-blocking.
Synthetic tests interpret the actual compiled module argument and prove full
snapshot/native-value/Key Vault metadata preservation and failure before child
submission. This offline interpreter is not a substitute for real ARM validation.

## Authoritative references

- [Secure nested parameters and inner evaluation](https://learn.microsoft.com/azure/azure-resource-manager/templates/linked-templates#expression-evaluation-scope-in-nested-templates)
- [What-if change types and result formats](https://learn.microsoft.com/azure/azure-resource-manager/templates/deploy-what-if#change-types)
- [REST what-if Deploy definition](https://learn.microsoft.com/rest/api/resources/deployments/what-if?view=rest-resources-2025-04-01#changetype)
- [Resource-ID list functions and dependencies](https://learn.microsoft.com/azure/azure-resource-manager/templates/resource-dependency#reference-and-list-functions)
- [Container Apps List Secrets 2025-01-01](https://learn.microsoft.com/rest/api/resource-manager/containerapps/container-apps/list-secrets?view=rest-resource-manager-containerapps-2025-01-01)
- [Container Apps writable contract](https://learn.microsoft.com/azure/templates/microsoft.app/2025-01-01/containerapps)
