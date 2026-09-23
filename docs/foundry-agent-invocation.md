# Foundry hosted-agent invocation smoke test

This project has an invocation client and a required text-only `card-orchestrator`
runtime. All live card-text generation uses this client; no direct Azure OpenAI
card-text path or automatic fallback remains. Authentication, deterministic
moderation, image generation, and persistence remain in the web backend. See the
[implemented architecture](architecture.md) for the current service boundaries.

## Configuration

Configure these for every live web runtime and when smoke-testing an already
deployed agent:

- `FOUNDRY_PROJECT_ENDPOINT`: canonical project endpoint, for example `https://<account>.services.ai.azure.com/api/projects/<project>`
- `FOUNDRY_AGENT_NAME`: hosted agent name, for example `card-orchestrator`
- `FOUNDRY_AGENT_VERSION`: exact hosted version that must exist and be `active`
- `FOUNDRY_AGENT_API_VERSION`: defaults to `v1`
- `FOUNDRY_AGENT_EXPECTED_VERSION`: optional application metadata check against the agent response `metadata.agentVersion` or `metadata.version`
- `FOUNDRY_AGENT_TIMEOUT_SECONDS`: defaults to `70`, covering the hosted runtime's
  65-second overall budget and bounded to a maximum of 90 seconds. ACA readiness
  allows 100 seconds so the supported maximum still has 10 seconds of bounded
  request/handler overhead within the 225-second request budget.
- `TELEMETRY_ENABLED=true` and `APPLICATIONINSIGHTS_CONNECTION_STRING`: mandatory for live startup.

`FOUNDRY_PROJECT_ENDPOINT` is intentionally separate from `FOUNDRY_ENDPOINT`;
there is no fallback to the account/model endpoint or to direct text generation.

At startup, the web app synchronously validates local configuration and mandatory
telemetry, then begins serving dependency-free `/livez`. Dependency-aware
`/healthz` readiness acquires an Entra token and performs bounded, content-free
checks of both `GET /agents/{name}/versions/{version}` and
`GET /agents/{name}`. The version resource must have the exact configured
name/version and `active` status, and the endpoint's single 100% fixed-ratio
selector must route to that same `FOUNDRY_AGENT_VERSION`.
Only the exact configured version with `status: active` passes. Failure prevents
traffic routing without blocking ASGI startup or causing liveness restart loops.

## Manual smoke command

```bash
python -m app.foundry_agent_client \
  --allow-nonprod-live \
  "Create a safe original fire drake trading card"
```

The command refuses production and requires `--allow-nonprod-live` because it can incur model usage. It prints only a sanitized summary: status, schema validity, request IDs, retryability, and reported agent version. It does not print tokens, prompts, raw response bodies, generated card text, or art prompts.

## Actual ACA managed-identity access probe

The separate operator tool below runs **inside an existing ACA replica**, not
with a developer's credential chain. It does not invoke an agent/model, install
packages, mount secrets, write serving files, or change ACA configuration.
First resolve the existing dev subscription, project endpoint, ACA system
principal, ready revision and running replica using allowlisted ARM queries in
the [operations runbook](foundry-agent-operations.md). Pin those exact values:

```bash
python deployments/card-orchestrator/aca_identity_probe.py \
  --environment dev --subscription "<dev-subscription-id>" \
  --resource-group "<dev-resource-group>" --app "<existing-app>" \
  --revision "<ready-revision>" --replica "<running-replica>" --container web \
  --project-endpoint "https://<account>.services.ai.azure.com/api/projects/<project>" \
  --expected-principal "<ACA-system-principal-id>"
# Add --execute after checking the plan and verified target arguments.
```

This is a dev-only operator guard, not an authorization boundary. It requires
existing permission to `az containerapp exec`, a Linux local host with PTY
support, and the image's existing `/app/.venv/bin/python` plus `azure-identity`.
A replaced/scaled-down pinned replica must be inventoried again, not silently
substituted. No fallback credential or package installation is attempted.

The adjacent `aca_identity_payload.py` is transported as compressed source over
stdin into a short, alarm-bounded Python command. Nothing is written into the
container. The local PTY addresses CLI `Inappropriate ioctl for device`; stdin
avoids the observed exec WebSocket HTTP 404 on long startup commands. No shell,
user content, bearer token or secret is supplied as a command/input argument.
The credential is explicit `ManagedIdentityCredential(client_id=None)`, guarded
by ACA identity endpoint/header presence. Token `oid`, audience and expiry are
compared **in memory**; only the expected-principal match boolean is returned.
Claims inspection is not a replacement for Foundry's server-side validation.

One bounded, no-retry GET goes to the verified project's
`/agents?api-version=2025-11-15-preview`. Redirects and environment HTTP proxies
are disabled. The response is size-bounded; only HTTP status and page count are
exported, never agent names/content, token/JWT, auth headers or raw exceptions.
Credential acquisition has bounded transport timeouts; payload alarm is 45s,
pre-input remote alarm 60s, local exec deadline 75s. The wrapper suppresses CLI
raw output and returns a fixed failure code when no valid evidence arrives.

Success is `status: access_verified`, HTTP 200 and a valid `data` list, including
an empty one. The default access-only mode reports `invocationVerified:false` and
`endpointPersisted:false`. HTTP 400/404 never count as invocation. A 403 is a
real authorization/network failure to investigate without broadening roles or
relaxing network policy. No hosted agent is needed to test MI token/access.

The dev run on 2026-09-08 succeeded with the actual serving ACA system identity:
token acquired, expected principal matched, HTTP 200, zero agents. Full hosted
invocation was not proven by that access check. The first smoke consumed its
single approved allowance. A **separately and newly authorized** second smoke
subsequently sent one Responses request and received HTTP 403; that new allowance
is now also consumed, with no retry authorized. The earlier
App Insights deployment gate was incorrect (linkage is needed for tracing only),
as corrected in the operations runbook.

Those per-attempt statements are historical records, not the current
authorization state. The requester subsequently authorized continued bounded dev
diagnostics. Source `45c03cbfda5a4667d36a73aee0184bcb908bc9f3`
was deployed once and the actual ACA-MI request returned the exact sanitized
boundary diagnostic `card_boundary_invalid_request`,
`serviceReason:"unsupported_field"`, `serviceParam:"agent_reference"`. The owned
session and hosted version were then deleted. See the
[exact execution record](foundry-agent-operations.md#exact-strict-boundary-rejection--2026-09-09).

The next exact-source E2E deployed reviewed source
`d2de0b358a9d665a2d63d5b5fb74119b4a77edec` and proved the correction live:
the actual ACA MI created and readied its exact-version session, the Responses
POST passed the corrected `agent_reference` boundary, and the service returned
HTTP 200. The envelope itself had `outcome:"failed"`,
`schemaValid:false`, and `invocationVerified:false`; it contained no valid card,
held result or refusal, so neither application nor hosted response-version
matching was established. Exactly one Responses POST was sent with no retry.
The owned session/version were deleted and both exact GETs returned 404; the web
baseline and `/healthz` HTTP 200 were preserved. See the
[latest exact execution record](foundry-agent-operations.md#exact-source-runtime-failure--2026-09-09).

Un **troisième smoke, nouvellement autorisé**, a depuis déployé la correction
à identité unique depuis `dc1925942c42756690f7dd5321cbdbf892cf7182`.
Sa préparation ACA a réussi (MI attendue, HTTP 200, imports/contrat/fixture prêts,
zéro POST), mais l'unique dispatch `--invoke-once` a renvoyé le diagnostic local
`exec_setup_timeout` sans marqueur distant. Le nombre de créations/Responses
effectivement arrivés est **inconnu**, pas zéro ou un confirmé ;
`invocationAllowanceConsumed:true`, aucune relance. Aucun HTTP ni code service
n'a été observé pour cette tentative. La nouvelle version a été supprimée et
les GET exacts version/session ont confirmé 404. L'image ACR reste conservée ;
le web est inchangé et `endpointPersisted:false`.
Les [preuves horodatées et la divergence d'horloges](foundry-agent-operations.md#smoke-corrigé-à-identité-unique--2026-09-08)
distinguent cette panne de transport du HTTP 403 historique. La création de
session par la MI et le résultat métier restent non démontrés ; aucune nouvelle
tentative facturable n'est autorisée.

### Nonbillable invocation preparation

Use the same pinned target arguments above with `--prepare-invocation --execute`.
Do **not** supply `--invoke-once`, versions or a session: conflicting/unused
invocation arguments fail locally before ACA exec. No hosted version, session or
server is required.

#### Proving the persisted endpoint

Add `--require-persisted-endpoint` to access-only, `--prepare-invocation`, or
`--invoke-once` mode. The probe reads **only** `os.environ["FOUNDRY_PROJECT_ENDPOINT"]`
inside the pinned ACA process (no dotenv/file reads). Environment configuration
is untrusted: it must pass the existing canonical Azure project URL validation
and exactly match the operator-verified `--project-endpoint` before credentials,
network requests, or session creation. Missing/malformed and mismatched values
produce only `persisted_endpoint_invalid` and `persisted_endpoint_mismatch`;
the environment value is never printed. Credentials, arbitrary URLs, redirects,
query strings and environment proxies are not accepted as project destinations.

Only after that actual comparison does request construction use the persisted
value and emit `endpointPersisted:true` with `endpointSource:"aca_environment"`.
The wrapper requires these fields to agree and, when opted in, rejects successful
legacy markers without persistence proof. The flag requests verification; it
cannot supply a boolean assertion of persistence. Default usage still reports
`endpointPersisted:false`, meaning persistence was not checked, not that the
environment variable is necessarily absent. Historical results remain unchanged.

Use `--prepare-invocation --require-persisted-endpoint --execute` for the
nonbillable gate: a successful `invocation_prepared` marker also requires the
actual expected MI principal, HTTP 200 with a valid agents list, and the bundled
parser/request/local-fixture checks. It sends one GET and **zero POSTs**.
Endpoint matching alone never proves access or invocation; preparation is not
hosted generation, domain validation of a service response, or E2E completion.
Invocation keeps the existing 30s setup / 65s remote invocation budget (95s
remote total, 130s local bound), same-MI session ownership, and no retries.

**Actual nonbillable persisted-endpoint preparation, 2026-09-09 UTC**
(evidence recorded at `06:26:33Z`; this is not the historical 2026-09-08 run):
fresh allowlisted management GETs selected healthy/running revision
`fcag-dev-app--endpoint-ea77f0bf2596`, ready replica
`fcag-dev-app--endpoint-ea77f0bf2596-5f75998d86-nwvzw`, container `web`.
The expected system principal was `946d8701-48f2-4fa5-8efd-bf053c7b4e4c`;
Single mode, 100%-latest traffic and the existing
`web-nat-dev:azd-deploy-1788775195` image were observed unchanged.
One `--prepare-invocation --require-persisted-endpoint --execute` completed:

```json
{"accessVerified":true,"agentCountOnPage":0,"endpointPersisted":true,"endpointSource":"aca_environment","httpStatus":200,"invocationVerified":false,"invocationsAttempted":0,"localFixtureParseReady":true,"parserImportReady":true,"preparationOnly":true,"principalMatched":true,"requestSchemaReady":true,"schemaValid":false,"status":"invocation_prepared","tokenAcquired":true}
```

The verified persisted value matched
`https://aifcagdevqhg3qc4rlbt4g.services.ai.azure.com/api/projects/fantasy-cards-dev`.
This preparation sent one agents GET, zero session-create/Responses POSTs and
created no hosted runtime. No app configuration, remote files or packages changed.
Persistence and GET access are now proven inside ACA; real hosted E2E remains
pending the separately reviewed bounded invocation.

This explicit mode uses the **same** owned parser/request-model bundle,
`app.generation.GeneratedCardModel` import, chunked stdin transport and phased
deadlines as invocation. It builds the same `GenerateCardAgentRequest`/Responses
body in memory with a clearly local placeholder session, exercises the parser
against a labelled local valid card fixture and an invalid empty envelope, then
acquires the actual explicit `ManagedIdentityCredential`, checks principal,
audience and expiry, and sends **one GET agents only**. It never sends a
Responses POST or calls a model, including when invocation arguments are malformed.
Bytecode writes are disabled in the remote process; no remote files, packages,
mounts, app settings or cloud resources are created or modified.

`invocation_prepared` requires `parserImportReady`, `requestSchemaReady`,
`localFixtureParseReady`, and real MI `accessVerified` with HTTP 200.
`preparationOnly` is true; `invocationsAttempted` is zero;
`invocationVerified` and service `schemaValid` remain **false**. No fixture content,
service body, token or raw CLI output is exported. The local fixture is **not
hosted-service acceptance evidence**, and this mode neither consumes nor grants
a paid invocation allowance.

The explicit `--invoke-once --hosted-version <version> --expected-version <full-sha>
--session-id <new-prerecorded-id>` mode first creates its **own** exact-version
session inside ACA, then sends at most one synthetic Responses request. Both use
one explicit system `ManagedIdentityCredential` and the same checked token in
memory. The operator must pre-record a never-used `smoke-109-<uuid4().hex>` ID and
the exact request-source/build/version bindings before dispatch, not create an
operator-owned warmup session. The create request is:

```text
POST {project}/agents/card-orchestrator/endpoint/sessions?api-version=v1
{"agent_session_id":"smoke-109-<32 lowercase hex characters>","version_indicator":{"type":"version_ref","agent_version":"<exact new hosted version>"}}
```

Only the documented **HTTP 201** `AgentSessionResource` is accepted. Its
`agent_session_id` and `version_indicator.type/agent_version` must match the
request; guessed `metadata.version` is not used. Only `status:active` permits
inference. `creating`/`updating` cause bounded GETs to
`.../endpoint/sessions/{id}?api-version=v1` (two-second intervals, at most 15 GETs,
within the setup deadline), revalidating ID/version every time. Other statuses,
malformed payloads, mismatches, HTTP failures and timeouts stop without inference.
Neither POST is retried. A conflict (409) is not accepted as a replacement session
and explicitly does **not** authorize deleting that existing session.

The Responses body uses the documented **`agent_session_id`**, not the unverified
legacy `session_id` alias, with the same ID and existing `store:false`,
`stream:false`, structured `schemaVersion:1` request. No Foundry-Features,
impersonation or isolation-header override is added. The owned response parser is bundled
in memory from source, imports the existing `GeneratedCardModel`, and validates
both build and hosted version metadata. No container files or settings are
written. Output contains only allowlisted status/booleans/IDs/versions; no cards,
model text or tokens. `sessionCreateAttempted` (boolean) and `invocationsAttempted`
(0/1) are distinct. `sessionCreated` means matching create/GET resource evidence;
`sessionReady` additionally requires `active`. `sessionCleanupRequired` is set
**before** create dispatch, so a timeout is reconcilable even without a response.
The HTTP diagnostic exports only `httpStatus`, `phase`
(`session_create|session_ready|invoke`), and `serviceCode`
(`session_not_accessible|invalid_request|card_boundary_invalid_request|unknown`).
For the boundary-specific code only, fixed `serviceReason`/`serviceParam` enums
are also exported. Error JSON reads are bounded to 64 KiB plus one overflow byte;
arbitrary codes/messages/body/headers/URLs are never exported.
No optional telemetry is required.

A dispatch timeout consumes the allowance; never retry. Without a strict remote
marker, local output conservatively sets `sessionCreationUnknown:true`,
`sessionCleanupRequired:true` and `invocationAllowanceConsumed:true`, not a
guessed attempt count. The operator's mandatory `finally` must reconcile the
pre-recorded ID and remove only this run's owned session/version, including
unknown creation completion. See the [cleanup contract](foundry-agent-operations.md#next-separately-approved-window-same-identity-session-contract).
Large invocation payloads are sent in lines of at most 1024 characters and
reconstructed in memory to respect canonical terminal limits.
`invocation_verified` can mean a validated `held` or `refused` result, not card
generation success; inspect `outcome`. Invocation mode allows **30 seconds** from CLI
launch for connection, terminal settling and complete payload delivery, followed
by **100 seconds** for a result, with a hard **130-second** local transport cap.
The earlier 10-second setup cap produced `exec_setup_timeout` during the latest
live attempt. Setup now matches the successful preparation path without reducing
the result budget. The deadline correction was initially exercised only offline. It was later used
successfully by the bounded live diagnostic recorded above; transport completed
and the strict application boundary identified `agent_reference` as an
unsupported top-level field.
Its remote budget is **95 seconds**: at most **30 seconds** for decoding,
parser/import, MI, session creation and readiness, separately reserving the
**65-second** invocation guard (including response parsing). Setup failure never
starts inference; cold imports cannot consume the model's reserved budget.
Preparation retains its existing 30/80/110-second local and 70-second remote limits.
A diagnostic
SIGALRM handler is installed **before** chunked input, bounded at 30 seconds.
After input, the 95-second invocation (70-second preparation) guard protects
decoding/source entry. The payload preserves the remaining bootstrap budget
**before importing the parser**, rather than resetting it after imports.
Cold imports consume the bounded setup budget instead of
silently dying under a separate five-second default signal. Parser setup failures
emit sanitized `parser_setup_failed`/`parser_setup_timeout`; early transport/source
failures emit `bootstrap_failed`/`bootstrap_timeout`. PTY writes are
nonblocking and remain within setup/total deadlines, including partial transfers.
Repeated connection messages never retransmit the payload. Local failures are
sanitized as `exec_setup_timeout`, `exec_timeout` (result), or `exec_total_timeout`;
process termination/reaping has a separate bounded cleanup wait. Access-only mode
retains its 75-second launch-to-result budget. These offline deadline corrections
do not establish the cause of the historical `exec_no_evidence` result or permit
a retry. An offline subprocess regression demonstrates that the former default
five-second SIGALRM kills a 5.2-second simulated cold import with no marker;
the new path survives that delay and emits a structured diagnostic.

```bash
python -m pytest -q --noconftest tests/test_aca_identity_probe.py \
  tests/test_foundry_agent_client.py tests/test_hosted_agent_deployment_config.py \
  tests/test_deployment_config.py
```

Contract sources checked 2026-09-08:
[session API and protocol binding](https://learn.microsoft.com/azure/foundry/agents/how-to/manage-hosted-sessions),
[caller-Entra ownership](https://learn.microsoft.com/azure/foundry/agents/how-to/isolate-sessions-per-user#troubleshoot-isolation),
[interaction permissions](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-permissions#agent-interaction),
and the public SDK's
[`AgentSessionResource` / `VersionRefIndicator`](https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/ai/azure-ai-projects/azure/ai/projects/models/_models.py)
and [`create_session` HTTP 201 contract](https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/ai/azure-ai-projects/azure/ai/projects/operations/_operations.py).
The existing project-scoped Foundry Agent Consumer interaction role is retained;
own-session creation with that role remains **unverified live**, not justification
for broader RBAC.

## Wire contract

The hosted boundary accepts two optional routing-only fields:

- `agent_session_id`: 1-128 ASCII letters, digits, dots, underscores or hyphens.
- `agent_reference`: the pinned AgentServer 2.1.0 `AgentReference` shape,
  exactly `{"type":"agent_reference","name":"<non-empty string>","version":"<string>"}`
  with `version` optional.

Both are validated and discarded before SDK normalization: neither is prompt
input, enables conversation history/response storage, nor replaces Foundry's
session-ownership authorization. Unknown keys or malformed routing values still
fail closed. An offline regression adds the supported platform reference to the
actual probe-built body and sends it through the real pinned SDK host with fake
specialists. The deployed `45c03cb` boundary rejected that live field before the
SDK/model; the new regression fixes that exact mismatch without claiming hosted
success.

Pinned SDK inspection also covered its other identity resolution inputs:
`response_id` and the `x-agent-response-id` header affect response correlation,
while `agent_session_id` selects session affinity. The current probe sends no
`response_id`, and the live diagnostic proved no additional rejected field.
Those surfaces remain unsupported rather than being speculatively allowlisted.
Standard Responses controls such as `model`, `instructions`, `conversation`,
`tools`, and persistence/streaming overrides remain strict caller inputs and are
not treated as routing metadata.

The boundary now returns `card_boundary_invalid_request` only when its own
strict validation rejects the container request, together with allowlisted
`serviceReason` and `serviceParam` enums. The probe exports only those fixed
values; it never exports request values, error messages or bodies. A subsequent
live result with that code proves the request reached the container boundary and
identifies the rejected field/category. A generic `invalid_request` instead
remains upstream-platform or inner-SDK evidence, not proof of boundary rejection.

The client uses Microsoft Entra ID with the `https://ai.azure.com/.default` token scope and posts to the documented hosted-agent Responses protocol endpoint:

```text
POST {projectEndpoint}/agents/{agentName}/endpoint/protocols/openai/responses?api-version=v1
```

Request body:

```json
{
  "store": false,
  "stream": false,
  "input": [
    {
      "role": "user",
      "content": [
        {
          "type": "input_text",
          "text": "{\"schemaVersion\":1,\"query\":\"...\"}"
        }
      ]
    }
  ]
}
```

The sole message contains `GenerateCardAgentRequest` JSON: `schemaVersion: 1` and
a trimmed `query` of 1–400 characters. No user ID, photo bytes, tool definitions,
or tool executions are sent.

The response parser reads the raw Responses wire envelope `output[]/content[]/output_text`, detects refusals before schema validation, treats incomplete or malformed output as non-success, and validates the proposed agent payload:

- `schemaVersion`
- `status`
- `card` using the existing `GeneratedCardModel`
- `artPrompt`
- `metadata`
- `safetyHints`

## Current live gap

The latest bounded dev diagnostic deployed exact reviewed source
`22aa53bc5678437cf6b4e8507220b7ed36e04ead`. Same-identity session creation and
readiness succeeded, and the single ACA-MI Responses POST returned HTTP 200 with
a failed envelope carrying the closed runtime tuple
`art_direction/rate_limited/rate_limit/http_429`. This precisely identifies a
provider HTTP 429 at the art-direction stage, not an authorization,
configuration, routing or schema failure. No POST retry, RBAC change, production
operation or evaluation occurred. The owned session and hosted version were
deleted; exact GETs returned 404; the unchanged web baseline returned
`/healthz` HTTP 200. See the
[exact classified runtime evidence](foundry-agent-operations.md#exact-classified-runtime-failure--2026-09-09).

At the historical checkpoint below, the same-identity session-creation/protocol
correction was **offline code, not yet deployed or live-tested**, and the last
deployed image was
`2bdbf9967d8c397f7d88914bac06285b3b477297`. Current documentation verifies
caller-scoped session ownership and `agent_session_id` binding. Those facts make
the operator-created/ACA-invoked session a concrete protocol defect to correct,
but do **not** prove the historical HTTP 403 cause: its error body was discarded,
and legacy alias acceptance was never established. No further Azure request is
authorized by this correction; #109 stays open.

The latest **newly authorized** smoke on 2026-09-08 used application build
`2bdbf9967d8c397f7d88914bac06285b3b477297`. Before deployment, the complete current
parser/chunk-transport preparation returned `invocation_prepared`: all three
parser/request/fixture readiness booleans true, actual ACA principal matched,
GET agents HTTP 200, zero invocations.

The dedicated azd deployment created a new version `1` and an active exact-version
session. The one actual ACA-MI Responses POST returned **HTTP 403**:
`tokenAcquired:true`, `principalMatched:true`, `invocationsAttempted:1`,
`reason:http_error`, `invocationVerified:false`, `schemaValid:false`.
`accessVerified:false` belongs to this rejected POST, not the successful
preparation GET. There is no validated domain outcome or application/hosted-version
match; HTTP authorization failure is **not** a domain `refused` card response.
No retry or developer-credential invocation occurred.

Session stop/delete and exact-version deletion completed within **2m16s** of
deployment submission. Independent exact session/version GETs returned HTTP 404.
The web baseline remained identical; `endpointPersisted:false`. Both images may
remain billable in ACR. The existing consumer role was verified, not widened;
the precise service authorization reason was not exported by the privacy-bound
probe and cannot be inferred from 403 alone. End-to-end generation remains
unproven. See the [new run evidence](foundry-agent-operations.md#newly-authorized-dev-smoke--2026-09-08).

The earlier approved smoke deployed `card-orchestrator` version `1`, then deleted it and
its session in the cleanup path. Its single ACA invocation dispatch returned
`exec_no_evidence`: Responses delivery, identity and card validation are unknown,
not successful. The allowance was consumed and no retry occurred. Exact version,
session and agent GETs subsequently returned 404; the web baseline was unchanged.
See the [actual run evidence](foundry-agent-operations.md#executed-approved-dev-smoke--2026-09-08).
Passing unit tests or mock transports is not evidence of live end-to-end success.

On 2026-09-08, nonbillable preparation on the existing serving revision
`fcag-dev-app--azd-1788775203`, container `web`, image
`azd-deploy-1788775195`, returned the strict `invocation_prepared` marker:
parser import, generated request schema and local fixture parsing all ready;
actual system principal matched; GET agents HTTP 200, empty page; **zero
invocations**, `invocationVerified:false`, `schemaValid:false`. This verifies
the deployed web image's parser/import compatibility and the complete preparation
collection path, not delivery to or generation by a hosted service. No Responses
POST, deployment, hosted session creation or model call was made. The previous
single paid allowance remained consumed at that checkpoint. The later, separately
authorized attempt above is distinct and is also now consumed.

The earlier 2026-09-08 checkpoint stopped without attempting deployment.
Missing Application Insights linkage was incorrectly called a deployment blocker;
it is not required for this instrumentation-disabled smoke. The earlier actual
ACA-MI access probe is not an invocation result. The operations runbook records
the subsequent actual deployment outcome separately. Never substitute developer
credentials for the authorized ACA invocation.

## Hosted runtime (offline candidate, issue #109)

Python 3.12 entrypoint and optional dependency installation:

```bash
uv sync --frozen --extra hosted-agent --group dev
uv run --frozen --extra hosted-agent python -m hosted_agents.card_orchestrator
```

Ordinary web installs omit `--extra hosted-agent` and do not install the hosting
stack. The lock pins `azure-ai-agentserver-responses==2.1.0`,
`agent-framework-core==1.17.0`, and `agent-framework-foundry==1.12.0`.
The hosting SDKs now use the existing Microsoft package proxy, which supplies
these exact versions and artifact hashes. The obsolete `hosted-sdk` PyPI
override was removed so frozen installs do not fetch from the unreachable
`files.pythonhosted.org` path in restricted build environments. See
[build TLS troubleshooting](foundry-agent-operations.md#build-time-package-tls-failures).

The runtime reads **only environment configuration**, never `.env`, web settings,
Key Vault, storage credentials, or the account-scoped `FOUNDRY_ENDPOINT`:

| Variable | Contract |
|---|---|
| `FOUNDRY_PROJECT_ENDPOINT` | Required canonical HTTPS project endpoint |
| `AZURE_AI_MODEL_DEPLOYMENT_NAME` | Required existing text deployment |
| `CARD_ORCHESTRATOR_VERSION` | Required immutable application release ID (1–64 safe identifier characters) |
| `CARD_ORCHESTRATOR_MODERATION_POLICY` | Optional; only `original-fantasy-v1` is supported; cannot disable local checks |
| `CARD_ORCHESTRATOR_STAGE_TIMEOUT_SECONDS` | Default/max 20 seconds, positive and finite |
| `CARD_ORCHESTRATOR_TIMEOUT_SECONDS` | Default/max 65 seconds, positive and finite |

The host listens on **8088**, with `POST /responses` and `GET /readiness`.
Imports, startup and readiness perform no cloud requests. Readiness reports
process readiness, **not** identity, quota, model availability or deployment health.
Model access is deferred until an allowed request, uses a project-scoped
`AIProjectClient`/MAF `FoundryChatClient`, and authenticates through an owned
noninteractive `DefaultAzureCredential`. No keys are required.

`metadata.agentVersion` is `CARD_ORCHESTRATOR_VERSION` and must equal the operator's
`FOUNDRY_AGENT_EXPECTED_VERSION` when configured. The platform-provided
`FOUNDRY_AGENT_VERSION`, if present and a bounded identifier, is separately reported
as `metadata.hostedVersion`; it is not the application-version check.

### Bounded orchestration and safety

Approved [#164 r1](architecture-agents-foundry.md#approved-workflow-engine-contract-164-r1)
moves the following stages into a real per-request `WorkflowBuilder` graph, with
three `AgentExecutor` specialists executed via `WorkflowAgent` and deterministic
typed decode/merge/safety nodes. This describes the current r1 PR candidate,
not evidence of a deployed graph. `ResponsesAgentServerHost` and
the pinned dependencies above remain; the sample's `ResponsesHostServer` is not
adopted. Inputs are accumulated validated cards, not raw `last_agent` output.

1. Heuristically moderate the original input.
2. A fresh MAF Concept agent produces `GeneratedCardModel`; validate and moderate it.
3. A fresh Lore agent receives that validated card and returns **only** `name` and
   `flavorText`; merge, revalidate and moderate before art direction.
4. A fresh Art Direction agent receives the lore-refined validated card and returns
   **only** `artBrief`; merge/revalidate without changing mechanics.
5. Derive `artPrompt` using the existing `derive_art_prompt()` and moderate both the
   final card text and the derived prompt.

Maximum **three model requests**, with model retries and function-invocation loops
disabled, no repair loops and no automatic fallback. All agents/sessions are fresh
per stage and clients are owned/closed per invocation, including cancellation.
The graph, executors and mutable evidence/card state are also request-local;
no Workflow conversations, checkpoints, persistent state or fan-out are enabled.
Pre-prompt rejection acquires no specialist clients. The request task owns client
acquisition/closure through graph event handshakes; closure precedes final safety
and mandatory terminal serialization, and cancellation drains owned graph work.
The 20-second stage / 65-second orchestration budgets fit inside the web client's
bounded 70-second hosted-agent timeout. The complete card-generation request
remains bounded by the 225-second outer deadline.
The same absolute HOSTED deadline covers scheduling, acquisition, closure,
serialization/revalidation and diagnostic preflight. The
[requester performance waiver](https://github.com/bmoussaud/fantasy-cards-generator/issues/164#issuecomment-5778665499)
does not relax these limits or privacy/capture bounds. No r2 startup preparation
is implemented.

Completed domain JSON is returned in the SDK's `TextResponse`, inside a genuine
Responses API envelope. Only the deterministic terminal envelope is output-eligible;
internal messages, graph events and intermediate JSON must never reach the wire,
including when a later stage fails. `refused`, `held` and `routing_defer` always omit content
(`card: null`, `artPrompt: null`). Reasons are bounded codes, not rejected content.
Schema failures or invalid/missing local safety evidence are held. Observed model
refusal/content-filter evidence takes precedence over generated JSON. Dependency
errors and timeouts produce an outer `response.failed`/`server_error`, not a
completed domain result. Malformed/unsupported requests return sanitized HTTP 400.

### Completion diagnostics (#166)

For the four existing completion-check failures only, HOSTED may add
`metadata.completionDiagnostics`. It is built from immutable, validated,
request-local state after resource closure and metadata replacement, never by
forwarding arbitrary SDK or terminal metadata. WEB validates this optional member
after domain-schema validation and before its held-result return; it remains on
the internal invocation result and the existing `agent.invocation` event only.
Older responses without it remain valid. Public held behavior is unchanged:
HOSTED HTTP 200 with `held` maps to WEB HTTP 502 / `invalid_model_output`, with the
same generic error body and audit/replay behavior.
The existing ACA identity probe's in-memory parser bundle includes the same closed
diagnostic definitions, so it does not require this new module on an older serving
image. Its result-marker contract does not expose completion diagnostics.

Required `stage` is `concept|lore|art_direction`; required `checker` is
`unexpected_agent_response|missing_raw_response|non_stop_finish|non_text_content`.
An invalid required value discards diagnostics only. Optional `finishReason` is
`stop|length|content_filter|tool_calls|function_call|other`; optional
`incompleteReason` is `max_output_tokens|content_filter|other`. Optional `usage`
contains only `inputTokens`, `outputTokens`, `totalTokens`, each an exact integer
(not bool) in 0..2147483647. Invalid counts are omitted independently, without
coercion, clamping or inferred totals. Unknown strings in verified reason fields
map to `other`; absent or malformed ancillary fields are omitted. Unknown keys
never survive projection, including nested usage extras.

Offline source/type verification against `agent-framework-core==1.17.0`,
`agent-framework-foundry==1.12.0`, its locked `agent-framework-openai==1.14.1`,
`azure-ai-agentserver-responses==2.1.0`, `azure-ai-projects==2.3.0` and
`openai==2.54.0` established these public paths:

- `AgentResponse.finish_reason` is the framework's string finish reason.
  The Responses adapter maps `max_output_tokens` to `length`, `content_filter`
  to `content_filter`, completed function calls to `tool_calls`, and otherwise
  completed responses to `stop`. Unrecognized/noncompleted status may yield no
  finish reason. `function_call` is an allowed diagnostic bucket, not a claim
  that this adapter produces it.
- `AgentResponse.usage_details` copies `ChatResponse.usage_details`, with
  `input_token_count`, `output_token_count`, `total_token_count` copied from the
  public OpenAI `Response.usage` token fields. Only these three keys are read.
- `AgentResponse.raw_representation` wraps a `ChatResponse`, whose public
  `raw_representation` is the OpenAI `Response`; its typed `incomplete_details`
  (`IncompleteDetails`) exposes `reason`. This optional extraction requires those
  verified types. Unsupported wrappers are not traversed and no private payload
  fallback is used.

Refusal/content-filter and provider-error precedence are unchanged. Missing raw
response still wins over non-stop finish; non-stop finish still precedes
non-text content. Completed-text acceptance and subsequent JSON/schema checks
are unchanged. These observations do not establish truncation, token exhaustion,
a framework defect, or the cause of any historical trace. No original content,
provider identifiers, errors or raw provider strings are retained by diagnostics.
For the closed telemetry keys and capture guards, see
[operational monitoring](operational-monitoring.md#content-free-completion-diagnostics-166).

`metadata.safetyEvidence` records bounded stage/policy/decision/reason codes for
the local gates. Completion requires allowed evidence for pre-prompt, concept,
lore, final-text and final-art-prompt checks. Hosted guardrails are recorded as
**unavailable/not observed**, never falsely passed; post-image is **not applicable**.
The reused heuristics are narrow pattern checks, **not comprehensive safety,
copyright detection, or prompt-injection protection**. Live guardrail integration,
quality evaluation and adversarial safety review remain release gates.

### Nonpersistence and telemetry boundary

Raw HTTP validation runs before SDK normalization/history dispatch. It requires
explicit `store:false` and `stream:false`; rejects background, conversations,
history references, caller instructions/tools/models, images/files and multiple
messages/parts. Only optional bounded `request_id`/`trace_id` metadata identifiers
are accepted, then discarded rather than passed to specialists. Unknown metadata
keys and duplicate JSON keys are rejected. Request bodies are capped at 8192 bytes.

Model calls also use `store:false`. An explicit fail-closed response provider
replaces SDK 2.1.0's default file/Foundry-backed storage. Durable tasks, background
recovery and steering are not enabled. Response retrieval/cancel/history routes
are inaccessible; no request/response caches, file writes or conversation retrieval
are used. The lazy static-contract cache contains only immutable application-owned
schema/instruction strings, with a fresh schema dictionary for every Agent.
HTTP disconnect and shutdown cancellation propagate to owned model work.

The SDK host defaults can capture sensitive telemetry; this runtime explicitly
disables host observability setup, automatic MAF payload instrumentation and
SDK/access payload logging. Baseline telemetry remains content-free. The narrow
#162 exception permits only bounded, sanitized, accepted application-owned detail
on the three original specialist spans, after resource closure, serialization and
whole-batch preflight; it is not raw prompt/output logging. #164 preserves those
original spans and their measured boundaries across SDK task contexts, rather than
substituting Workflow/provider spans. See the
[exact release and privacy contract](operational-monitoring.md#release-ownership-source-links-and-compatibility).
This application-layer behavior is **not** a promise that the hosted platform or
model provider retains no service telemetry.

### Offline validation

```bash
uv run --frozen --extra hosted-agent pytest \
  tests/test_card_orchestrator.py tests/test_card_orchestrator_models.py \
  tests/test_foundry_agent_client.py tests/test_workflow_integration.py -q
```

These tests exercise the installed stable SDK host through the existing operator
parser and real MAF/Foundry/OpenAI clients over mocked model HTTP, plus moderation,
refusal precedence, invariants, cancellation, timeouts and request isolation.
They make no live Azure requests and are not deployment or creative-quality evidence.
For #164, fake-specialist tests or import checks alone are insufficient: the
[additional acceptance requirements](agent-evaluation.md#workflow-migration-acceptance-164-r1)
require real scheduler/edge execution and preserve the #162 privacy/lifecycle
regressions. No passing result for the new graph is asserted here.
