# Agent architecture with Microsoft Foundry and Microsoft Agent Framework

This document proposes how `fantasy-cards-generator` can introduce agent-based capabilities while preserving the current FastAPI + Azure Container Apps application shape.

The direction is intentionally conservative: keep authentication, rate limiting, persistence, and HTTP/UI behavior in the existing web application, and add a Foundry-hosted agent layer only where agent reasoning adds value.

## Executive summary

The current application already has a clean generation pipeline: authenticate the user, validate the request, moderate the prompt, generate structured card text, derive an art prompt, generate or edit artwork, moderate the result, then persist metadata and image assets.

That pipeline works, but its creative reasoning is currently concentrated in one text-model call plus deterministic Python glue. Introducing Microsoft Foundry agents gives the application a better place to hold multi-step creative reasoning, card-design critique, and prompt refinement without moving user/session ownership or persistence into model code.

**Recommended target architecture:**

- keep the existing FastAPI application in Azure Container Apps as the public API/UI surface
- add **one Microsoft Foundry hosted agent** built with the **Microsoft Agent Framework (MAF)** as the orchestration entrypoint for agentic reasoning
- implement the initial specialist behaviors as **MAF skills inside the hosted agent**
- optionally externalize mature specialists into separate **Foundry prompt agents** later if independent versioning is needed
- keep image generation, image-edit retry semantics, Blob/Cosmos persistence, and most request policing in the existing backend

In short: use agents for **creative orchestration**, not for **core application control-plane concerns**.

## Why agents fit this application

Agent-based reasoning is useful here because card generation is not only a single completion problem. The application must:

- translate a loose player prompt into a coherent fantasy-card design
- keep the output structured and game-shaped
- generate original lore/flavor text without drifting into copyrighted or unsafe content
- refine an image prompt that is strong enough for image generation/editing
- support future extensions such as critique/rewrite loops, house-style enforcement, and offline evaluation

A hosted agent is a better long-term home for those responsibilities than adding more prompt-shaping logic directly into `app/generation.py`.

## Current architecture recap

### Application layer today

The current application is a Python FastAPI service loaded through `app/entrypoint.py` and assembled in `app/main.py`.

The main generation path is:

1. authenticated request reaches `POST /api/v1/cards/generate` or the UI equivalent
2. request parsing, CSRF validation, and ownership enforcement happen in `app/main.py`
3. `CardGenerationService` in `app/generation.py` runs the generation workflow
4. prompt moderation runs before any model call
5. `AzureFoundryAIClient` calls the current Azure AI Foundry/OpenAI deployments directly:
   - text via chat completions
   - image generation or image edit via image endpoints
6. post-text, art-prompt, and post-image moderation decisions are applied
7. successful results persist to Cosmos DB + Blob Storage; partial artwork failures return `awaiting_artwork_retry`
8. telemetry and bounded dependency logging flow through `app/telemetry.py`; `/healthz` probes Cosmos and Blob via `app/health.py`

### Current service boundaries

Today, the backend already has strong boundaries worth preserving:

- **HTTP/session/auth boundary:** `app/main.py`, `app/auth.py`, `app/session_middleware.py`
- **runtime configuration boundary:** `app/settings.py`, `app/secrets.py`
- **generation orchestration boundary:** `app/generation.py`
- **operability boundary:** `app/telemetry.py`, `app/health.py`
- **state boundary:** Cosmos for metadata/audits and Blob for images/photos

### Infrastructure and deployment today

The deployment topology is currently centered on a single Python web service in `azure.yaml`:

- `web-nat` runs as an Azure Container App
- infra is provisioned with Bicep from `infra/`

`infra/main.bicep` already provisions most of the platform needed for a future agent design:

- Azure Container Apps environment and app
- Azure Container Registry
- Key Vault
- Azure Monitor / Application Insights / Log Analytics
- Cosmos DB
- Storage account + private endpoint
- Azure AI Foundry account, project, and model deployments
- network resources and private endpoints

Important observation: the current app is wired to the **Foundry account endpoint** for direct model deployments (`FOUNDRY_ENDPOINT`), not to a **Foundry project endpoint** for agent operations. That distinction matters for the proposed design.

### Documentation style and conventions already present

The existing docs in `docs/` use:

- concise architecture prose
- explicit environment/configuration tables
- stable API contracts
- operational notes, rollout cautions, and security warnings

This document follows the same style.

## Proposed architecture

### Design principles

1. **Do not move authentication, ownership, or persistence into agents.**
2. **Do not replace deterministic safety gates with open-ended reasoning.**
3. **Do not break the current response contract or artwork-retry behavior.**
4. **Prefer one hosted orchestrator first; split into more deployable agents only when the boundaries prove stable.**
5. **Keep phase 1 tool usage minimal.** The card generator does not currently need broad web search, arbitrary MCP tools, or direct data-store write tools.

## Recommended agent topology

### 1. Card Orchestrator Agent — required

**Type:** Foundry hosted agent  
**Implementation style:** code-first, Python, Microsoft Agent Framework  
**Responsibility:** the single agent entrypoint called by the web app for creative reasoning

This hosted agent should own the agentic part of card generation:

- decompose the user's fantasy-card prompt
- run a structured concept pass
- run a lore/style pass
- run an art-prompt refinement pass
- optionally run a self-critique/repair loop before returning structured output

This is the recommended place to use MAF because the workflow is multi-step and benefits from code-defined orchestration, schema validation, and explicit boundaries.

### 2. Card Concept Specialist — logical agent/skill

**Phase 1 form:** MAF skill inside the Card Orchestrator  
**Phase 2 optional form:** standalone Foundry prompt agent

Responsibilities:

- convert the user's prompt into a structured card concept
- choose card type, rarity, mana, attack, health, and rules shape
- keep output aligned with the existing JSON schema used by `GeneratedCardModel`

### 3. Lore and Flavor Specialist — logical agent/skill

**Phase 1 form:** MAF skill inside the Card Orchestrator  
**Phase 2 optional form:** standalone Foundry prompt agent

Responsibilities:

- refine card name and flavor text
- improve thematic coherence between mechanics and narrative
- enforce “original fantasy” tone without leaning on copyrighted franchises or living-artist imitation

### 4. Image Prompt Specialist — logical agent/skill

**Phase 1 form:** MAF skill inside the Card Orchestrator  
**Phase 2 optional form:** standalone Foundry prompt agent

Responsibilities:

- transform approved card content into an image-safe art prompt
- preserve style constraints needed by the current image deployment
- produce a prompt suitable for both pure generation and reference-photo edit flows

### 5. Safety Review Specialist — optional, advisory only

**Phase 1 form:** MAF skill or omitted entirely  
**Phase 2 optional form:** standalone Foundry prompt agent with narrow instructions

Responsibilities:

- flag likely policy problems before the app spends image-generation budget
- suggest rewrite guidance when the concept or art brief looks risky

This specialist should be **advisory**, not authoritative. The final enforcement points should remain deterministic or platform-managed:

- existing prompt/text/image moderation checks in the app
- Azure AI Content Safety where already used
- Foundry guardrails attached at the hosted-agent or model level

## Hosted vs prompt agents: recommendation

### Recommended initial shape

Use **one hosted agent** as the deployed runtime boundary and keep the first specialist behaviors as internal MAF skills.

Why:

- the current workflow already needs precise control over schema, retries, partial completion, and telemetry
- the app must preserve `awaiting_artwork_retry` semantics
- reference-image handling and persistence compensation remain easier in the current backend
- this avoids prematurely creating four separately versioned runtime resources

### When to add standalone prompt agents

Introduce separate prompt agents only when one of these becomes true:

- a specialist prompt needs independent lifecycle/versioning
- a non-Python team wants to tune instructions without changing orchestration code
- evaluation shows a specialist benefits from separate prompt-only optimization
- a future UX wants to expose one specialist directly (for example, a lore-only assistant)

## Integration with the existing app

### Boundary decision

The web app remains the system of record for:

- authentication and session state
- request validation and CSRF
- rate limiting
- owner scoping
- persistence to Cosmos/Blob
- image bytes and image retry flow
- API/UI contracts

The agent layer becomes a **new upstream dependency** of `CardGenerationService`.

### Proposed call sites

`app/generation.py` is the correct integration point.

Replace the current single-step text-generation responsibility with a new `AgentGenerationClient` or similar boundary that:

1. calls the hosted Foundry agent through the project endpoint
2. receives structured card output plus a refined art prompt
3. hands the art prompt back to the existing image-generation path

The rest of the current Python workflow can remain mostly intact:

- pre-prompt moderation stays before the agent call
- post-agent moderation replaces today’s post-text moderation and art-prompt moderation checks
- image generation/edit remains in the app using the existing image deployment
- post-image moderation, persistence, and compensation remain unchanged

### Proposed phase-1 data flow

```mermaid
flowchart TD
    U[Browser / API client] --> A[FastAPI app
app/main.py]
    A --> B[CardGenerationService
app/generation.py]
    B --> C[Prompt pre-moderation
existing app policy]
    C --> D[AgentGenerationClient
new backend boundary]
    D --> E[Foundry project endpoint]
    E --> F[Card Orchestrator Agent
Hosted agent built with MAF]
    F --> F1[Concept skill]
    F --> F2[Lore skill]
    F --> F3[Image-prompt skill]
    F --> F4[Optional safety skill]
    F --> D
    D --> G[Structured card + refined art prompt]
    G --> H[Existing image generation/edit
Azure Foundry image deployment]
    H --> I[Post-image moderation
existing app policy]
    I --> J[Blob + Cosmos persistence]
    J --> K[API/UI response]
```

### Request contract between app and agent layer

The current public API should remain unchanged. The **internal** app-to-agent contract should be explicit and versioned.

#### `GenerateCardAgentRequest`

```json
{
  "schemaVersion": 1,
  "requestId": "diagnostic-correlation-id",
  "userPrompt": "Create a moonlit guardian with a shield of stars",
  "constraints": {
    "cardSchemaVersion": 1,
    "imageQuality": "medium",
    "allowReferenceImageFlow": true
  },
  "policy": {
    "moderationPolicyName": "conservative-v1",
    "disallowCopyrightedCharacters": true,
    "disallowLivingArtistImitation": true
  },
  "referenceImage": {
    "present": false
  }
}
```

Notes:

- do **not** send raw auth tokens, session cookies, or PII
- do **not** send raw owner IDs unless the agent genuinely needs them
- if correlation is needed, pass `requestId` and optionally an owner hash only
- phase 1 does not require passing image bytes to the agent

#### `GenerateCardAgentResponse`

```json
{
  "schemaVersion": 1,
  "status": "completed",
  "card": {
    "schemaVersion": 1,
    "name": "Moonshield Warden",
    "cardType": "hero",
    "rarity": "rare",
    "manaCost": 6,
    "attack": 8,
    "health": 9,
    "rulesText": "...",
    "flavorText": "...",
    "artBrief": "..."
  },
  "artPrompt": "Safe original fantasy trading card illustration ...",
  "metadata": {
    "agentName": "card-orchestrator",
    "agentVersion": "v1",
    "modelDeployment": "gpt-5-5"
  },
  "safetyHints": []
}
```

If the agent cannot safely complete, it should return a **structured refusal/failure payload**, not free-form text. The app should translate that into the existing `ProblemDetails` model.

### Retry and partial-result semantics

The current implementation already has a valuable behavior: if text succeeds but artwork fails, the user gets a partial card and can retry artwork later.

That behavior should remain.

Therefore:

- the agent should complete **before** image generation begins
- the agent should not own Blob/Cosmos writes
- the existing `retry_artwork` flow should continue to reuse the persisted validated payload and derived art prompt

In the new world, the persisted payload would simply contain **agent-derived** card content and art prompt instead of **single-call text-model-derived** content.

## How the agents are hosted and deployed

### Recommended runtime model

### Public web app

- stays in Azure Container Apps
- continues to serve HTML, API routes, auth callbacks, health probes, and media streaming

### Agent runtime

- runs as a **Foundry hosted agent** in the existing Azure AI Foundry project
- is built in Python with the **Microsoft Agent Framework**
- is deployed as a separate agent workload, not as another HTTP route inside the public web container

This separation is useful because it:

- isolates prompt/instruction evolution from the public web app
- makes agent traces/evaluations first-class Foundry concerns
- avoids turning the web app into a monolithic “do everything” runtime

## Code-first hosted agent vs prompt agents

### Code-first hosted agent

Use this for the **Card Orchestrator Agent**.

Why it fits:

- multi-step orchestration
- structured inputs/outputs
- explicit skill registration
- future toolbox support if needed
- cleaner observability and versioning for an agent runtime

Expected artifacts in a future implementation:

- a dedicated hosted-agent source folder in this repo
- `agent.yaml` for runtime/metadata/policy declaration
- Python agent entrypoint using MAF
- deployment via `azd` agent workflow into the existing Foundry project

### Prompt agents

Use only for narrow specialists if they later need independent lifecycle management.

Good candidates:

- lore-only tuning
- art-prompt rewriting
- internal critique agent

Not recommended for phase 1 as the primary runtime boundary.

## Microsoft Agent Framework usage pattern

The Microsoft Agent Framework should be used for:

- defining the hosted orchestrator agent
- registering specialist skills/components
- controlling agent-side tool access
- optionally connecting to a Foundry toolbox later
- keeping the agent runtime code-first and testable

In this design, MAF is the code-first layer that unifies the useful parts of
Semantic Kernel-style skills/plugins and AutoGen-style orchestration patterns
without forcing the web application itself to become the agent runtime.

Phase-1 recommendation: **no general toolbox yet**.

This application does not currently need broad external tools such as web search, arbitrary MCP servers, or direct file/database actions. Starting without a toolbox reduces latency, cost, and prompt-injection surface.

If future capabilities require tools, add them narrowly through a toolbox and keep the allowlist short.

## `agent.yaml` expectations

A future hosted agent should declare:

- agent identity/name/version
- runtime entrypoint
- model deployment bindings
- optional guardrail/policy attachment
- environment variables required by the agent runtime

If Foundry guardrails are used at the hosted-agent layer, the guardrail attachment belongs in the agent definition rather than being hand-improvised in application code.

## Infrastructure changes needed

The repo already provisions a Foundry account, project, and model deployments, so this is an extension of the existing pattern rather than a greenfield platform.

## Changes to `infra/main.bicep`

1. **Expose a Foundry project endpoint for the app runtime**
   - current runtime wiring focuses on the account endpoint used for direct model calls
   - add an output and env-var path for the **project endpoint** required by agent/project SDKs
   - example runtime variable: `FOUNDRY_PROJECT_ENDPOINT`

2. **Grant the Container App managed identity project-scope access**
   - current infra gives the deployer `Foundry User` on the project
   - the application runtime will also need a project-scope role assignment to invoke agents
   - keep existing account-scope model access if direct image generation remains in the app

3. **Add configuration for agent invocation**
   - inject non-secret settings for the agent name/version and rollout mode
   - examples:
     - `FOUNDRY_PROJECT_ENDPOINT`
     - `FOUNDRY_AGENT_NAME`
     - `FOUNDRY_AGENT_API_VERSION` (if the chosen client path needs it)
     - `AGENT_GENERATION_MODE=off|shadow|live`

4. **Optionally provision Foundry guardrail resources**
   - if the team wants managed RAI policies beyond the current heuristic/content-safety gates, define them in the Foundry account and reference them from the agent/model configuration

5. **Preserve the current account endpoint variables**
   - the app still needs direct model access for image generation/editing unless that path is redesigned later

## Changes to `infra/modules/ai-foundry.bicep`

Recommended additions:

- new output for project endpoint
- new role assignment for the Container App identity at **project scope** (not only account scope)
- optional RAI policy resources/outputs if the team decides to standardize on Foundry guardrails

## Changes to `infra/modules/container-apps.bicep`

Add env vars for the application-to-agent integration while keeping the current Foundry/OpenAI env vars intact.

Suggested additions:

- `FOUNDRY_PROJECT_ENDPOINT`
- `FOUNDRY_AGENT_NAME`
- `AGENT_GENERATION_MODE`
- optional timeout override for the agent call if it differs from text-generation timeout

## Changes to `azure.yaml`

The current `azure.yaml` defines only the `web-nat` Container App service.

For a future implementation, extend the topology to include a dedicated hosted-agent service managed by `azd`, for example:

- `web-nat` — `host: containerapp`
- `card-orchestrator` — `host: azure.ai.agent`

That keeps a clean split between:

- public application runtime
- Foundry-hosted agent runtime

If the team later decides to place the hosted agent in a separate repo, the contract in this document still holds, but the single-repo azd topology is the simplest first move.

## Secrets and managed identity

No new user secrets should be introduced for normal runtime calls.

Preferred approach:

- web app uses `DefaultAzureCredential`
- hosted agent uses platform-managed identity/runtime auth
- project/model access is granted through RBAC
- any future toolbox or external connector secret stays in Key Vault or Foundry-managed connection surfaces, not in source control

## New Python dependencies to plan for

The current root `pyproject.toml` already contains `azure-identity`, `httpx`, FastAPI, and observability packages.

Add these for the agent architecture:

| Package | Why |
|---|---|
| `agent-framework` | Build the Foundry hosted orchestrator with Microsoft Agent Framework |
| `azure-ai-projects` | Call Foundry project/agent capabilities from the web app and support prompt-agent/project interactions |
| `azure-ai-agents` | Optional lower-level agent-service operations if needed by the implementation path |
| `azure-identity` | Already present; continue using it for RBAC-based auth |

Notes:

- `azure-identity` is already present and should remain the standard auth library.
- `azure-ai-projects` is the most important new dependency for the app-side invocation path.
- `azure-ai-agents` may remain optional if `azure-ai-projects` fully covers the chosen runtime contract.
- If the hosted agent code is kept in a separate folder with its own dependency file later, the team can narrow package placement at implementation time.

## Security and Responsible AI considerations

### Core rule: agents are not trusted with unrestricted side effects

User prompts, saved-photo context, and intermediate model outputs must all be treated as untrusted input.

### Recommended security posture

1. **No direct data-store tools for the agent in phase 1**
   - do not let the agent write to Cosmos or Blob
   - persistence stays in application code

2. **No general web search in phase 1**
   - fantasy card generation does not require internet grounding today
   - adding search would enlarge prompt-injection and copyright risk

3. **Schema-first outputs**
   - the agent must return structured JSON aligned to the existing card schema
   - the backend must still validate the result before use

4. **Keep existing deterministic moderation checkpoints**
   - pre-prompt moderation before agent execution
   - moderation on agent-returned card text and art prompt
   - post-image moderation after image generation

5. **Use managed guardrails where helpful, but do not rely on them alone**
   - attach Foundry guardrails to the hosted agent and/or deployments if adopted
   - retain app-level safety checks as defense in depth

6. **Redaction and telemetry discipline**
   - continue existing privacy posture from `app/telemetry.py`
   - do not log raw prompts, generated card text, image prompts, image bytes, identities, or tokens in agent/application telemetry

### Prompt injection considerations

Even without web search, prompt injection still matters because user prompts can attempt to alter system behavior.

Mitigations:

- keep the hosted agent’s instruction hierarchy explicit and stable
- keep tool access minimal
- validate output schema and allowed enums/ranges in the backend
- treat all agent text as data, not executable instructions
- never let agent output decide authorization, storage scope, or RBAC behavior

### Copyright and originality considerations

This application already blocks living-artist imitation and obvious copyrighted-character requests. The agent design must preserve and strengthen that posture.

Recommendations:

- instruct specialists to produce **original fantasy** content only
- reject style-transfer or artist-imitation requests early
- keep final image publication behind post-image moderation
- capture only sanitized audit metadata when requests are blocked

## Migration and rollout plan

### Phase 0 — architecture only

- produce this specification
- no runtime changes

### Phase 1 — add the hosted orchestrator in shadow mode

- build a Foundry hosted `card-orchestrator` agent with MAF
- do **not** change the public API contract
- call the agent alongside the current text-generation path for sampled/internal traffic
- compare:
  - schema validity
  - latency
  - moderation rejection rates
  - thematic quality
  - prompt-to-art coherence

This phase is where Samwise should define golden test prompts and evaluation fixtures.

### Phase 2 — switch text/card design to the agent, keep image generation in app

- replace the current direct text-generation call with the agent call
- keep image generation/edit, moderation, persistence, and retry behavior in `app/generation.py`
- enable runtime fallback to the current non-agent text path through config if the agent is degraded

This is the recommended first live rollout because it captures most of the product value with the least disruption.

### Phase 3 — deepen agent specialization

- split internal skills into independently versioned prompt agents only if warranted
- optionally add critique/repair loop for better consistency
- optionally add managed Foundry guardrails and evaluation suites

### Phase 4 — optional advanced tooling

Only if real requirements appear:

- toolbox-backed retrieval for approved lore/style assets
- prompt optimization/eval workflows in Foundry
- broader agent observability and continuous evaluation

## Backward-compatibility requirements

Any implementation should preserve:

- current public API routes and payload shapes
- authenticated ownership semantics
- `awaiting_artwork_retry` behavior
- Cosmos/Blob document shape unless an explicit migration is approved
- health and telemetry discipline

## Open questions and risks

1. **Region/runtime availability**  
   Hosted-agent support, model availability, and quota must be checked against the actual deployment region strategy before implementation.

2. **Latency budget**  
   The current request timeout budget is finite. Adding an agent hop may require a tighter internal timeout and explicit fallback behavior.

3. **Project endpoint vs account endpoint confusion**  
   The current app uses the account endpoint for model inference. Agent operations need project-aware configuration and RBAC.

4. **Operational ownership**  
   The team will now operate two runtimes: the public web app and the hosted agent runtime. Dashboards, alerts, and rollout procedures must reflect that.

5. **Evaluation readiness**  
   The team needs a stable prompt set and review rubric before changing production generation behavior.

6. **Safety-policy drift**  
   If heuristic moderation, Content Safety, and Foundry guardrails disagree, the team needs a clear precedence order.

7. **Reference-image flow scope**  
   Phase 1 intentionally keeps image bytes out of the agent layer. If future designs want the agent to reason over image input, the privacy, latency, and cost model must be re-reviewed.

## Final recommendation

Proceed with a **single Foundry hosted orchestrator agent built with Microsoft Agent Framework**, integrated into the existing backend as a new upstream dependency for card-design reasoning only.

Do **not** move auth, persistence, or image-retry control into the agent layer. Do **not** start with a broad toolbox. Keep the current FastAPI + Container Apps application as the stable shell, and let the agent layer evolve behind that boundary.
