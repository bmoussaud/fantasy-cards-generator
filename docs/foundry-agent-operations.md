# Card-orchestrator operations

## Dev model-capacity correction and successful ACA-MI E2E — 2026-09-09

Safe management-plane inspection after the classified HTTP 429 found the exact
dev deployment `gpt-5-5` healthy but allocated only **1 capacity unit**:
`GlobalStandard`, `gpt-5.5` `2026-04-24`, `Microsoft.DefaultV2`,
`OnceNewDefaultVersionAvailable`, with reported limits of **1 RPM** and **1,000
TPM**. The East US 2 regional quota record reports **1 of 1,000 capacity units
allocated** for `OpenAI.GlobalStandard.gpt-5.5`; this is deployment allocation,
not actual token consumption. The account has only this text deployment and the
existing `gpt-image-2` deployment.

One orchestration performs three sequential model requests inside 65 seconds,
each configured with `max_output_tokens=1800`. Repository field bounds produce
7,762 bounded input characters across the three request payloads, instructions,
and duplicated strict schemas before framework overhead. The output reservation
alone is 5,400 tokens. Azure documents that throttling admission estimates
prompt characters plus the configured maximum output tokens, and RPM expects
requests to be evenly distributed. Therefore 1 RPM/1K TPM is structurally
insufficient; this is discriminating evidence for capacity correction rather
than another identical E2E.

The proposed dev allocation is **10 units = 10 RPM / 10K TPM**. Ten is the live
catalog default for this exact model/SKU, permits the three sequential calls,
and leaves roughly 20% headroom over an approximately 8K-token bounded
reservation estimate without consuming the 1,000-unit regional allowance. Prod
remains at 1. No PTU, model, region, deployment, RAI, upgrade-policy, RBAC,
network, hosted runtime, or application change is proposed.

Source changes persist the dev default in `infra/main.bicep`, explicitly pin the
existing text deployment RAI and upgrade policies, and add the dev-only
`infra/text-model-capacity.bicep` leaf so a reviewer can update only this model
deployment without root provisioning. The leaf accepts no target parameters.
Its deployment-time `fail()` guard requires the exact dev subscription,
`rg-fcag-dev`, and ARM deployment name `dev-text-model-capacity-10`; the
Foundry account `aifcagdevqhg3qc4rlbt4g` and model deployment `gpt-5-5` are
hard-bound in the compiled resource ID. A production-like subscription,
resource group, deployment operation name, or account therefore cannot be
substituted while retaining a valid template deployment.

Bicep compilation and the deployment configuration tests passed. ARM
validation succeeded for the exact guarded dev target and failed for an
existing arbitrary resource group and for a production-like ARM deployment
name; ARM also rejected a production-like `accountName` override because the
template exposes no such parameter. The fail-closed template's live ARM
`what-if` succeeded and reported one `Modify`: `sku.capacity` from 1 to 10 on
`gpt-5-5`; its only other delta was deletion of read-only
`properties.currentCapacity` from the request shape. Every unrelated resource
was `Ignore`. A root `azd provision --preview` was not used because this clean
worktree has no local azd environment and the dedicated leaf intentionally
bypasses root provisioning; the management-plane validation and `what-if` are
the applicable real-ARM gates. The reviewed apply sequence is:

```bash
az deployment group validate \
  --name dev-text-model-capacity-10 \
  --resource-group rg-fcag-dev \
  --template-file infra/text-model-capacity.bicep

az deployment group what-if \
  --name dev-text-model-capacity-10 \
  --resource-group rg-fcag-dev \
  --template-file infra/text-model-capacity.bicep

# REVIEW GATE: do not run before approval of the what-if.
az deployment group create \
  --name dev-text-model-capacity-10 \
  --resource-group rg-fcag-dev \
  --template-file infra/text-model-capacity.bicep
```

The existing Application Insights component showed no 429 dependency row and no
exception row during `08:45:30Z`–`08:46:20Z`; its safe aggregate counters were
43 successful result-code-0 dependencies, 22 successful HTTP-200 dependencies,
and 7 successful HTTP-200 requests. This does not contradict the typed hosted
runtime failure because that provider call is not exported as an App Insights
429 dependency.

After Gandalf approved revision
`669899d23701983fc0a540ad93d932fee688577e` and Samwise independently approved
its application, Gimli pushed that exact accepted revision without force and
repeated the live gates. ARM validation succeeded. The fresh full-payload
what-if contained one `Modify`, on the exact `gpt-5-5` resource: only
`sku.capacity` changed from 1 to 10, plus omission of the read-only
`properties.currentCapacity`; model, version, SKU, RAI policy and upgrade policy
were identical before and after. Every other resource was `Ignore`. Regional
quota was 1 of 1,000 units before apply.

The documented leaf deployment ran once. ARM deployment
`dev-text-model-capacity-10` completed `Succeeded` at
`2026-09-09T09:17:49.389075Z`. The effective deployment then reported capacity
10, 10 RPM and 10,000 TPM, with regional allocation 10 of 1,000 units and the
same `GlobalStandard`, `gpt-5.5` `2026-04-24`, `Microsoft.DefaultV2` and
`OnceNewDefaultVersionAvailable` properties.

The corrected actual ACA-managed-identity E2E then built and deployed exact
source `669899d23701983fc0a540ad93d932fee688577e` as
`card-orchestrator:1`, image digest
`sha256:fd18e117ec58c3bcee3b692d464e41aae92bea0b1b5300424832b937e268761a`.
Deployment was submitted at `09:20:25.680407Z`; the image was published at
`09:20:39.5991604Z`, and the hosted version became active within the deploy
command's reported 58 seconds. The version used 0.5 CPU / 1 GiB and exposed the
expected application SHA and existing `gpt-5-5` deployment.

GET-only preparation against revision
`fcag-dev-app--endpoint-ea77f0bf2596`, replica
`fcag-dev-app--endpoint-ea77f0bf2596-5f75998d86-nwvzw`, container `web`
confirmed the persisted project endpoint, actual ACA system identity
`946d8701-48f2-4fa5-8efd-bf053c7b4e4c`, project access, parser, request schema
and local fixture, with zero invocation attempts. The unique pre-recorded
session `smoke-109-dc546e69831d47fa8dcbc217b98765c5` returned not found before
the invocation.

One ACA-MI probe was submitted at `09:22:17.960284Z`. The same credential
created and readied its own exact version-pinned session, then sent one Responses
POST with zero retries. HTTP 200 carried a completed, schema-valid card result,
not a held result or refusal. Both hosted and application versions matched, so
the prior `art_direction/rate_limited/rate_limit/http_429` failure is resolved
for this bounded run. The result intentionally contains no card text:

```json
{"accessVerified":true,"applicationVersion":"669899d23701983fc0a540ad93d932fee688577e","applicationVersionMatched":true,"endpointPersisted":true,"endpointSource":"aca_environment","hostedVersion":"1","hostedVersionMatched":true,"httpStatus":200,"invocationVerified":true,"invocationsAttempted":1,"outcome":"completed","phase":"invoke","principalMatched":true,"responseId":"caresp_0c3111ffd0c24c5300Da3S4fgS2WviBtzGZpmFkhFtz2p9NCee","schemaValid":true,"sessionCleanupRequired":true,"sessionCreateAttempted":true,"sessionCreated":true,"sessionReady":true,"status":"invocation_verified","tokenAcquired":true}
```

Cleanup began at `09:23:04.234Z`. Ownership was rechecked against version `1`;
the session stopped at `09:23:08.346Z`, was deleted at `09:23:10.525Z`, and the
exact hosted version was deleted at `09:23:13.218Z`. Exact session lookup
returned not found, agent-version resolution failed after deletion, and the
session-list endpoint returned HTTP 404. The serving ACA image, revision,
system identity, Single/100%-latest traffic and persisted endpoint were
unchanged; its actual FQDN `/healthz` returned HTTP 200. Capacity 10 is an
intentional persistent dev change and was not rolled back. No local probe,
deployment or azd process remained.

Cloud-write count was one targeted ARM model-deployment update, one agent image
publication, one hosted version, one ACA-owned session create and one Responses
POST, followed by exact session/version cleanup. Provider request count and
token usage were not exported, so they remain unobserved rather than being
invented from the three-stage maximum. No raw prompt, response/card body, token,
secret or `.env` value was read or recorded. No production, application,
traffic, identity, RBAC, network, root-provisioning, evaluation, PTU or unrelated
resource change occurred.

## Exact classified runtime failure — 2026-09-09

Working as Gimli (DevOps / Infra), exact Samwise-approved source
`22aa53bc5678437cf6b4e8507220b7ed36e04ead` was deployed once to the existing
dev Foundry project. No production, web-app, RBAC, network, secret, model,
provisioning or evaluation change was made.

**Exact failure cause:** the hosted runtime reached the `art_direction`
specialist, where its provider request was rate-limited with HTTP 429. The
closed diagnostic tuple was
`art_direction/rate_limited/rate_limit/http_429`. This is not an authorization,
configuration, routing or schema diagnosis, and it does not justify an RBAC
change or an unchanged retry. The Responses endpoint returned HTTP 200 carrying
a genuine failed envelope; no schema-valid card, held result or refusal was
returned.

| Evidence | Observed result (2026-09-09 UTC) |
| --- | --- |
| Exact source / image tag | `22aa53bc5678437cf6b4e8507220b7ed36e04ead` |
| Image | `fcagdevqhg3qc4rlbt4gacr.azurecr.io/card-orchestrator:22aa53bc5678437cf6b4e8507220b7ed36e04ead` |
| ACR digest / created | `sha256:12919b30280b6157672618ef2cdde8f423169c8256eef0a168aa5eb8ee95e67f` / `08:44:31.286686Z` |
| Fresh preparation | `08:42:56.939Z`–`08:43:11.825Z`; expected ACA system MI and persisted endpoint matched; agents GET HTTP 200; parser/request/local-fixture gates passed; zero POSTs |
| Pre-recorded session | `smoke-109-a863247758484454bdb0ecc2094db656`; exact operator GET returned 404 before deployment |
| Deployment | `08:44:15.603Z`–`08:45:17.755Z`, **62.118 monotonic seconds**; 0.5 CPU / 1 GiB |
| Owned hosted version | `card-orchestrator:1`, active, created `08:44:43Z`; exact application SHA and tagged image verified |
| ACA target | Revision `fcag-dev-app--endpoint-ea77f0bf2596`, replica `fcag-dev-app--endpoint-ea77f0bf2596-5f75998d86-nwvzw`, container `web` |
| Invocation | `08:45:41.723Z`–`08:46:11.952Z`, **30.178 monotonic seconds**; one session create and one Responses POST, zero retries |
| Result | HTTP 200 failed envelope; `art_direction/rate_limited/rate_limit/http_429`; schema/version verification false |
| Cleanup | Ownership rechecked against exact session/version; stop, session delete and exact version delete completed `08:46:24.622Z`–`08:46:42.555Z` in **17.903 monotonic seconds** |
| Absence verification | Exact session GET 404, exact version GET 404 and session-list endpoint `not_found` |
| Final web verification | Same image, revision, system MI, Single/100%-latest traffic and persisted endpoint; `/healthz` HTTP 200 at `08:47:04.445Z` |
| Hosted lifetime through final health | **168.842 UTC seconds**, below the 30-minute limit; no local probe, azd or exec process remained |

Exact sanitized result marker:

```json
{"accessVerified":true,"applicationVersion":"22aa53bc5678437cf6b4e8507220b7ed36e04ead","endpointPersisted":true,"endpointSource":"aca_environment","hostedVersion":"1","httpStatus":200,"invocationVerified":false,"invocationsAttempted":1,"outcome":"failed","phase":"invoke","principalMatched":true,"responseId":"caresp_0d880e91d9b2b29d00kxt80Hq5RyFuEK7yDD4FSjWCr1XGZX4N","runtimeHttpStatus":"http_429","runtimeHttpType":"rate_limit","runtimeReason":"rate_limited","runtimeStage":"art_direction","schemaValid":false,"sessionCleanupRequired":true,"sessionCreateAttempted":true,"sessionCreated":true,"sessionReady":true,"status":"failed","tokenAcquired":true}
```

The existing `gpt-5-5` deployment was independently healthy immediately before
the run (`Succeeded`, `gpt-5.5` version `2026-04-24`, `GlobalStandard`). The
typed runtime result supplies the missing causal evidence: provider throttling
occurred at the third orchestration stage. Exact provider token usage and
service-side quota counters were not exported. No retry was sent because the
request contract forbids POST retries and no changed hypothesis exists.

## Payload-free runtime classification candidate — 2026-09-09

Working as Aragorn (Backend Dev), the successful strict-routing live result at
`dc6b83161a62f66b93e2a487f2b5d16e91bfd2b0` proves that the prior HTTP 400
`agent_reference` failure is resolved. The remaining HTTP 200
`outcome:"failed"` is a genuine Responses failure envelope, not a valid card
domain envelope and not a schema failure that may be treated as success.

Safe read-only configuration evidence narrows, but does not identify, the
downstream cause:

- the dedicated azd environment resolves `AZURE_AI_MODEL_DEPLOYMENT_NAME` to
  `gpt-5-5`;
- the account exposes deployment `gpt-5-5`, model `gpt-5.5`, version
  `2026-04-24`, `GlobalStandard`, provisioning state `Succeeded`;
- the project system identity has the repository's expected Foundry User
  assignment at account scope.

These facts rule out an absent configured alias and an absent model deployment.
They do **not** prove the dedicated per-agent runtime identity was authorized,
nor distinguish credential failure, provider request rejection, timeout,
transport failure or provider response failure. The deleted hosted version
cannot supply further identity metadata. No paid call was made for this code
cycle.

The exact observability defect is in the existing exception path. Pinned Agent
Framework `1.17.0` wraps provider exceptions as `ChatClientException` while
preserving the provider exception as its cause. `CardOrchestrator.generate()`
then caught every exception and replaced that chain with the single
`RuntimeFailure("dependency_failure")`; the response host consequently emitted
only `server_error`. Provider HTTP type and status therefore existed in-process
but were intentionally erased before the ACA probe could observe them.

The candidate preserves the failed Responses envelope and the unchanged card
domain schema, but carries only four closed enums in its error code:

- `runtimeStage`: `specialist_setup|concept|lore|art_direction|orchestration`
- `runtimeReason`: `timeout|authentication|authorization|resource_not_found|`
  `invalid_request|rate_limited|service_error|transport_error|`
  `invalid_response|dependency_error`
- `runtimeHttpType`: `none|bad_request|authentication|permission_denied|`
  `not_found|conflict|unprocessable|rate_limit|server|api_status`
- `runtimeHttpStatus`: `none|http_400|http_401|http_403|http_404|http_408|`
  `http_409|http_422|http_429|http_500|http_502|http_503|http_504|http_other`

The host code is assembled only from those enums. The application parser
validates the tuple, and the ACA marker revalidates every field before export.
Exception messages, response bodies, URLs, headers, request/model text, tokens
and stack traces never cross the boundary. Unknown values cause the diagnostic
to be discarded, not echoed.

Expected Gimli handoff after review: deploy the exact candidate once and run the
existing single bounded ACA-MI invocation. A marker such as
`concept/authorization/permission_denied/http_403` proves a model-call
authorization failure; `concept/resource_not_found/not_found/http_404` proves
the provider endpoint could not resolve the configured resource;
`concept/invalid_request/bad_request/http_400` proves model-contract rejection.
`specialist_setup/*/none/none` identifies pre-call client or credential setup,
while `concept/timeout/none/none` identifies the first bounded model stage.
Do not broaden roles or replace failure with a success fallback before that
typed result exists. Budgets remain three stages, 20 seconds and 1800 output
tokens per stage, 65 seconds overall, managed identity, `store:false`, and no
retry.

## Exact-source runtime failure — 2026-09-09

Working as Gimli (DevOps / Infra), exact reviewed source
`d2de0b358a9d665a2d63d5b5fb74119b4a77edec` was deployed once to the existing
dev Foundry project. This is the latest live E2E result and supersedes the prior
failure as the current status; the older attempts below remain historical facts.
No production, web-app, RBAC, network, secret, model-capacity, provisioning or
evaluation change was made.

The corrected strict boundary accepted the platform `agent_reference`: the
ACA-managed-identity request passed session creation/readiness and returned HTTP
200 from the Responses endpoint. The response was nevertheless a failed
envelope, not a schema-valid domain result:
`outcome:"failed"`, `schemaValid:false`, `invocationVerified:false`. No card,
held result or refusal was returned, and application/hosted response-version
matching therefore could not be established. This proves the prior
`unsupported_field/agent_reference` boundary is fixed live, but it does not
prove successful model orchestration.

| Evidence | Observed result (2026-09-09 UTC) |
| --- | --- |
| Exact source / image tag | `d2de0b358a9d665a2d63d5b5fb74119b4a77edec` |
| Image | `fcagdevqhg3qc4rlbt4gacr.azurecr.io/card-orchestrator:d2de0b358a9d665a2d63d5b5fb74119b4a77edec` |
| ACR digest / created | `sha256:28031d04a6cfcefe54efdee3cab99bf32a0b68097097afdd966a0aa57b8bb767` / `08:25:21.7308694Z` |
| Preparation | Expected ACA system MI and persisted endpoint matched; agents GET HTTP 200; parser/request/local-fixture gates passed; zero POSTs |
| Deployment | `08:25:03.527Z`–`08:26:08.294Z`, **64.677 monotonic seconds**; 0.5 CPU / 1 GiB |
| Owned hosted version | `card-orchestrator:1`, active; exact image and application SHA verified |
| Pre-recorded session | `smoke-109-7bd0b9c6bf73420584f32c19c09e15be`; exact GET 404 before deployment |
| ACA target | Revision `fcag-dev-app--endpoint-ea77f0bf2596`, replica `fcag-dev-app--endpoint-ea77f0bf2596-5f75998d86-nwvzw`, container `web` |
| Invocation | `08:26:31.624Z`–`08:27:02.446Z`, **30.780 monotonic seconds**; one session create and one Responses POST, zero retries |
| Result | HTTP 200; failed envelope; no valid card, held result or refusal; schema/version verification false |
| Run-scoped diagnostics | Console/system logs contained none of the allowlisted auth, timeout, quota, runtime-failure, model-not-found, HTTP 4xx or HTTP 5xx classes; the response-ID-filtered App Insights query returned no rows |
| Cleanup | Session stop/delete and exact version deletion completed `08:28:07.623Z`–`08:28:18.584Z` |
| Absence verification | Exact session GET 404 and exact version GET 404 |
| Final web verification | Same image, revision, system MI, Single/100%-latest traffic and persisted endpoint; `/healthz` HTTP 200 at `08:28:38.492Z` |
| Hosted lifetime through final health | **214.965 UTC seconds**, below the 30-minute limit; no local probe/azd/exec process remained |

Exact sanitized result marker:

```json
{"accessVerified":true,"applicationVersion":"d2de0b358a9d665a2d63d5b5fb74119b4a77edec","endpointPersisted":true,"endpointSource":"aca_environment","hostedVersion":"1","httpStatus":200,"invocationVerified":false,"invocationsAttempted":1,"outcome":"failed","phase":"invoke","principalMatched":true,"responseId":"caresp_011c8c59d7b18d60001mM36LHXR7pW4rpMhTyL0gnsTJ8C2o3z","schemaValid":false,"sessionCleanupRequired":true,"sessionCreateAttempted":true,"sessionCreated":true,"sessionReady":true,"status":"failed","tokenAcquired":true}
```

The runtime intentionally converts every dependency exception into the same
payload-free failed envelope. The bounded diagnostics exposed no safe narrower
cause, so an unchanged paid retry would not add evidence and was not sent.
The next backend step is to add a reviewed, fixed-enum runtime failure
classification at the dependency boundary (without exception text, model/user
content or raw bodies), then use another bounded dev E2E to distinguish model
authorization, timeout and model-response/schema failure. Do not broaden roles
from this result.

## Exact strict-boundary rejection — 2026-09-09

Working as Gimli (DevOps / Infra), source
`45c03cbfda5a4667d36a73aee0184bcb908bc9f3` was deployed to the existing dev
Foundry project after independent review. The requester's instruction to
continue investigating #122 supersedes the historical per-attempt authorization
notes below. This run made no production, web-app, RBAC, network, model,
provisioning, secret, or evaluation change.

**Exact cause observed:** the one ACA-managed-identity Responses POST reached
the hosted application's strict boundary. That boundary returned
`card_boundary_invalid_request` with `serviceReason:"unsupported_field"` and
`serviceParam:"agent_reference"`. This is direct application-boundary evidence,
not an inference from the earlier generic `invalid_request`. It proves that the
live platform-to-container envelope included the top-level `agent_reference`
field and that `StatelessBoundary.validate_wire()` rejected that field before
SDK normalization or model orchestration. The diagnostic intentionally did not
export its value, any user/model content, raw body, headers, token, or exception.

Fresh GET-only preparation against the pinned serving ACA replica succeeded
before deployment: persisted endpoint and expected system MI matched, agents
access returned HTTP 200, parser/request/local-fixture gates passed, and zero
POSTs were sent.

| Evidence | Observed result (2026-09-09 UTC) |
| --- | --- |
| Source / image tag | `45c03cbfda5a4667d36a73aee0184bcb908bc9f3` |
| Image | `fcagdevqhg3qc4rlbt4gacr.azurecr.io/card-orchestrator:45c03cbfda5a4667d36a73aee0184bcb908bc9f3` |
| ACR digest / created | `sha256:2bb4c0b8fcad79a9a16bb056c4b45dbf25b5a0ed9c9ea325feaaeefdac08db5b` / `08:08:52.9697442Z` |
| Deployment clock start | `08:08:32Z`; 0.5 CPU / 1 GiB |
| Owned hosted version | `card-orchestrator:1`, active, created `08:09:05Z`; exact image and application SHA verified |
| Pre-recorded session | `smoke-109-3a551997b5f44314924754257d39f092`; confirmed absent before invocation |
| ACA target | Revision `fcag-dev-app--endpoint-ea77f0bf2596`, replica `fcag-dev-app--endpoint-ea77f0bf2596-5f75998d86-nwvzw`, container `web` |
| Expected ACA system MI | `946d8701-48f2-4fa5-8efd-bf053c7b4e4c`; audience `https://ai.azure.com/.default` |
| Diagnostic invocation | Submitted `08:10:03.480Z`, returned `08:10:24.246Z`; one session create and one Responses POST, zero retries |
| Exact rejection | HTTP 400, `card_boundary_invalid_request`, `unsupported_field`, `agent_reference` |
| Session evidence | Created/ready against exact version `1`; created `08:10:15Z` |
| Cleanup | Exact session stop/delete and version `1` deletion succeeded from `08:10:33.390Z` through `08:10:48.205Z` |
| Absence verification | Exact session not found; deleted agent version no longer resolves |
| Final web verification | Same image, revision, system MI, Single/100%-latest traffic and persisted endpoint; `/healthz` HTTP 200 |
| Entire hosted lifetime | **146.532 monotonic seconds**, `08:08:32Z`–`08:10:59Z`; no diagnostic process remained |

Exact sanitized marker:

```json
{"accessVerified":false,"endpointPersisted":true,"endpointSource":"aca_environment","httpStatus":400,"invocationVerified":false,"invocationsAttempted":1,"phase":"invoke","principalMatched":true,"reason":"http_error","schemaValid":false,"serviceCode":"card_boundary_invalid_request","serviceParam":"agent_reference","serviceReason":"unsupported_field","sessionCleanupRequired":true,"sessionCreateAttempted":true,"sessionCreated":true,"sessionReady":true,"status":"failed","tokenAcquired":true}
```

Evidence stops at the strict boundary: it does not prove the inner SDK or model
would accept the envelope after that field is handled, and it records no model
call or token count. Aragorn's next backend step is to verify the hosted
platform/SDK contract for `agent_reference` and add an exact-envelope regression
before proposing the smallest validated routing-only handling. Do not weaken
unrelated strict validation, persistence, history, identity, or content-safety
controls. Further dev diagnostic deployment remains authorized when a reviewed,
hypothesis-driven source is ready; do not blindly repeat this unchanged call.

**Backend correction prepared after this run:** pinned
`azure-ai-agentserver-responses==2.1.0` defines `AgentReference` as an object
with required literal `type:"agent_reference"`, required non-empty string
`name`, and optional string `version`. The SDK uses it for agent identity,
response-item stamping, telemetry, and deterministic session derivation; it is
not card-domain input. The strict boundary now accepts only those exact keys and
types, then discards the object together with `agent_session_id` while rebuilding
the existing `store:false`, `stream:false`, single-message canonical request.
Malformed references, extra reference keys, unrelated top-level fields, caller
controls, and domain-schema failures remain rejected before the SDK/model.

The regression takes the real `aca_identity_payload.invocation_body()` envelope,
adds the documented hosted `agent_reference` shape, and sends it through the
real pinned AgentServer host with fake specialists. This proves the prior local
boundary rejection is removed; it does not prove the hosted gateway's exact
value, a model call, or a successful hosted response. Gimli's post-review
deployment is still required to establish the next live boundary/result.

## Endpoint persistence follow-up gate (2026-09-09)

PR #121 is merged at `0cf7acc`. PR #122 now implements guarded **Azure-side**
secret pass-through; no operator secret-value lookup is authorized.
The earlier approved direct-resource apply failed with an ARM circular
dependency before any app write. The replacement parent/secure-child graph
passed actual resource-bearing ARM validation. Fresh `ResourceIdOnly` preview
returned the exact existing app `Deploy` and 40 `Ignore` resources; real
resource-free guard diagnostics returned true/false for valid/invalid inputs.
Samwise independently **approved execution under gates** at executable
`0f9e33a7ec77a5db9b7cdb5cdc972c354ea820bd`. The authorized helper repeated
all gates and successfully persisted the endpoint: parent/child deployments
both `Succeeded`, new revision `fcag-dev-app--endpoint-ea77f0bf2596`
Healthy/Running, `/healthz` HTTP 200, unchanged image and other writable metadata.
See the [candidate and exact diagnostic evidence](../deployments/dev-endpoint/README.md).
Do not run root provisioning, retrieve secret values into operator context, or
treat scope-only preview as cloud verification of property/value equality.
Endpoint persistence is complete. No hosted compute or model call accompanied
the persistence change itself. The separately authorized E2E subsequently ran
and reached HTTP 400 at invocation, as recorded below.

Related: #109 (hosting), #99 (operations), #117 (merged client/RBAC wiring),
#118 (read-only preflight). This package does **not** deploy or enable the web
generation path. It contains no web hooks, model deployment, new registry,
new Foundry account/project, or monitoring resource.

## Corrected-routing ACA MI E2E — 2026-09-09

Working as Gimli (DevOps / Infra), using the requester `@bmoussaud`'s fresh
bounded authorization (explicit "ok pour un nouvel essai" at
`2026-09-09T07:34:40Z`) after being informed the corrected image was undeployed
and one bounded trial remained. Source `a7a1e2f03d089eeb489ad1614383c889a32894ce`
independently reviewed (PR #122 comment). **That SHA is the deployed application
and image source; subsequent evidence commits are not the deployed image.**

Routing boundary fix: `validate_wire()` now accepts a validated `agent_session_id`
field and discards it before SDK normalization; canonical request to SDK contains
only `store`, `stream`, `input`. No re-provisioning or permission change.

**Result: deployment and same-identity session creation/readiness succeeded;
the one Responses POST returned HTTP 400 `invalid_request`. E2E domain success
remains blocked.** The code became observable because source `a7a1e2f` added
`invalid_request` to the probe allowlist; the prior source mapped that same code
to `unknown`. Therefore the changed marker does **not** establish that the
service response or reject boundary changed.

Fresh GET-only `--prepare-invocation` was NOT re-run separately for this trial;
endpoint persistence, session creation and readiness were confirmed live by the
`--invoke-once` result: `endpointPersisted:true`, `endpointSource:"aca_environment"`,
`sessionCreated:true`, `sessionReady:true`, `principalMatched:true`,
`tokenAcquired:true`, `invocationsAttempted:1`.

| Evidence | Observed result (2026-09-09 UTC) |
| --- | --- |
| Image / source SHA | `fcagdevqhg3qc4rlbt4gacr.azurecr.io/card-orchestrator:a7a1e2f03d089eeb489ad1614383c889a32894ce` |
| ACR digest | `sha256:c8d813c4d5aeaa6402f2c945cdeef9775bb629280f8365e7cbaa9cf126140aab` |
| ACR image created | `2026-09-09T07:39:25.789Z` |
| Pre-recorded session | `smoke-109-44ca46e57cd64e19a3a7e3752c49b5de`; exact GET 404 at `07:38:44.852Z` |
| Initial version inventory | Zero existing versions; no reuse |
| Deploy submission / runtime clock start | `07:39:00.228Z`; 0.5 CPU / 1 GiB, dedicated azd project, one deployment |
| Deploy return | Exit 0 at `07:40:19.194Z`, **78.977 monotonic seconds** |
| New owned version | `card-orchestrator:1`, status `active`, `created_at:2026-09-09T07:39:39Z`; version/image/application ownership verified |
| ACA target | Revision `fcag-dev-app--endpoint-ea77f0bf2596`, replica `fcag-dev-app--endpoint-ea77f0bf2596-5f75998d86-nwvzw`, container `web` |
| Expected ACA system MI | `946d8701-48f2-4fa5-8efd-bf053c7b4e4c`; audience `https://ai.azure.com/.default` |
| Single probe submission / return | `07:41:01.168Z` / `07:41:53.246Z`, exit 1, **52.087 monotonic seconds** |
| Session and inference counters | One session-create attempt, created and ready; **one Responses POST**, zero retries |
| Finally begins | `07:41:53.247Z`, **173.041 seconds** after deployment submission |
| Session stop / delete | Exit 0 at `07:42:02.894Z` / `07:42:12.065Z` |
| New version deletion | Exact version `1` only, exit 0 at `07:42:19.984Z`, **203.620 seconds** after submission |
| Independent absence verification | Exact session GET **not found** and version GET **"agent version could not be resolved"** at `07:42:33.115Z` |
| Cleanup complete | `07:42:23.823Z`, **203.620 seconds** after submission |
| Final web verification | `07:43:15.046Z`, unchanged image `web-nat-dev:azd-deploy-1788775195`, revision `fcag-dev-app--endpoint-ea77f0bf2596`, Single/100%-latest, `/healthz` HTTP 200, `FOUNDRY_PROJECT_ENDPOINT` persisted |
| Entire run | `07:44:09.387Z`, **309.195 seconds**; well below 30 minutes |

Exact sanitized probe result:

```json
{"accessVerified": false, "endpointPersisted": true, "endpointSource": "aca_environment", "httpStatus": 400, "invocationVerified": false, "invocationsAttempted": 1, "phase": "invoke", "principalMatched": true, "reason": "http_error", "schemaValid": false, "serviceCode": "invalid_request", "sessionCleanupRequired": true, "sessionCreateAttempted": true, "sessionCreated": true, "sessionReady": true, "status": "failed", "tokenAcquired": true}
```

`sessionCleanupRequired:true` describes the marker at invocation return; the subsequent operator cleanup above completed successfully. `accessVerified:false` here does not negate the successful session creation or endpoint persistence; the invocation branch only sets it after a successful Responses reply.

**Changed from prior run:** `serviceCode` is now `"invalid_request"` (was
`"unknown"` from source `aad771477c34fb4c699d2bc63e279aa2be1badd7`).
Source `aad7714` allowed only `session_not_accessible`, so it would map an
`invalid_request` body to `unknown`; source `a7a1e2f` added that code to the
allowlist. The marker change is thus a diagnostic-code change, not causal proof
that the routing fix passed the container boundary. Offline, the exact probe
body passes `validate_wire()` and the pinned SDK 2.1.0 host. Live, the gateway
may consume or transform routing fields before the container, and the retained
evidence contains no safe field-level detail. The exact reject boundary and
offending hosted input remain unproven by this run.

The next source uses a unique fixed `card_boundary_invalid_request` code plus
allowlisted reason/field enums for strict-boundary failures. If Gimli's next
otherwise-identical run returns that code, the container boundary and exact
field/category are proven. If it still returns generic `invalid_request`, the
strict application boundary did not generate the observed body; inspect hosted
system logs before changing the domain contract or permissions.

Exact probe flags, run from the repository root:

```bash
python deployments/card-orchestrator/aca_identity_probe.py \
  --environment dev \
  --subscription b8ff3e15-7e2d-4fac-a773-992fb59ccedd \
  --resource-group rg-fcag-dev --app fcag-dev-app \
  --revision fcag-dev-app--endpoint-ea77f0bf2596 \
  --replica fcag-dev-app--endpoint-ea77f0bf2596-5f75998d86-nwvzw \
  --container web \
  --project-endpoint https://aifcagdevqhg3qc4rlbt4g.services.ai.azure.com/api/projects/fantasy-cards-dev \
  --expected-principal 946d8701-48f2-4fa5-8efd-bf053c7b4e4c \
  --require-persisted-endpoint --invoke-once --hosted-version 1 \
  --expected-version a7a1e2f03d089eeb489ad1614383c889a32894ce \
  --session-id smoke-109-44ca46e57cd64e19a3a7e3752c49b5de --execute
```

This is an execution record, **not permission to rerun**. Cloud writes: ACR image publication, one hosted version, one ACA-MI-created session and one Responses POST, followed by session stop/delete and exact-version deletion. No app PUT, identity/RBAC/network/secret/model-capacity change, production action, web activation, image generation or shared-resource deletion. Serving web image remained unchanged. No operator process remains running.
Post-run regression tests: **225 passed** (`python -m pytest --noconftest tests/test_aca_identity_probe.py tests/test_foundry_agent_client.py -q`). `git diff --check` passed. No application or infrastructure source changed during this E2E workstream.

**#109 remains open:** routing compatibility is verified offline and its source
was deployed, but live passage through the strict boundary is unproven;
`invalid_request` is not schema-valid domain success. PR #122 is not merged by
this operation.

## Persisted-endpoint ACA MI E2E — 2026-09-09

Working as Gimli (DevOps / Infra), using the requester's fresh, bounded
authorization and coordinator-reported independent Samwise review of
`aad771477c34fb4c699d2bc63e279aa2be1badd7`. **That full SHA is the deployed
application/image source; subsequent evidence-documentation commits are not
the deployed image.** This run did not re-provision the already-persisted
endpoint or change permissions.

**Result: deployment and same-identity session creation/readiness succeeded;
the one Responses POST returned HTTP 400. E2E domain success remains blocked.**
There is no validated card, refusal, held result, schema or response-version
match. `serviceCode:"unknown"` does not identify the source of the HTTP 400.
Actual model-call/token counts are unobserved, not proven zero. No inference
retry is authorized by this result; this run's allowance is consumed.

Fresh GET-only preparation on the serving ACA replica returned
`invocation_prepared`, HTTP 200, `accessVerified:true`, `principalMatched:true`,
`endpointPersisted:true`, `endpointSource:"aca_environment"`,
`parserImportReady:true`, `requestSchemaReady:true`,
`localFixtureParseReady:true`, and `invocationsAttempted:0`.
It made no session-create or Responses POST. The local fixture is not service
generation evidence.

| Evidence | Observed result (2026-09-09 UTC) |
| --- | --- |
| Image | `fcagdevqhg3qc4rlbt4gacr.azurecr.io/card-orchestrator:aad771477c34fb4c699d2bc63e279aa2be1badd7` |
| ACR digest | `sha256:9eceae6213c24da0636ce4691a0f1d77d5c40128b291870154571688a657267f` |
| Pre-recorded session | `smoke-109-3cb91a4027a3452aadad1f8e946f16ae`; exact GET 404 at `06:32:48.009856Z` |
| Initial version inventory | HTTP 200, zero versions; no existing version reused |
| Deploy submission / runtime clock start | `06:32:48.010438Z`; 0.5 CPU / 1GiB, dedicated azd project, one deployment |
| Deploy return | Exit 0 at `06:33:45.780705Z`, **57.776 monotonic seconds** |
| New owned version | `card-orchestrator:1`, `created_at:1788935592`; exact version/image/application ownership checked at `06:33:48.315762Z` |
| ACA target | Revision `fcag-dev-app--endpoint-ea77f0bf2596`, replica `fcag-dev-app--endpoint-ea77f0bf2596-5f75998d86-nwvzw`, container `web` |
| Expected actual ACA system MI | `946d8701-48f2-4fa5-8efd-bf053c7b4e4c`, checked audience `https://ai.azure.com/.default` |
| Single probe submission / return | `06:33:52.026525Z` / `06:34:12.308277Z`, exit 1, **20.285 monotonic seconds** |
| Session and inference counters | One session-create attempt, created and ready, exact session/version matched by the fail-closed creation gate; **one Responses POST attempt**, zero retries |
| Finally begins | `06:34:12.309084Z`, **84.308 seconds** after deployment submission |
| Session ownership reconciliation | Exact GET 200 and matching pre-recorded ID/version at `06:34:13.823606Z` |
| Session stop / delete | Exit 0 at `06:34:22.442762Z` / `06:34:25.023440Z` |
| New version deletion | Exact version `1` only, exit 0 at `06:34:27.802542Z`, **99.801 seconds** after submission |
| Independent absence verification | Exact session and version GET **404** at `06:34:30.671927Z`; session-list endpoint **404** at `06:34:31.479304Z` |
| Cleanup complete | `06:34:31.480434Z`, **103.479 seconds** after submission |
| Final web verification | `06:34:35.382233Z`, unchanged safe baseline, endpoint persisted, `/healthz` HTTP 200 |
| Entire run including digest read | `06:34:41.368186Z`, **113.370 seconds**; below 30 minutes |

The session-list 404 proves endpoint absence after removal of its sole version,
not a fictional HTTP-200 empty page. Exact session/version 404s and successful
stop/delete independently establish cleanup. This run's UTC clock and monotonic
durations agree; the earlier September 8 records and their clock caveats remain
separate history.

Exact sanitized marker (the probe prints this object without its internal
`ACA_IDENTITY_PROBE=` transport prefix):

```json
{"accessVerified":false,"endpointPersisted":true,"endpointSource":"aca_environment","httpStatus":400,"invocationVerified":false,"invocationsAttempted":1,"phase":"invoke","principalMatched":true,"reason":"http_error","schemaValid":false,"serviceCode":"unknown","sessionCleanupRequired":true,"sessionCreateAttempted":true,"sessionCreated":true,"sessionReady":true,"status":"failed","tokenAcquired":true}
```

`sessionCleanupRequired:true` describes the marker at invocation return;
the subsequent operator cleanup above completed. `accessVerified:false` here
does not negate the separate HTTP-200 access preparation or successful session
creation; the invocation branch only sets it after a successful Responses reply.

Exact probe flags, run from the repository root:

```bash
python deployments/card-orchestrator/aca_identity_probe.py \
  --environment dev \
  --subscription b8ff3e15-7e2d-4fac-a773-992fb59ccedd \
  --resource-group rg-fcag-dev --app fcag-dev-app \
  --revision fcag-dev-app--endpoint-ea77f0bf2596 \
  --replica fcag-dev-app--endpoint-ea77f0bf2596-5f75998d86-nwvzw \
  --container web \
  --project-endpoint https://aifcagdevqhg3qc4rlbt4g.services.ai.azure.com/api/projects/fantasy-cards-dev \
  --expected-principal 946d8701-48f2-4fa5-8efd-bf053c7b4e4c \
  --require-persisted-endpoint --invoke-once --hosted-version 1 \
  --expected-version aad771477c34fb4c699d2bc63e279aa2be1badd7 \
  --session-id smoke-109-3cb91a4027a3452aadad1f8e946f16ae --execute
```

This is an execution record, **not permission to rerun**. The prior nonbillable
preparation used the same target plus `--require-persisted-endpoint
--prepare-invocation --execute`, omitting invocation/version/session flags.
Local invocation budgets were 30 seconds setup / 100 result / 130 total;
remote setup and invocation budgets remained 30 / 65 / 95 seconds.
The unchanged runtime bounds allow at most three existing-model stages,
20 seconds and 1800 output tokens per stage, 65 seconds overall, no retries.

The isolated dev azd context was missing in this worktree, so it was created
from explicit allowlisted nonsecret values using `azd env new` / `azd env set`,
not copied from another environment or read from `.env`. Both endpoint context
names were set identically, along with the existing model `gpt-5-5`, existing
registry/project/resource-group bindings and the full application SHA.
`AZURE_DEV_USER_AGENT=microsoft_foundry_skill` was process-local for azd.
No dependencies were installed, no root hooks or provisioning ran, and no
evaluation/extra model probe was executed.

**Offline diagnostic after cleanup:** the exact deployed image was run with
`--rm --network none`, no credentials or mounts, and no server. Its actual
`validate_wire()` rejects the exact outbound request with `invalid_request`;
removing only `agent_session_id` makes that request pass. The hosted boundary
currently allows only `store`, `stream`, `input`, and `metadata`.
This is a reproducible local contract mismatch **if the platform forwards the
routing field**; it is not proof that the platform forwarded that field in
this failed live call or that its HTTP 400 originated in this boundary.
The local operator interpreter lacked the optional hosted dependency, so the
already-built image supplied the offline diagnostic without any installation.

**Precise backend follow-up:** verify the platform-to-container routing-field
contract offline; if forwarded, accept only a tightly validated routing-only
`agent_session_id` and discard it before SDK normalization. Add an exact
probe-body-to-host-boundary regression and retain strict rejection of unrelated
fields/history/persistence. Extend fixed-code diagnostics to distinguish the
owned `invalid_request` response from platform errors without exporting bodies.
Do not remove the required session selector from the platform request, broaden
RBAC, or retry paid inference by guess. No backend fix is claimed in this
documentation-only follow-up.

Cloud writes in this run were the owned ACR image/package publication, one
hosted version, one ACA-MI-created session and the one Responses attempt,
followed by session stop/delete and exact-version deletion. No app PUT,
identity/RBAC/network/secret/model-capacity change, production action, web
activation, image generation or shared-resource deletion occurred. The
serving web image remained
`fcagdevqhg3qc4rlbt4gacr.azurecr.io/fantasy-cards-generator/web-nat-dev:azd-deploy-1788775195`;
revision, identities, ingress, Single/100%-latest traffic and
`FOUNDRY_PROJECT_ENDPOINT` matched the fresh before/after baseline.
The agent image and earlier images remain in ACR and can incur storage cost.
No operator server/container/process remains running.
Post-run existing probe/client regression tests: **222 passed**
(`python -m pytest --noconftest tests/test_aca_identity_probe.py
tests/test_foundry_agent_client.py -q`); `git diff --check` passed.
No application or infrastructure source changed during this E2E workstream.

**#109 remains open:** persisted project configuration, existing RBAC,
actual-audience ACA identity/session access and real hosted packaging are
observed; schema-valid domain invocation and response version matches are not.
PR #122 is not merged by this operation.

## Contract and status

| Dimension | Contract |
| --- | --- |
| azd project | `deployments/card-orchestrator/azure.yaml` |
| Environment / service | `dev` / `card-orchestrator`; `prod` remains blocked pending review |
| Runtime | Python 3.12, Linux amd64, non-root |
| Entrypoint | `python -m hosted_agents.card_orchestrator` |
| HTTP | `/responses` and non-model `/readiness` on port 8088 |
| Protocol declaration | `responses`, version `2.0.0` (not REST `api-version=v1`) |
| Initial reviewed allocation | 0.5 CPU / 1GiB; not automatically provisioned |
| Model | Existing text deployment; three bounded concept/lore/art-direction specialists |
| Excluded | Image generation, persistence, web activation, scheduled evaluation/probes |

The real runtime and `hosted-agent` optional extra are integrated with the
dedicated container/deployment package in PR #119. Packaging-only tests are not
startup evidence; the integrated validation below includes the correct agent
Dockerfile, real entrypoint and credential-free readiness check.

Résultat historique du 8 septembre : le [smoke corrigé à identité unique](#smoke-corrigé-à-identité-unique--2026-09-08)
a déployé la nouvelle image, mais son unique dispatch a échoué localement avec
`exec_setup_timeout`. Aucun marqueur distant ne permet de compter les POST :
leur nombre reste **inconnu**, et la nouvelle autorisation est consommée.
La nouvelle version a été supprimée ; les GET exacts de version/session ont
confirmé HTTP 404. L'invocation de bout en bout reste non démontrée. Le HTTP 403
documenté plus bas appartient à la tentative précédente, pas à celle-ci.

### Smoke corrigé à identité unique — 2026-09-08

Exécution Gimli sur PR #121, après la **nouvelle** approbation explicite et la
revue indépendante du contrat par Samwise. La préparation non facturable dans
la réplique ACA existante a réellement renvoyé `invocation_prepared`, HTTP 200,
`principalMatched:true`, `parserImportReady:true`, `requestSchemaReady:true`,
`localFixtureParseReady:true` et `invocationsAttempted:0`. Elle n'a créé aucune
session et sa fixture locale ne prouve aucune génération par le service.

| Preuve | Résultat réel (UTC) |
| --- | --- |
| Source application, image et requête | `dc1925942c42756690f7dd5321cbdbf892cf7182` ; les commits documentaires ultérieurs ne sont pas cette image |
| Image ACR | `fcagdevqhg3qc4rlbt4gacr.azurecr.io/card-orchestrator:dc1925942c42756690f7dd5321cbdbf892cf7182` |
| Digest vérifié dans ACR | `sha256:1c15ec813aa47991d59f8dba42ec50062130e10a878c4f4e9551751ab6eb5f94` |
| Session préenregistrée | `smoke-109-46f9af78e7ef4129b961f4d80ef16b00`, GET exact **404** à `13:15:23.040602Z`, avant tout déploiement/création |
| Soumission / début du plafond de 30 minutes | `13:15:23.568639Z`, **0,5 CPU / 1GiB**, une seule soumission |
| Déploiement azd dédié | Exit 0 à `13:16:44.523033Z` ; aucun provisionnement/hook racine |
| Nouvelle version détenue | `card-orchestrator:1`, `created_at:1788873361`, image et SHA applicatif vérifiés à `13:16:46.797699Z` |
| Cible ACA inchangée | Révision `fcag-dev-app--azd-1788775203`, réplique `fcag-dev-app--azd-1788775203-69f9dc897b-c97p9`, conteneur `web` |
| MI attendue, confirmée par la préparation | `946d8701-48f2-4fa5-8efd-bf053c7b4e4c` ; aucune identité développeur de substitution dans la sonde |
| Unique dispatch de sonde / résultat local | `13:16:54.671713Z` / `13:17:05.120162Z`, exit 1, **zéro relance** |
| Marqueur local exact | `status:failed`, `reason:exec_setup_timeout`, `invocationAllowanceConsumed:true`, `sessionCreationUnknown:true`, `sessionCleanupRequired:true` |
| Compteurs distants | `sessionCreateAttempted`, `sessionCreated`, `sessionReady`, `invocationsAttempted` **non observés** : ne pas les remplacer par 0 ou 1 |
| HTTP / code de service / schéma / résultat métier | **Non observés** ; aucun HTTP 403/200 d'invocation, aucun `serviceCode`, aucune validation de versions dans une réponse réelle |
| Entrée dans le `finally` | `13:17:05.121148Z` |
| Réconciliation avant suppression | GET session **404** à `13:17:06.878569Z` ; inventaire sessions HTTP **200**, zéro session liée à cette version à `13:17:07.820645Z` |
| Suppression de la seule nouvelle version | `azd ai agent delete card-orchestrator --version 1 --force`, exit 0 à `13:17:11.618497Z` |
| Première vérification indépendante après suppression | GET exacts version/session **404** à `13:17:14.738093Z` ; liste sessions **404**, donc endpoint absent, pas une page HTTP-200 vide |
| Réconciliation répétée | GET exacts toujours **404** jusqu'à `13:18:07.892724Z`, puis au contrôle final horodaté `13:54:04.740514Z` |
| Comparaison web finale | Identique : image, identités, révisions, trafic, ingress et mode ; `endpointPersisted:false` |

Le numéro de plateforme `1` a été réattribué après suppression des anciennes
versions ; l'inventaire initial de versions était absent. L'image, le SHA et
`created_at` identifient la **nouvelle** version, sans confusion avec les versions
historiques également numérotées `1`.

**Échec réel et limite de preuve.** Le délai local de dix secondes pour la
connexion et l'envoi complet du payload a expiré. Le wrapper n'a reçu aucun
marqueur distant validé. Une seule sonde a été dispatchée ; le nombre exact de
POST de création/Responses arrivés au service est inconnu, et non « un appel
Responses confirmé » ou « zéro appel ». L'autorisation est donc consommée par
prudence, sans nouvelle tentative. La préparation réussie utilisait son propre
budget local de 30 secondes et ne prouvait pas le budget de dix secondes.
Ce résultat ne teste ni ne réfute les permissions de création par la MI ; il
n'établit pas non plus la cause du HTTP 403 historique.

**Nettoyage et horloges.** Aucune session n'a été observée : aucun `sessions stop`
ou `sessions delete` n'a donc été envoyé sans preuve de propriété. La version
nouvellement détenue a été supprimée avec `--version 1 --force`, puis le contrôleur
a poursuivi uniquement les GET bornés de réconciliation d'une création inconnue.
Suppression et premiers GET exacts 404 sont enregistrés moins de deux minutes
après soumission, donc avant le plafond de 30 minutes. L'horodatage UTC saute
ensuite de `13:18:07` à `13:54:04`, alors que le contrôleur mesure **193,6 secondes
monotones** de la soumission à la comparaison web finale. Cette divergence est
conservée, sans en inventer la cause : le dernier contrôle UTC est après la
fenêtre, mais la suppression et plusieurs preuves 404 précèdent ce saut.
Ne pas présenter toute la vérification finale comme achevée avant 30 minutes UTC.

**Périmètre et coûts.** Les trois grants préexistants ont été revérifiés à leurs
scopes exacts : MI ACA → Consumer projet ; MI projet → Foundry User compte et
AcrPull registre. Aucun grant, réseau, secret, capacité modèle ou ressource web
n'a été modifié. Les seules écritures cloud demandées étaient l'image ACR et la
nouvelle version agent, puis sa suppression ciblée ; la création de session
demandée par la sonde reste inconnue. L'état azd isolé a enregistré le nouveau SHA
et les métadonnées de déploiement. Pas d'évaluation, de télémétrie optionnelle
activée, d'images utilisateur ou de génération d'image. Les bornes source restent
trois appels maximum à `gpt-5-5`, 1800 tokens de sortie par étape, sans retry ;
l'usage effectif modèle/tokens n'est pas observé. La nouvelle image et les images
historiques sont conservées dans ACR, avec coût de stockage possible.

**Écarts ouverts.** Création effective par la MI, readiness de sa session,
Responses réel, schéma métier terminé, versions retournées et autorisation du
runtime vers le modèle restent non démontrés. Endpoint ACA toujours non persisté ;
son éventuelle injection, l'intégration web et l'acceptation production restent
séparées. #109 reste ouvert et PR #121 n'est pas fusionnée automatiquement.

### Next separately approved window: same-identity session contract

**Historical correction gate.** At its offline review, no new allowance,
deployment, session or inference had occurred. The then-last deployed image was
application source
`2bdbf9967d8c397f7d88914bac06285b3b477297`; it did not include this correction.
The separately approved corrected execution is recorded above; it did not obtain
live same-identity session/invocation evidence and does not authorize another run.
The documented caller-Entra session scope and current Responses
`agent_session_id` field are verified contract facts. Cross-identity ownership
is a plausible explanation of the historical 403, **not a proven diagnosis**
because the error body was discarded. No RBAC escalation is justified.

For Gimli's **next independently approved** execution, this sequence supersedes
any earlier operator-created warmup-session recipe:

1. Keep the existing identity, project Consumer role, model limits and web
   baseline. Build/deploy only the reviewed new source and record its immutable
   application SHA, image digest, exact new hosted version and submission time.
   Start the **30-minute maximum runtime clock at deployment submission**, with
   cleanup in an outer `finally`, including deployment timeout. Do not deploy
   from a historical image and label it the corrected code.
2. Before any session creation or probe dispatch, durably record a fresh
   `smoke-109-` plus `uuid.uuid4().hex` identifier, agent name, exact new version,
   request-source SHA, application SHA and create-dispatch timestamp. Using the
   already-privileged operator's read-only session GET, establish that this exact
   ID is absent (404); don't reuse old IDs. A 403/timeout is not absence.
   Do **not** create a session as the operator or invoke for warmup.
3. Run GET-only `--prepare-invocation` if needed; preparation never creates a
   session and is not invocation proof. Invoke the corrected probe **once**,
   with `--invoke-once --hosted-version <new-version>
   --expected-version <application-sha> --session-id <recorded-id>`, pinned to the
   existing ACA revision/replica/container and expected system principal.
   Inside ACA, the same checked in-memory MI token creates the session (one POST)
   and invokes it (at most one Responses POST). HTTP 201 and matching
   `agent_session_id` / `version_indicator` are mandatory. `creating`/`updating`
   trigger only bounded readiness GETs; only `active` permits inference.
4. Local invocation transport is capped at **30 seconds setup + 100 seconds
   result, 130 seconds total**. Remote work is capped at **30 seconds setup**
   (source decoding/import/MI/create/readiness, at most 15 GETs) plus a separate
   **65-second invocation** guard. Preparation retains 30/80/110 local and
   70 remote. The existing model orchestration remains three stages, 20 seconds
   per stage / 65 overall, no retry. No timeout or HTTP failure authorizes a
   second inference call; own-session create permissions remain untested live.
5. In **every finally**, preserve the strict sanitized result first, then
   reconcile the pre-recorded exact ID with bounded operator `azd`/session API
   operations. `sessionCreateAttempted:true` with timeout/malformed output means
   completion can be unknown. Missing marker means creation **and** invocation
   completion are unknown; assume allowance consumed. Never depend on a
   server-returned session ID being captured before cleanup.
6. Stop/delete only the recorded newly owned session, after matching the exact
   new version and the pre-recorded absence/create interval; never delete a
   mismatched or pre-existing resource. **409 collision forbids deleting that
   session**, even if it resembles the expected ID. Reconcile unknown creation
   with exact GETs every two seconds for at most **60 seconds**, each request
   capped at **10 seconds**; no create or inference retries. Run this reconciliation
   again after deleting only the run's new hosted version, so late completion
   cannot be mistaken for early absence. Verify exact session/version GET 404
   and no active matching sessions using the privileged operator (no role
   changes). Bound the entire cleanup to **five minutes**, begin it no later than
   minute 25 of the deployment clock, and report unresolved cleanup explicitly
   on deadline/403/mismatch rather than claiming success or touching older state.
   A single early 404 after unknown create completion is not cleanup proof.

The remote probe deliberately does **not** stop/delete in its own finally:
cleanup must not consume its model/result budget or destroy evidence. Existing
privileged operator cleanup can manage cross-user sessions; the ACA caller
continues to have only its existing consumer grant. No optional telemetry
inspection is a gate, and no service messages, raw responses, tokens or arbitrary
headers are exported. See the [wire and diagnostic contract](foundry-agent-invocation.md#actual-aca-managed-identity-access-probe).

The platform supplies `FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_AGENT_NAME`,
`FOUNDRY_AGENT_VERSION`, and the Application Insights connection configuration.
Do **not** redeclare reserved values in service `environmentVariables`.
Only non-secret runtime switches are supplied by this manifest:
`AZURE_AI_MODEL_DEPLOYMENT_NAME`, `CARD_ORCHESTRATOR_VERSION`,
`TELEMETRY_ENABLED`, `TELEMETRY_ENVIRONMENT`, and the experimental
`AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING=true` SDK tracing opt-in. The version is
the full immutable application Git commit, not a Foundry version number. Runtime
timeout/policy defaults belong to the runtime; inspect their bounded values during
integration rather than adding guessed SDK settings here.

The implemented defaults/maxima are 20 seconds per specialist and 65 seconds
overall. These are **offline candidate budgets**, not compliance with the
proposed production budgets of 8.15 seconds per stage and 30.15 seconds overall.
Narrow local safety heuristics are not comprehensive safety or prompt-injection
protection; unobserved hosted guardrails remain unavailable and post-image checks
are not applicable. Application nonpersistence does not guarantee zero platform
telemetry.

The same `FOUNDRY_PROJECT_ENDPOINT` name is also an **azd deployment-context**
value consumed by the extension. Bicep outputs both that name and
`AZURE_AI_PROJECT_ENDPOINT`; this does not override the platform's container
environment. If provisioning is skipped, configure both locally to the same
verified project endpoint.

Current hosted-agent documentation distinguishes the dedicated **per-agent
Entra runtime identity** from the **project MI** used for infrastructure such as
registry pulls. The model client must use the supported platform credential
chain; do not force the project principal into the container. The compatibility
role repairs below preserve main's contract but are not proof of the agent's
runtime identity or successful model authorization. Verify that distinction at
the approved deployment gate without granting additional downstream roles.

## Source and tooling verification

Verified locally: azd **1.32.0**, `azure.ai.agents` **1.0.0-beta.13**,
Bicep **0.46.1**. The extension is pinned; no setup script installs/upgrades tools
automatically. JSON-compatible YAML permits offline structural tests without a
new parser dependency.

Authoritative references (reviewed 2026-09-08; Azure/azure-dev source revision
`d73bf6194913b257e02332a79932c727ac5eaaa0`):

- [azd schema: service Docker path/context/platform and extension versions](https://github.com/Azure/azure-dev/blob/d73bf6194913b257e02332a79932c727ac5eaaa0/schemas/v1.0/azure.yaml.json)
- [Agent container resource tiers](https://github.com/Azure/azure-dev/blob/d73bf6194913b257e02332a79932c727ac5eaaa0/cli/azd/extensions/azure.ai.agents/internal/project/config.go)
- [Agent deployment: project endpoint and Responses 2.0.0](https://github.com/Azure/azure-dev/blob/d73bf6194913b257e02332a79932c727ac5eaaa0/cli/azd/extensions/azure.ai.agents/internal/project/service_target_agent.go)
- [Existing-project Bicep route](https://github.com/Azure/azure-dev/blob/d73bf6194913b257e02332a79932c727ac5eaaa0/cli/azd/extensions/azure.ai.agents/internal/synthesis/templates/existing-project.bicep)
- [Official project registry connection and AcrPull identity](https://github.com/Azure/azure-dev/blob/d73bf6194913b257e02332a79932c727ac5eaaa0/cli/azd/extensions/azure.ai.agents/internal/synthesis/templates/modules/acr.bicep)
- [Hosted agent requirements](https://learn.microsoft.com/azure/ai-foundry/agents/concepts/hosted-agents?view=foundry)
- [Docker-specific ignore-file semantics](https://docs.docker.com/build/building/context/#dockerignore-files)

The connection API deliberately uses `2025-04-01-preview`: the official azd
template records a GA `2025-06-01` connection-resolution failure. This is not a
reason to change the account or project's existing API/resource configuration.
At that packaging checkpoint, installed CLI help and upstream source had been
inspected but live deployment had not yet been verified. The executed-smoke
sections below record the subsequent successful deployments and failed invocation
outcomes separately.
The hosted-agent observability documentation confirms platform injection of the
Application Insights connection string; no speculative connection resource is
added by this package.

## Offline integration gate

### Completed integrated evidence — 2026-09-08

Application build `39278a3f0f80c735ed52235a7d5e21026f10ecfb` combines runtime
`941dc39` and packaging `f17ff4c` (cherry-picked as `39278a3`). The coordinator
directly verified:

- **202 tests passed** in one invocation: `test_card_orchestrator`,
  `test_card_orchestrator_models`, `test_foundry_agent_client`,
  `test_hosted_agent_deployment_config`, and `test_deployment_config`.
  Earlier backend-only results are separate evidence, not an additive total.
- Dedicated Bicep compiled; the exact Linux amd64 agent Dockerfile built as
  `card-orchestrator-agent:39278a3`. A root web-image build is not agent evidence.
- Image entrypoint `python -m hosted_agents.card_orchestrator`, port 8088,
  non-root user `agent`. With synthetic endpoint/model settings, build version
  `39278a3`, and **no credentials, secrets or mounts**, `/readiness` returned
  HTTP 200 with `{"status":"ready","cloudProbe":false}`. A request to `/responses`
  with `store:true,input:[]` returned HTTP 400, sanitized `invalid_request`.
  An initial cold-start probe reset before startup (approximately 14 seconds);
  later probes succeeded. The test container was stopped and removed.
- Independent Samwise review of `origin/main..39278a3` found no significant
  defects. This is code-review evidence, not live authorization or model-quality
  acceptance.

No billable calls or Azure resource mutations occurred in those checks.
The subsequent publication update changes this runbook only, not that tested
application source.

For repeatable infrastructure checks from the repository root:

```bash
python -m pytest -q --noconftest tests/test_hosted_agent_deployment_config.py tests/test_deployment_config.py
az bicep build --file deployments/card-orchestrator/infra/main.bicep --stdout >/dev/null
docker build --platform linux/amd64 \
  -f hosted_agents/card_orchestrator/Dockerfile \
  -t "card-orchestrator:$(git rev-parse HEAD)" .
```

`--noconftest` is intentional for these infrastructure-only tests: it avoids
bootstrapping the web application, authentication fixtures, and local settings.
Run the runtime owner's targeted tests separately, with fake model clients and
telemetry disabled. Validate readiness in that offline harness without a model
call; don't expose a cloud credential to a local container just to test health.

The extension requires service `project: "."` inside the dedicated manifest
folder; `project: "../.."` fails its service-path validation. Docker `path` and
`context` are relative to that service root and explicitly select the repository
build context and agent Dockerfile. `remoteBuild: false` avoids uploading an ACR build archive.
`language: docker` makes azd build the Dockerfile directly, rather than attempting
a host-side Python/requirements.txt restore in the manifest directory.
The Dockerfile-specific ignore file takes precedence over root `.dockerignore`.
Only Python source, dependency metadata, README and this Dockerfile are allowed;
azd state, histories, dotenv files, credentials and assets are excluded. Runtime
data-file additions require an explicit allowlist/test update.

## Required nonsecret inputs and read-only checks

Use a separate azd `dev` state under the dedicated project. Do not copy the root
`.azure` directory or dump `azd env get-values`. Record these nonsecret bindings
with `azd env set` inside `deployments/card-orchestrator`:

| Variable | Source / constraint |
| --- | --- |
| `AZURE_SUBSCRIPTION_ID`, `AZURE_LOCATION` | Reviewed dev subscription and supported existing project region |
| `AZURE_RESOURCE_GROUP` | Existing shared resource group; this package assumes all references are in it |
| `AZURE_AI_ACCOUNT_NAME`, `AZURE_AI_PROJECT_NAME` | Existing account/project names from management-plane lookup |
| `AZURE_AI_PROJECT_ID` | Exact verified project ARM ID, never guessed from DNS |
| `AZURE_AI_PROJECT_ENDPOINT`, `FOUNDRY_PROJECT_ENDPOINT` | Same verified project endpoint |
| `AZURE_CONTAINER_REGISTRY_NAME`, `AZURE_CONTAINER_REGISTRY_ENDPOINT` | Existing registry name and login server |
| `AZURE_CONTAINER_REGISTRY_RESOURCE_ID` | Exact registry ARM ID |
| `AZURE_CONTAINER_APP_NAME` | Existing dev ACA app whose system identity invokes the agent |
| `AZURE_AI_MODEL_DEPLOYMENT_NAME` | Existing text deployment; no model capacity is created |
| `AZURE_AI_PROJECT_ACR_CONNECTION_NAME` | Existing registry connection name, or reviewed new `<registry>-conn` |
| `CARD_ORCHESTRATOR_VERSION` | Full integrated commit SHA; never reuse a tag for a different build |

For a fresh isolated azd state, the manifest already exists:

```bash
cd deployments/card-orchestrator
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env new dev
# Set the reviewed inputs individually; examples deliberately use placeholders.
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set AZURE_SUBSCRIPTION_ID "<subscription-id>"
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set AZURE_RESOURCE_GROUP "<existing-dev-rg>"
```

Check identity principal IDs, exact project ID/endpoint, registry role-assignment
mode/network policy, existing connection **names/categories/targets only**, and
existing role-assignment **IDs/scopes/principals/role IDs only**. Do not request
connection credentials, listKeys, tokens, dotenv contents or complete app config.
Verify the image publisher already has the registry's appropriate push role;
this package does not grant deployer privileges.

Classic registry RBAC is the only implemented path. If
`roleAssignmentMode=AbacRepositoryPermissions`, stop: classic AcrPull is ignored.
Require a separately reviewed repository-scoped ABAC policy, not a wider role or
a registry mode change. Also stop if existing network restrictions prevent
access; no public-access relaxation is authorized.

Application Insights linkage is a prerequisite for end-to-end tracing, **not**
hosted deployment. The existing-project template and deployment validation do
not require it. This runtime disables instrumentation, host observability and
SDK/access logging; proceed without telemetry IaC changes. This is not a promise
about platform retention or evidence that monitoring is configured.

## Bounded prerequisite preview, then separate approvals

The launcher is **plan-only by default**, dev-only, always uses the dedicated
manifest folder and requires an extra approval flag for execution of mutations.
It is an operator guard, not a security boundary: raw `azd deploy` bypasses it.
No scheduled workflow or automatic deployment is installed.

```bash
python deploy.py preview
python deploy.py deploy
```

Infrastructure defaults are `CARD_ORCHESTRATOR_ENABLE_PREREQUISITES=false` and
`CARD_ORCHESTRATOR_CREATE_REGISTRY_CONNECTION=false`. With both off, provisioning
only resolves existing references and outputs; it creates no agent compute.
After nonsecret inventory and approval to **preview** the proposed role repair:

```bash
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set CARD_ORCHESTRATOR_ENABLE_PREREQUISITES true
# Only if the registry connection is confirmed absent:
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set CARD_ORCHESTRATOR_CREATE_REGISTRY_CONNECTION true
AZURE_DEV_USER_AGENT=microsoft_foundry_skill python deploy.py preview --execute
```

Allow only these changes in the what-if:

1. Project system MI → **Foundry User** at its existing account.
2. Existing ACA system MI → **Foundry Agent Consumer** at the existing project.
3. Project system MI → **AcrPull** at the existing registry (classic RBAC).
4. Optional `ContainerRegistry` project connection using managed identity,
   existing login server/registry ID, and the official project-principal shape.

The first two assignments are a **repair facade**, not new ownership:
`infra/modules/ai-foundry.bicep` remains canonical. Their names, scopes,
principals and role IDs exactly match main; offline tests enforce parity.
Do not run root and isolated provisioning concurrently. Use incremental
deployment only, never complete mode or `azd down` on this shared-resource
package. At the next independently approved root rollout keep its existing
`ENABLE_FOUNDRY_AGENT_ACCESS=true` setting aligned.

A same-scope/principal/role assignment under another GUID must be reconciled
before apply: do not delete/recreate or silently invent a second assignment.
Likewise reuse an existing registry connection by setting its discovered name
and leaving connection creation off. Root already owns a **different** ACA image
pull identity; do not modify that assignment.

Reject any what-if that writes an account/project/registry/app, deploys a model,
changes networking/traffic/image/secrets, or removes existing resources. Bicep
compilation alone cannot validate ARM-time permissions or connection semantics.
Only after separate approval:

```bash
AZURE_DEV_USER_AGENT=microsoft_foundry_skill python deploy.py provision --execute --approve-change
```

### Actual dev prerequisite apply and ACA identity — 2026-09-08

PR #119 merged as `5f76207`. The earlier subscription/azd previews returned
success without inspectable changes. A **resource-group leaf what-if** against
`infra/modules/prerequisites.bicep`, with exact live ACA/project principals,
resolved this: precisely three role-assignment Creates and one registry
connection Create; every other resource was Ignore. Remaining `reference()`
expressions pointed to the existing project principal and registry login server,
independently verified through safe management-plane projections.

For this diagnostic use `az deployment group what-if --result-format
FullResourcePayloads --no-pretty-print` with the leaf's explicit parameters.
**Project the response before printing or persisting it**: retain change types,
resource IDs and allowlisted role/connection fields only. FullResourcePayloads
can include complete configuration for unrelated **Ignore** resources; never
dump the unfiltered response. Diagnostic ARM what-if does not replace azd/Bicep
for apply.

After confirming all three exact grants and the connection were absent, and
the registry used `LegacyRegistryPermissions`, the authorized dedicated
`python deploy.py provision --execute --approve-change` ran **once**. Actual
resource mutations, subsequently verified:

- Project MI → Foundry User, existing account scope.
- ACA system MI → Foundry Agent Consumer, existing project scope.
- Project MI → AcrPull, existing registry scope.
- Existing project → `<registry>-conn`, ContainerRegistry / ManagedIdentity.

The nested `card-orchestrator-dev-prerequisites` deployment **Succeeded**.
The parent `dev-1788866608` deployment nevertheless failed **after** these writes:
`DeploymentOutputEvaluationFailed`, because the old subscription-scope
`resourceId(resourceGroupName, ...)` output interpreted the RG name as a
subscription ID. This branch fixes the output to the existing `project.id`;
ARM subscription validation **Succeeded** with prerequisites off. After
independent review, the output fix was applied through isolated azd with both
resource-mutation flags disabled. Deployment `dev-1788867445` **Succeeded** and
returned the correct existing project ARM ID. Its operations were a registry
read and deployment-output evaluation; no additional resource changes were made.
Do not interpret the failed parent deployment as rollback or rerun blindly.

The new read-only [ACA identity probe](foundry-agent-invocation.md#actual-aca-managed-identity-access-probe)
ran inside the existing serving `web` container, explicitly acquired an MI
token for `https://ai.azure.com/.default`, and matched its `oid` to the live ACA
system principal before sending it. `GET /agents?api-version=2025-11-15-preview`
returned **HTTP 200, `data:[]`**. This proves target-MI project **access**, not
agent invocation or hosted-runtime/model authorization.

Safe ACA baseline comparison before apply and after the probe was identical:
image, container, latest/ready revision, traffic, ingress, revision mode and
identity. `FOUNDRY_PROJECT_ENDPOINT` remains **absent from ACA configuration**;
the probe received the verified endpoint only as an argument. There was no
serving image/file/secret/env/traffic mutation, registry upload, agent compute,
model call, production change or root provision/hook. No tools/dependencies
were installed. The isolated ignored azd opt-in booleans were reset to `false`.
Targeted offline probe/infrastructure validation: **100 tests passed**.

Further hosted-deployment gates are tracked in the approved-smoke checkpoint
below. The prerequisite proposal creates no compute or model
capacity; a later separately approved hosted deploy can incur 0.5 CPU / 1GiB
compute, registry storage and telemetry ingestion costs, and a remote smoke
request can incur model charges. This runbook is partial #99 operations scope,
not installed dashboards, alerts or production readiness.

### Earlier checkpoint: incorrect telemetry gate — 2026-09-08

At requester approval, the allowed live test was **one** synthetic ACA-MI
invocation, at most three existing-model calls (1800 output tokens per stage,
no retry), with 0.5 CPU / 1GiB hosting for at most 30 minutes and explicit
cleanup. Registry storage, compute, telemetry and model costs were approved.
**Missing spend approval is no longer the blocker.**

Read-only checks against source `f927b2c0bc23c615eecc95919a37a9c4686c0ff0`
completed at **2026-09-08T11:43:40Z**:

- Existing ACR `fcagdevqhg3qc4rlbt4gacr` uses
  `LegacyRegistryPermissions`; the publisher has an existing inherited Owner
  role. No publisher grant or registry/network change is necessary.
- Existing project `fantasy-cards-dev` is in `eastus2`, a currently supported
  hosted-agent region. The Cognitive Services location-usage response contained
  no hosted-agent quota entries; this is **unknown capacity**, not zero usage or
  verified available quota.
- The project and account connection inventories each returned only
  `fcagdevqhg3qc4rlbt4gacr-conn` (`ContainerRegistry`, `ManagedIdentity`).
  No Application Insights connection or project telemetry property was present.
  Existing Insights component `fcag-dev-appi` exists in `rg-fcag-dev`, but its
  existence is **not** a project binding.
- The earlier checkpoint incorrectly treated a missing Application Insights
  link as a deployment blocker. Gandalf verified installed help and upstream
  revision `16f49c2`: this is a tracing prerequisite only. The approved smoke
  proceeds without a telemetry connection or credential changes. The earlier
  checkpoint did not attempt deployment; no connection string was read.
- The platform creates the agent's runtime identity on deployment and provides
  project model access by default; absence of a pre-created runtime identity
  was **not** treated as a blocker. No extra identity grant was requested.
- `GET /agents?api-version=v1` returned zero agents. No image was built/pushed,
  no hosted version/session was created, and **zero invocation attempts / zero
  model calls** occurred. Deployment/start/stop timestamps and image digest /
  hosted version are **not applicable**, rather than a claimed successful stop.

Installed `azure.ai.agents` beta.13 has **no `azd ai agent stop` command**.
The supported command is `azd ai agent sessions stop <session-id>`, which
terminates session compute and retains its filesystem; a later invocation can
resume it. Current platform documentation describes per-session sandboxes,
not configurable replicas. There is no supported endpoint-disable field/command
in the verified installed toolchain; the prior disable claim is withdrawn.
Cleanup paginates sessions, selects exact `version_indicator.agent_version`,
stops/deletes those sessions, and force-deletes **only the new version created
this run**. Verify no active matching sessions and exact-version GET 404.
Timeout/403 is not cleanup evidence. Start the 30-minute clock at deployment
submission and use `finally`; a deploy timeout does not cancel server-side work.

Before/after ACA projections were identical: container `web`, image
`fcagdevqhg3qc4rlbt4gacr.azurecr.io/fantasy-cards-generator/web-nat-dev:azd-deploy-1788775195`,
latest/ready revision `fcag-dev-app--azd-1788775203`, `Single`, latest traffic
100%, principal `946d8701-48f2-4fa5-8efd-bf053c7b4e4c`.
No Azure mutation command ran during this checkpoint, and no root hook,
production change, evaluation or dependency installation ran. The previously
verified actual ACA-MI access evidence remains valid but **invocation remains
unproven**. #109 stays open; PR #120 is not merged. The ACA endpoint-injection
gap and production latency/privacy acceptance remain separate.

Verify only the expected assignments/connection via safe projections; allow RBAC
propagation. Then build/readiness/runtime tests, privacy review, region/quota
checks and explicit compute/model-spend approval are separate deployment gates:

```bash
AZURE_DEV_USER_AGENT=microsoft_foundry_skill python deploy.py deploy --execute --approve-change
```

This creates a **new immutable Foundry agent version** and can incur compute
costs. The resource booleans do not disable this explicit deploy action.
Do not assume 0.5 CPU is production sizing; review observed readiness/latency
before changing the allocation. No production deploy is supported by the launcher.

## ACA endpoint injection: a separate stop gate

### Executed approved DEV smoke — 2026-09-08

The approved run **did execute** against the existing resources, without an
Application Insights link, telemetry IaC, root hooks, evaluation, web activation,
new model capacity, network changes or additional role assignments.

| Evidence | Actual result |
| --- | --- |
| Application/image source | `0dfb3ef8e47c29f86d6936eaedcea17ab6871334` |
| Registry image | `fcagdevqhg3qc4rlbt4gacr.azurecr.io/card-orchestrator:0dfb3ef8e47c29f86d6936eaedcea17ab6871334` |
| Registry digest | `sha256:22c7b63b85ad90bf4a2513437660b2c9eccec6de5de3e06d22ed1a64847584c6` |
| Deployment submission / clock start | `2026-09-08T11:57:15.473661Z` |
| Dedicated azd deploy | Exit 0, `2026-09-08T11:58:35.608805Z` |
| New hosted version | `card-orchestrator`, version **`1`**, observed `active` |
| Version-ref session | `02ea88d182aae88700SxWkhpky6493MLlK0tvxy7EiFIyYXZUP`, observed `active` at `11:58:46.431496Z` |
| ACA invocation dispatches | **1**, at `11:58:46.437594Z`; **0 retries** |
| Invocation outcome | `exec_no_evidence`, at `11:58:56.224365Z`; no validated card/refusal/held response |
| Session stop / delete | Exit 0 at `11:59:01.561745Z` / `11:59:03.373857Z` |
| Exact new-version force deletion | Exit 0 at `11:59:07.138924Z` |
| Independent cleanup checks | Exact version GET **404**, exact session GET **404**, agent GET **404**, session-list endpoint **404**, each `not_found` |
| Web baseline | Unchanged image, container, latest/ready revision, Single/latest 100% traffic, ingress and identity |
| Endpoint persisted in ACA | **false** |

The control script's first cleanup summary conservatively reported failure
because session listing returned 404 after deleting the sole version (the agent
endpoint disappeared too). Independent exact-resource GETs then confirmed
version/session/agent absence. This is **not** an HTTP-200 empty-list result:
the absence evidence is successful explicit stop/delete plus exact-resource and
endpoint 404s, not an ignored 403/timeout. Compute/session termination completed
within two minutes of submission, well inside the approved 30-minute window.
No unrelated version or shared resource was deleted. The image remains in ACR
and can continue incurring storage charges.

**One ACA exec invocation was dispatched; whether its Responses POST reached
Foundry is unknown (0 or 1), and its allowance is consumed.** No retry, developer
inference call or model repair was attempted. Model-call/token usage is
unobserved, not zero; code limits remain three calls, 1800 output tokens/stage,
20 seconds/stage and 65 seconds overall. The earlier explicit-MI access probe
proved the expected ACA principal, but this invocation exported no new identity
or schema evidence and must not be called an end-to-end success.

Live preparation found and fixed two manifest defects: extension service paths
cannot escape the isolated project; container builds need `language: docker`
to avoid a host-side requirements.txt restore. The failed Python restore created
a local virtual environment but installed no project dependencies; that local
artifact was removed. Actual agent Dockerfile build, packaging and push then
succeeded through the dedicated azd project.

An offline reproduction produced an encoded invocation command over 5700
characters. Canonical PTY line limits are a plausible transport failure, not a
proven service/model diagnosis.
The subsequent source-only fix splits it into lines of at most 1024 characters
and reconstructs it in memory; a canonical-PTY regression test passes. This
fix was **not** retried live and is not part of the published image source.
Targeted offline probe/deployment validation after the fix: **107 tests passed**.

#109 remains open. Actual invocation/schema/MI evidence, runtime model
authorization, production latency/privacy acceptance and separately reviewed
ACA endpoint injection remain unproven or out of scope. Approval for this
single consumed smoke does not authorize another invocation.

### Newly authorized DEV smoke — 2026-09-08

Requester `@bmoussaud` explicitly approved a **new** temporary DEV runtime and
one new synthetic request after the preceding allowance was consumed. This run
used that new allowance, not a retry under the old one. Bounds were unchanged:
0.5 CPU / 1GiB, at most 30 minutes from deployment submission, one Responses
request, at most three existing `gpt-5-5` calls, no retry, immediate owned-resource
cleanup. Instrumentation remained disabled; no Insights link was required or added.

Before any deployment, the current whole-parser/chunk bundle completed
`--prepare-invocation --execute` inside the pinned serving ACA replica:
`invocation_prepared`, `parserImportReady:true`, `requestSchemaReady:true`,
`localFixtureParseReady:true`, `tokenAcquired:true`, `principalMatched:true`,
`accessVerified:true`, HTTP **200**, empty agents page, **zero** invocations.
Its labelled local fixture is not real hosted-card evidence. Offline validation
of this revision passed **149 targeted probe/deployment tests**, Ruff and Black.

Fresh isolated azd `dev` state was initialized using verified nonsecret bindings
only; no existing dotenv/state was copied or read. The agent manifest retained
`project: "."`, `language: docker`, the repository build context and dedicated
agent Dockerfile. No root hooks or provisioning ran. The immutable source
remained unchanged through build, package, push and invocation.

| Evidence | Actual result (UTC) |
| --- | --- |
| Application/image source | `2bdbf9967d8c397f7d88914bac06285b3b477297` |
| Registry image | `fcagdevqhg3qc4rlbt4gacr.azurecr.io/card-orchestrator:2bdbf9967d8c397f7d88914bac06285b3b477297` |
| Registry digest | `sha256:cd23ea7a8f782eb434659d3c774646599df93bcbbfdb3322c1313f158f1f3989` |
| Deployment submission / clock start | `2026-09-08T12:45:17.427155Z` |
| Dedicated azd deploy | Exit 0 at `12:46:40.547790Z` |
| Newly created hosted version | `card-orchestrator`, **`1`**, observed `active` |
| Exact version-ref session | `smoke-109-2bdbf9967d8c-1788871606`, observed `active` at `12:46:58.064908Z` |
| Actual ACA target | `fcag-dev-app--azd-1788775203-69f9dc897b-c97p9`, container `web` |
| Expected/actual principal match | `946d8701-48f2-4fa5-8efd-bf053c7b4e4c`; `tokenAcquired:true`, `principalMatched:true` |
| Invocation dispatch / result | **1** at `12:46:58.065475Z`; HTTP **403** at `12:47:16.550685Z`; **0 retries** |
| Sanitized invocation marker | `status:failed`, `reason:http_error`, `invocationsAttempted:1`, `accessVerified:false`, `invocationVerified:false`, `schemaValid:false` |
| Domain/application/hosted-version validation | No validated domain result; both version-match checks **unobserved**, not successful |
| Session stop / delete | Exit 0 at `12:47:26.266723Z` / `12:47:28.886497Z` |
| Exact new-version force deletion | Exit 0 at `12:47:32.826264Z` |
| Independent exact-resource checks | Version, session, agent and session-list GETs all HTTP **404**, completed `12:49:32.516169Z` |
| Web/endpoint | Allowlisted baseline identical; `endpointPersisted:false` |

The platform reused version number `1` after the prior sole version was deleted;
this is a newly created version with a different immutable application SHA, image
digest and session, not reuse of the previous runtime. Initial agent inventory
was explicitly empty. Ownership was recorded before invocation, including the
caller-assigned session ID before session creation.

The control script entered `finally` immediately after the response. Successful
stop/delete/version deletion completed **135.4 seconds** after submission. Its
first verification conservatively failed because Azure CLI omitted the HTTP
status from its `not_found` rendering and post-deletion azd lookups returned
errors without usable absence evidence. Two further bounded cleanup attempts
also returned errors; they did not recreate resources. Independent SDK GETs to the exact
version and documented `/agents/{name}/endpoint/sessions/{id}` paths then
observed HTTP 404 directly, without printing bodies, headers or credentials.
The session-list endpoint also returned 404: this is endpoint absence, **not**
a fabricated empty HTTP-200 page. Verification completed within 4m16s of
submission; no runtime was left running.

**403 diagnosis and limits:** the POST was made by the expected actual ACA MI,
not a developer credential fallback. Its successful preparation GET proves
project-list access only. Subsequent nonbillable ARM projections confirmed the
existing project-scoped **Foundry Agent Consumer** assignment
`ee5a9eb3-9011-50f0-851b-5ca98438c8b1` and inherited account-scoped Cognitive
Services User assignment. The consumer role's current data action is
`Microsoft.CognitiveServices/accounts/AIServices/endpoints/interact/action`.
The account reported `publicNetworkAccess:Enabled`, `networkAcls:null`.
No role or network setting was changed. The probe intentionally discarded the
HTTP error body/headers; no more specific service error code or request ID was
exported. Therefore neither a missing grant, session-owner restriction nor a
runtime/model failure is established. HTTP 403 is not a domain policy refusal.
Do not send another prompt to diagnose it without another explicit approval.

The allowance is now consumed. Model-call and token usage are unobserved, not
asserted zero; enforced source bounds remain three calls, 1800 output tokens per
stage, 20 seconds per stage and 65 seconds overall. No evaluations, image
generation, production changes, new capacity, manual roles, network relaxation,
secret changes, app persistence or endpoint injection occurred. Agent-image
dependencies remain governed by the existing frozen lockfile; no host dependency
installation ran.
Only this run's hosted version/session were deleted. Its new immutable ACR image
and the prior image remain and can incur storage cost, in addition to already
incurred build/compute and any platform telemetry/model charges.

Issue #109 remains open: actual completed domain/schema/version evidence and
runtime model authorization remain unproven; separately reviewed ACA endpoint
injection, web integration and production latency/privacy acceptance remain out
of scope. PR #121 publishes the reviewed probe and truthful run evidence only,
not a claim that the acceptance criteria are complete.

The latest actual ACA-MI probe returned an empty agent inventory; the project
endpoint remains absent from ACA environment configuration. The three
prerequisite grants now exist. The repair above does not write the ACA resource,
and endpoint injection is **not a prerequisite for the one-off MI probe**.

Before any endpoint injection, capture only this safe baseline:

```bash
az containerapp show --resource-group "<existing-dev-rg>" --name "<app-name>" \
  --query "{id:id,revision:properties.latestReadyRevisionName,mode:properties.configuration.activeRevisionsMode,traffic:properties.configuration.ingress.traffic,containers:properties.template.containers[].{name:name,image:image},projectEndpoint:properties.template.containers[].env[?name=='FOUNDRY_PROJECT_ENDPOINT'].value}" \
  --output json
```

Keep this nonsecret baseline outside tracked files. Preserve the exact deployed
image/digest, container names, ingress weights/labels, revision mode and all
existing environment/secret references. Changing an environment value creates
an ACA revision; preserving traffic policy does not mean retaining the same
revision ID. Do not send traffic to an unverified revision.

**Do not run root `azd provision`, including preview, as an endpoint patch.**
Its prehook can generate a session secret, its posthook appends an Entra secret,
and an empty image parameter selects a bootstrap image. Current root
`container-apps.bicep` fixes revision mode to `Single` and does not explicitly
model traffic weights, so it cannot safely replay arbitrary live traffic.

The concrete apply gate is a separately reviewed, hook-free Bicep/azd
configuration update that consumes the verified current image and explicitly
preserves the observed traffic/revision policy and existing secret references.
Review its what-if and reject anything beyond the intended endpoint env value
and unavoidable new revision. If the existing root module cannot represent the
baseline, coordinate that change with its owner **before** applying; this package
intentionally contains no generic whole-resource PUT, root provisioning command
or blind CLI env patch. Re-read image/traffic after apply and compare with the
baseline before approving web traffic. Do not activate agent-backed generation
as part of endpoint injection.

## Independent monitoring and rollback (#99)

Keep release dimensions separate:

- Web application version/revision.
- Agent application build SHA (`CARD_ORCHESTRATOR_VERSION`) and registry digest.
- Foundry agent name + immutable agent version (platform values).
- Existing model deployment name; model version changes are another release.

Before release, prove the integrated runtime emits sanitized request outcome
(success/error/timeout/rejection), elapsed milliseconds and readiness status.
Model use must include deployment name and token counts when the SDK supplies
them; absent usage is unknown, **not zero**. Correlate with random request/trace
IDs and the version dimensions above, never user IDs or raw card text.

Disable content capture in SDK/MAF/OTel instrumentation and verify exported
telemetry with synthetic nonpersonal inputs before enabling exporters. Do not
record prompts, completions, tool arguments/results, card descriptions, photos,
tokens or connection strings. Raw exception bodies can contain model output and
must not be emitted. Do not enable debug/body tracing to diagnose readiness.

Use existing project/Insights telemetry for on-demand outcome/latency/model-use
and startup inspection. Readiness is a local server check, not a model
completion. The container healthcheck uses only `/readiness`; it neither invokes
the model nor represents a Foundry platform probe configuration. No dashboards,
alert rules, recurring evaluations, billable synthetic model probes, ingestion
budget or production SLO have been provisioned by this change.

Keep a release record of commit, digest, Foundry version, configuration and the
last approved version. A rollback is **agent-only**: route an approved caller to
the recorded immutable Foundry version when version pinning is supported, or
deploy the recorded image/configuration as a new agent version through this
isolated azd service. Preserve the original image and app build SHA; record the
new Foundry version separately. Confirm the current extension's version/image
selection interface before executing—do not guess an `azd rollback` command.
The hosted agent endpoint serves one version with 100% traffic; agent-version
traffic splitting is not supported and is not a rollback strategy.
Stop idle/candidate sessions using the supported agent session lifecycle after
approval; retain versions needed for rollback. Do not redeploy the web image or
change its traffic to roll back an independently deployed agent.

An optional single remote smoke request needs explicit model-spend approval
after deployment and must check schema/outcome, not merely HTTP 200. No recurring
probe is authorized. Keep the web path disabled until its separate integration,
safety and rollback review is complete.
