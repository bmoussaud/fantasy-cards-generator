# Foundry hosted-agent invocation smoke test

This project has an invoke-only client and an opt-in text-only `card-orchestrator`
runtime. Neither is wired into card generation startup or `/generate`; the existing
direct Azure OpenAI flow remains unchanged.

## Configuration

Set these only when an operator wants to smoke-test an already deployed agent:

- `FOUNDRY_PROJECT_ENDPOINT`: canonical project endpoint, for example `https://<account>.services.ai.azure.com/api/projects/<project>`
- `FOUNDRY_AGENT_NAME`: hosted agent name, for example `card-orchestrator`
- `FOUNDRY_AGENT_API_VERSION`: defaults to `v1`
- `FOUNDRY_AGENT_EXPECTED_VERSION`: optional application metadata check against the agent response `metadata.agentVersion` or `metadata.version`
- `FOUNDRY_AGENT_TIMEOUT_SECONDS`: defaults to `5.0`

`FOUNDRY_PROJECT_ENDPOINT` is intentionally separate from `FOUNDRY_ENDPOINT`; there is no fallback to the account/model endpoint.

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
an empty one. It **always** reports `invocationVerified:false` and
`endpointPersisted:false`. HTTP 400/404 never count as invocation. A 403 is a
real authorization/network failure to investigate without broadening roles or
relaxing network policy. No hosted agent is needed to test MI token/access.

The dev run on 2026-09-08 succeeded with the actual serving ACA system identity:
token acquired, expected principal matched, HTTP 200, zero agents. Full hosted
invocation remains unproven. The subsequent bounded smoke was cost-approved,
but stopped before publication because the required project Application Insights
binding is absent; see the [approved-smoke checkpoint](foundry-agent-operations.md#approved-temporary-smoke-blocked-before-publication--2026-09-08).

```bash
python -m pytest -q --noconftest tests/test_aca_identity_probe.py \
  tests/test_hosted_agent_deployment_config.py tests/test_deployment_config.py
```

## Wire contract

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

No hosted `card-orchestrator` agent is deployed by this branch. Passing unit tests or mock transports is not evidence of live end-to-end success; a real smoke test requires Gimli's infra/RBAC work and an existing configured agent endpoint.

On 2026-09-08 the authorized temporary live smoke reached a real readiness
blocker: neither project nor account inventory included an Application Insights
connection. The existing Insights resource alone does not establish platform
injection. No image publication, hosted compute, or invocation attempt occurred.
The earlier actual ACA-MI access probe is not an invocation result. Do not
reinterpret this as denied cost authorization or retry with developer credentials.

## Opt-in runtime (offline candidate, issue #109)

Python 3.12 entrypoint and optional dependency installation:

```bash
uv sync --frozen --extra hosted-agent --group dev
uv run --frozen --extra hosted-agent python -m hosted_agents.card_orchestrator
```

Ordinary web installs omit `--extra hosted-agent` and do not install the hosting
stack. The lock pins `azure-ai-agentserver-responses==2.1.0`,
`agent-framework-core==1.17.0`, and `agent-framework-foundry==1.12.0`.
The explicit `hosted-sdk` PyPI source supplies SDK versions absent from the normal
package proxy; the normal package source remains unchanged.

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
The 20-second stage / 65-second orchestration budgets are an **OFFLINE CANDIDATE**:
they do **not** meet, replace or provide evidence for the proposed production
8.15-second agent-hop / 30.15-second degraded-direct-path latency budgets. The
operator's default 5-second timeout is intentionally unchanged; any later approved
nonproduction live trial needs its own explicit deadline.

Completed domain JSON is returned in the SDK's `TextResponse`, inside a genuine
Responses API envelope. `refused`, `held` and `routing_defer` always omit content
(`card: null`, `artPrompt: null`). Reasons are bounded codes, not rejected content.
Schema failures or invalid/missing local safety evidence are held. Observed model
refusal/content-filter evidence takes precedence over generated JSON. Dependency
errors and timeouts produce an outer `response.failed`/`server_error`, not a
completed domain result. Malformed/unsupported requests return sanitized HTTP 400.

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
are inaccessible; no application caches, file writes or conversation retrieval
are used. HTTP disconnect and shutdown cancellation propagate to owned model work.

The SDK host defaults can capture sensitive telemetry; this runtime explicitly
disables host observability setup, MAF instrumentation and SDK/access logging.
It does not export prompts, model outputs, tokens, raw exceptions or user content.
This application-layer behavior is **not** a promise that the hosted platform or
model provider retains no service telemetry.

### Offline validation

```bash
uv run --frozen --extra hosted-agent pytest \
  tests/test_card_orchestrator.py tests/test_card_orchestrator_models.py \
  tests/test_foundry_agent_client.py -q
```

These tests exercise the installed stable SDK host through the existing operator
parser and real MAF/Foundry/OpenAI clients over mocked model HTTP, plus moderation,
refusal precedence, invariants, cancellation, timeouts and request isolation.
They make no live Azure requests and are not deployment or creative-quality evidence.
