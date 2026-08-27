# Squad Decisions

## Active Decisions

# 2026-08-27T08:30:46Z — Issue #21 triaged to Gimli and marked high priority

## Decision

- Mark GitHub issue #21 ("Build and deploy the real app container to Azure Container Apps") with `priority:high`.
- Reassign the issue from `squad:legolas` to `squad:gimli`.
- Remove the stale `go:needs-research` label.

## Why

- Repository inspection shows the gap is operational deployment work, not frontend work: `infra/main.bicep` still defaults `containerImage` to the hello-world placeholder, `infra/modules/container-apps.bicep` wires that image into the live Container App, `azure.yaml` defines infra only, the current GitHub workflow validates Bicep/Python but does not build or push an application image, and no ACR resource exists in `infra/`.
- The running app is a small Python/FastAPI service (`app/main.py`), so the missing work is primarily Docker packaging, ACR provisioning, and `azd`/CI deployment automation. Per `routing.md`, that belongs with **Gimli (Infra & Deployment)** rather than **Legolas (Frontend / Web UI)**.
- `go:needs-research` is a generic workflow label in this repo, not a Go-language marker, but issue #21 already contains the necessary investigation and the repository state confirms the implementation path. The label was no longer adding useful triage signal.

# 2026-08-27T08:20:13Z — Issue #18 triaged to Gimli for infra verification

## Decision

- Route GitHub issue #18 (`azd up` Foundry managed-identity failure) to **Gimli** with `squad:gimli`.

## Why

- Static analysis of `infra/modules/ai-foundry.bicep` on `main` shows the code-level mitigations are already present: the Foundry account has `identity: { type: 'SystemAssigned' }`, the project is a child resource of that account, and the project itself now also has a system-assigned identity.
- Git history shows those mitigations landed via recent infra fixes and were merged in PR #20, so the remaining work is verifying `azd up` against Azure and cleaning up any stale account / identity / soft-delete state, which is operational infra work and fits Gimli's remit better than a fresh @copilot coding task.

## Governance

- All meaningful changes require team consensus
- Document architectural decisions here
- Keep history focused on work, decisions focused on direction

# 2026-08-26 — Initial architecture for the Fantasy Cards Generator

## Context

- The repository is greenfield: only a placeholder README exists.
- The product goal is a web application on Azure that generates fantasy-themed cards using Azure AI Foundry services.
- No PRD exists yet, so this decision optimizes for a simple MVP path that can evolve without re-platforming too early.
- Assumptions for this draft:
  - generation is initiated from a browser UI;
  - AI calls must remain server-side;
  - enterprise-only network isolation is not yet a hard requirement.

## Decision

### Proposed architecture

Use a simple four-part architecture:

1. **Frontend**
   - A React-based web UI (preferably a full-stack framework such as Next.js).
   - Responsibilities: collect the generation prompt/theme, display card previews, show job status/errors, and let the user download or save a card.
   - **Amendment (2026-08-26):** The user has since chosen a **Python-served, server-rendered frontend** rather than a separate JS/TS application. Treat the original React/Next.js note as superseded draft context; implementation should keep the UI in the Python web app, with progressive interactivity added from that server-rendered baseline.

2. **Backend / application API**
   - A server-side web/API layer hosted with the frontend on **Azure Container Apps** for the initial version.
   - Responsibilities:
     - validate user input;
     - enforce rate limits and safety rules;
     - orchestrate AI generation steps;
     - optionally persist card metadata/assets;
     - return a final card payload to the UI.

3. **AI layer in Azure AI Foundry**
   - Use **one Azure AI Foundry account with separate projects per environment** (`dev` first, `prod` later).
   - The backend calls the **Foundry project endpoint** through the **Azure AI Projects / Foundry SDK**.
   - Authentication:
     - **Production:** `ManagedIdentityCredential`
     - **Local development:** `DefaultAzureCredential`
   - Generation flow:
     1. Call a **text model deployment** to create a structured card specification: card name, lore, class/faction, stats, abilities, rarity, and an art prompt.
     2. Validate that payload against a server-side schema before using it.
     3. Call an **image generation model deployment** to create the card artwork from the validated art prompt.
     4. Compose the final card response in the backend, including text, stats, and image URL or binary.
   - **Do not start with Foundry Agent Service** for the MVP. Direct model calls are simpler, more deterministic, and easier to operate. Agent orchestration can be introduced later if the product needs multi-step world-building, tool use, or multi-turn creative workflows.

4. **Storage**
   - **Baseline:** Azure Blob Storage for generated images and exported card assets.
   - **Optional, only if the user wants saved history/collections:** add **Azure Cosmos DB (serverless)** for card metadata, prompts, ownership, and favorites.
   - If persistence is not required for the MVP, the application can operate with Blob Storage only or even return transient results first.

### Interaction model

1. Browser submits a card-generation request to the backend.
2. Backend validates the request and applies rate/safety checks.
3. Backend calls Azure AI Foundry text generation.
4. Backend validates structured output and derives the final art prompt.
5. Backend calls Azure AI Foundry image generation.
6. Backend stores the generated asset in Blob Storage if persistence is enabled.
7. Backend returns the card payload to the frontend for preview/download/save.

### Azure hosting choice

Choose **Azure Container Apps** for the first implementation.

**Rationale**

- Strong fit for a containerized web/API deployment from day one, while still staying Azure-native.
- Supports scale-to-zero and flexible scaling behavior for an MVP that may have bursty usage.
- Leaves a cleaner path if background jobs, async generation workers, or split API/worker patterns emerge later.
- Infrastructure can still be expressed and revised cleanly in **Bicep or Terraform**, which aligns well with Gimli's ownership of deployment/IaC.

**Override note (2026-08-26)**

- Benoit Moussaud explicitly overrode the earlier App Service recommendation and mandated **Azure Container Apps**.
- This change is recorded as a user direction rather than as a reversal driven by a technical defect in App Service.

### Non-functional concerns

#### Auth and secrets

- Use **managed identity everywhere possible**:
  - Container Apps -> Azure AI Foundry project
  - Container Apps -> Blob Storage
  - Container Apps -> Key Vault
- Use **Key Vault** only for values that cannot be replaced by managed identity.
- Do not expose Foundry endpoints, keys, or model calls directly to the browser.
- End-user authentication is deferred until the user decides whether the app is anonymous, private, or multi-user.

#### Cost control

- Separate `dev` and `prod` Foundry projects and set environment-specific quotas.
- Start with one text model deployment and one image model deployment only.
- Enforce per-user or per-IP generation limits in the backend.
- Cap image size/quality for the MVP and allow “high quality” only if justified.
- Log token usage, image calls, latency, and failures for cost visibility.
- Prefer saving the generated structured card spec so retries do not always regenerate text and image together.

#### Rate limiting and error handling

- Backend-owned rate limiting; never rely on the client.
- Retry transient Foundry failures (`429`, selected `5xx`) with capped exponential backoff.
- Use request timeouts and clear user-facing statuses.
- If image generation fails but text generation succeeded, return the structured card plus a retry path instead of losing the whole result.
- Add idempotency for repeated generate clicks where practical.

#### Observability

- Use **Application Insights** for traces, dependency telemetry, failures, and latency.
- Add correlation IDs from browser request -> backend request -> Foundry calls -> storage writes.
- Track business events: generation requested, generation succeeded, generation failed, image retry, asset saved.
- Build dashboards for latency, success rate, 429s, and estimated AI spend.

#### Responsible AI / content controls

- Add server-side prompt validation and output checks before image generation.
- Define a content policy early for prohibited content, living artists/brands/IP references, and unsafe prompt attempts.
- Review Azure AI Foundry safety features before model selection is finalized.

## Alternatives considered

1. **Azure App Service**
   - Prior rationale:
     - simplest greenfield deployment model for a single web application with server-side AI calls;
     - native support for **managed identity**, **Key Vault references**, deployment slots, and Application Insights;
     - lower operational complexity than Container Apps for an MVP;
     - better fit than Static Web Apps + Functions when requests may become longer-running and orchestration logic grows beyond a thin function.
   - Rejected in the current architecture record due to explicit user direction on 2026-08-26, not because of a technical flaw in the option itself.

2. **Azure Static Web Apps + Azure Functions**
   - Pros: clean split for SPA + lightweight APIs.
   - Rejected for now: less comfortable if generation becomes long-running, stateful, or orchestration-heavy.

3. **Azure AI Foundry Agent Service first**
   - Pros: useful for reusable agent behaviors, conversations, tools, and richer orchestration.
   - Rejected for now: over-engineered for an MVP whose core need is deterministic text + image generation.

4. **Cosmos DB from day one**
   - Pros: future-ready persistence.
   - Rejected for now: unnecessary if the first release only needs generate-and-download behavior.

## Open questions for the user

1. ~~Which models should we standardize on in Azure AI Foundry~~ — **RESOLVED 2026-08-26:** text model = **gpt-5.5**, image model = **GPT-image-2** (per Benoit Moussaud).
2. ~~Should generated cards be persisted~~ — **RESOLVED 2026-08-26:** Yes, persist cards. MVP will save generated card metadata to **Cosmos DB (serverless)** and images to **Blob Storage**, enabling a user library/history (per Benoit Moussaud).
3. ~~Will end users need to sign in~~ — **RESOLVED 2026-08-26:** Yes, sign-in required (Microsoft Entra ID / social login) so card libraries are tied to a user account (per Benoit Moussaud).
4. ~~Do you want only single-card generation, or also batch generation, decks, or iterative editing?~~ — **RESOLVED 2026-08-26:** Single-card generation only for MVP; batch/deck and iterative editing deferred to a later phase (per Benoit Moussaud).
5. ~~Are there product rules for the generated cards~~ — **RESOLVED 2026-08-26:** No specific tone/balance/IP rules for MVP; prompts stay open-ended (per Benoit Moussaud). Rai should still enforce baseline safety/content checks and flag obvious real-world IP/artist-style copying regardless of this decision.
6. ~~Is private networking / VNet isolation required~~ — **RESOLVED 2026-08-26:** Standard public Azure access + managed identity for MVP; no VNet isolation required (per Benoit Moussaud).
7. ~~Do you want the MVP to store only the final image, or also the prompt, structured card JSON, and generation audit trail?~~ — **RESOLVED 2026-08-26:** Store the full record — final image, original prompt, structured card JSON, and a generation audit trail (per Benoit Moussaud).

## All open questions resolved (2026-08-26)

Architecture decision is now finalized pending any follow-up detailed design (data model, auth flow, model deployment names). Ready to move into implementation planning.

# 2026-08-26 — Infrastructure-as-Code policy: Bicep + Azure Verified Modules

## Decision

- All Azure resources that ship with this solution MUST be provisioned via **Bicep**. This includes, at minimum, app hosting via **Azure Container Apps**, **Azure AI Foundry** hub/project resources, **Cosmos DB**, **Blob Storage**, **Key Vault**, **Application Insights**, and related supporting Azure resources.
- No manual Azure Portal provisioning is allowed for shippable infrastructure.
- Module preference order:
  1. Use **Azure Verified Modules (AVM)** from the public AVM registry when a suitable, maintained module exists for the resource type and required configuration.
  2. Use **native/custom Bicep modules** only when no AVM module is a good fit or when AVM coverage does not support a required configuration.
- This is a standing **team policy**, not a one-off project preference.
- **Gimli (DevOps / Infra)** is the primary owner and enforcer of this rule for all infrastructure and provisioning PRs.

## Why

- Bicep gives the team a reviewable, reproducible, source-controlled definition of Azure infrastructure.
- Preferring AVM reduces bespoke IaC, aligns with maintained Azure-native module patterns, and keeps infra changes easier to audit and evolve.
- Falling back to native/custom Bicep preserves full coverage when AVM does not yet fit a concrete need without weakening the Bicep-first rule.

# 2026-08-26 — Application language & tooling: Python + uv + pyproject.toml

## Decision

- Python is the **primary application development language** for this project, covering the backend / API and any project scripts or tooling that are part of the application codebase.
- Use **`uv`** for Python project management, dependency management, virtual environments, and running project commands. Do **not** standardize on invoking `pip`, `poetry`, or `venv` directly for normal project workflows.
- Use **TOML** as the canonical configuration format for Python application work, centered on a single **`pyproject.toml`** per Python project or service. Dependencies, build metadata, and Python tool configuration should live there instead of being scattered across `setup.cfg`, `requirements.txt`, and similar files where avoidable.
- Runtime secrets and deployment settings do **not** belong in `pyproject.toml`. They must remain in environment variables, secret stores such as **Azure Key Vault**, or deployment configuration such as **Bicep parameters** and **Azure Container Apps environment variables**.
- This is a standing **team-wide convention**, not a one-off instruction for the current task.
- **Aragorn (Backend Dev)** is the primary owner and enforcer for backend Python / `uv` conventions.
- **Gimli (DevOps / Infra)** is responsible for keeping CI/CD, container builds, and deployment workflows aligned with `uv`-based Python workflows.

## Clarification / resolved frontend direction

- **RESOLVED (2026-08-26):** The frontend will be **Python-served and server-rendered**, not a separate JavaScript/TypeScript application.
- Suggested implementation direction: prefer an idiomatic Python web stack such as **FastAPI + Jinja2 templates + HTMX** for fast MVP delivery while keeping AI calls and page composition in one deployable service. This is a recommendation, not a hard mandate — **Aragorn and Gandalf should finalize the exact framework choice during implementation**.

## Why

- This user directive closes the previous backend-language ambiguity and gives the team one clear application-language baseline for backend implementation, scripts, dependency management, and developer workflows.
- Standardizing on `uv` + `pyproject.toml` keeps Python setup, tooling, and CI/CD consistent across local development and Azure deployment work.

# 2026-08-26 — Deployment tooling: Azure Developer CLI (azd)

## Decision

- Azure Developer CLI (`azd`) is the standard tool for provisioning and deploying this project's Azure resources end-to-end, covering both infrastructure and application deployment while wrapping the project's Bicep / Azure Verified Modules templates.
- The repository must include an `azure.yaml` at the repo root and keep an `azd`-discoverable project layout (for example, the standard `infra/` directory with `main.bicep` and matching service definitions) so `azd up`, `azd provision`, and `azd deploy` work out of the box.
- This complements, and does not replace, the standing Bicep + Azure Verified Modules-first IaC policy: Bicep remains the source of truth for Azure resource definitions, while `azd` is the orchestration and workflow layer on top of that IaC.
- CI/CD automation, including GitHub Actions, should use `azd` commands such as `azd pipeline config`, `azd provision`, and `azd deploy` where practical instead of hand-rolled Azure CLI deployment scripts.
- This is a standing team-wide policy.
- **Gimli (DevOps / Infra)** is the primary owner and enforcer of this rule and should scaffold `azure.yaml` plus an `azd`-compatible infra/service structure when infrastructure work begins.

## Why

- Standardizing on `azd` gives the team one Azure-native workflow for local provisioning, deployment, environment management, and CI/CD automation.

### 2026-08-26: AVM boundary for core data services
**By:** Gimli
**What:** Provision Cosmos DB, Storage, and the Azure AI Foundry account through AVM resource modules, but keep the Azure AI Foundry project as a native Bicep child resource because the account AVM does not cover project creation.
**Why:** This keeps us aligned with the team's AVM-first policy while still shipping the missing Foundry project binding in source control. Model deployment names, versions, and SKUs are parameterized because exact catalog/quota support must be confirmed against the live target subscription and region.

# 2026-08-26 — CLI environment loading: python-dotenv

## Decision

- Every CLI application / entry point in this project must load a local `.env` file at startup using **`python-dotenv`** (for example, `load_dotenv()`), for local development convenience.
- `.env` is for local developer convenience only. It must **never** be committed to git (it must be listed in `.gitignore`) and must **never** be relied upon in deployed / production environments.
- Production and deployed runtime configuration still comes from environment variables injected by **Azure Container Apps**, **`azd`**, **Azure Key Vault**, and related deployment configuration, per the existing secrets policy.
- Add **`python-dotenv`** as a project dependency via **`uv`** (for example, `uv add python-dotenv`) in the relevant `pyproject.toml`.
- This is a standing **team-wide convention**.
- **Aragorn (Backend Dev)** is the primary owner and enforcer for application code.
- **Gimli (DevOps / Infra)** ensures `.env.sample` / `.env.example` exists where appropriate and that `.env` remains gitignored.

## Why

- This gives local CLI workflows a consistent, low-friction way to pick up developer-specific settings without hardcoding secrets or scattering ad hoc environment-loading logic.
- It preserves the existing production secrets policy by making `.env` a development convenience rather than a deployment dependency.

# 2026-08-26 — Detailed MVP operating decisions (draft, pending confirmation)

## Needs your confirmation

1. ~~**Auth:** Should the MVP use **Microsoft Entra External ID** (customer identities) with email sign-in first and optional Google/Microsoft social providers later, rather than workforce-only Entra tenant sign-in?~~ — **CONFIRMED 2026-08-26:** Yes, Entra External ID with email sign-in first (per Benoit Moussaud).
2. ~~**Cost guardrails:** Are you comfortable starting with **Azure budget alerts + an app-level generation kill switch** (for example dev ≈ €50/month, prod ≈ €200/month) since Azure does not provide a universal hard spend cap for pay-as-you-go services?~~ — **RESOLVED 2026-08-26:** No. MVP will ship with **no cost guardrails** (no budget alerts, no kill switch, no spend caps). The only existing control is the previously-decided per-user/IP rate limiting (per Benoit Moussaud). **Risk accepted by the user:** uncontrolled Azure AI Foundry spend is possible if rate limiting is bypassed or insufficient; revisit before public/production launch.
3. ~~**Moderation:** Should the app **block disallowed prompts before generation and quarantine failed outputs after generation**, instead of merely warning?~~ — **CONFIRMED 2026-08-26:** Yes, hard block pre-generation and quarantine post-generation, no manual review queue for MVP (per Benoit Moussaud).
4. ~~**Retention/deletion:** Should the MVP support **user-triggered hard deletion** of accounts/cards, with async blob cleanup and a short-lived minimal audit trail?~~ — **CONFIRMED 2026-08-26:** Yes, hard delete cards/accounts with async blob cleanup and a minimal 30-day audit trail (per Benoit Moussaud).
5. ~~**Generation flow:** Should card creation be **asynchronous by default** (submit job -> poll/status UI -> completed card) rather than a single synchronous request?~~ — **RESOLVED 2026-08-26:** No. MVP will use **synchronous request/response** generation (single blocking call, no job/polling model) for simplicity (per Benoit Moussaud). **Risk accepted:** long-running generation calls may approach or exceed Azure Container Apps request timeout limits under slow model latency; revisit if timeouts or poor UX are observed.
6. ~~**Legal / ToS / IP:** Should the MVP ship with a **conservative policy**: no living-artist style imitation, no obvious third-party franchise/logo copying, and output offered for personal use pending fuller terms?~~ — **CONFIRMED 2026-08-26:** Yes, conservative policy as proposed (per Benoit Moussaud).

## All 6 confirmation items resolved (2026-08-26)

Remaining ~10 lower-stakes items (Cosmos DB model — note: `jobs` container may be dropped per the sync-generation decision in §11, Blob storage, testing strategy, CI/CD, local dev mock mode, dependency/container scanning, API boundary, domain/TLS/session config, backup/DR) proceed as their proposed defaults unless the user objects.

## 1. Cosmos DB data model

**Proposed decision:** Use **two Cosmos DB serverless containers**: `cards` and `jobs`, both partitioned by `/userId`. Store one document per card in `cards` with immutable generation inputs/outputs plus mutable presentation fields; store async generation state in `jobs`. Add `schemaVersion: 1` to every document and treat schema migrations as additive unless a future explicit migration is approved.

**Rationale:** Partitioning by `userId` keeps the dominant MVP queries cheap and simple: "my cards" and "my jobs." Splitting cards from jobs avoids hot updates on card documents during generation and makes future lifecycle rules easier.

**Status:** Proposed default — proceeding unless you object

## 2. Blob storage serving strategy

**Proposed decision:** Use a **private Blob Storage account** with one container `card-assets` and per-card paths such as `users/{userId}/cards/{cardId}/original.png` and `users/{userId}/cards/{cardId}/preview.webp`. Serve assets to the browser via **short-lived user delegation SAS URLs** (15 minutes), and accept only `png`, `webp`, or `jpeg` outputs up to **10 MB** per stored asset.

**Rationale:** Private blobs keep assets out of anonymous public reach while still allowing direct browser display/download. A simple path convention is enough for MVP and leaves room for derivatives later without reworking the account layout.

**Status:** Proposed default — proceeding unless you object

## 3. Auth flow specifics

**Proposed decision:** Standardize on **Microsoft Entra External ID** for end-user sign-in. Start with email-based sign-in and keep Google/Microsoft social identity providers as optional follow-up configuration. Use an **OIDC authorization code flow** from the server-rendered Python app, validate tokens against the External ID issuer metadata, create an internal app user on first sign-in, and maintain login state with a **server-side signed session cookie** (`Secure`, `HttpOnly`, `SameSite=Lax`).

**Rationale:** External ID matches a consumer-facing app better than workforce-only Entra tenant auth and keeps Microsoft-hosted identity flows. A server-managed session is simpler and safer for a server-rendered FastAPI-family app than pushing raw tokens into the browser.

**Status:** Proposed — confirm or override → **CONFIRMED 2026-08-26** (per Benoit Moussaud)

## 4. Frontend stack

**Proposed decision:** Finalize the MVP web stack as **FastAPI + Jinja2 templates + HTMX**, with a small amount of vanilla JavaScript only where HTMX alone is awkward. Do not introduce React/Next.js or a separate frontend deployment for the MVP.

**Rationale:** This aligns with the already-decided Python-first, server-rendered direction and minimizes moving parts while auth, generation orchestration, and storage are still being established.

**Status:** Proposed default — proceeding unless you object

## 5. Testing strategy

**Proposed decision:** Use **pytest** with three layers: (1) unit tests for prompt shaping, schema validation, moderation rules, and cost/session helpers; (2) integration tests against mocked Foundry/Cosmos/Blob adapters; and (3) a small Playwright E2E suite for sign-in stub, generate flow, polling UI, and library/delete flows. For AI outputs, assert **schema, safety, and invariant ranges** rather than exact wording or exact pixels.

**Rationale:** The highest risk is orchestration correctness, not deterministic prose. Invariant-based testing keeps the suite stable while still catching broken contracts and unsafe regressions.

**Status:** Proposed default — proceeding unless you object

## 6. CI/CD and environments

**Proposed decision:** Use **feature branches -> PR -> `main`** as the delivery flow. Map `azd` environments as `dev`, `staging`, and `prod`, with `main` auto-deploying to `dev`, manually promoted release candidates deploying to `staging`, and tagged/manual releases deploying to `prod`. Protect `main` and require tests plus security scans before merge.

**Rationale:** This keeps MVP delivery simple without giving up a clean promotion path once real users begin testing. The `azd` environment names stay explicit and match the infrastructure topology we already expect.

**Status:** Proposed default — proceeding unless you object → **OVERRIDDEN 2026-08-26: User mandated 2 environments only (dev, prod), no staging (per Benoit Moussaud).**

**Override note (supersedes the proposed default above):** Use **two `azd` environments only: `dev` and `prod`**. Keep the branch flow as **feature branches -> PR -> `main`**. Protect `main` and require tests plus security scans before merge. On merge to `main`, auto-deploy to **`dev`** (azd env `dev`). Promote to **`prod`** (azd env `prod`) via a tagged release or explicit manual release workflow, with no separate staging tier.

## 7. Local development experience

**Proposed decision:** Add an **`AI_MODE=mock|live`** switch. In `mock` mode, the app returns deterministic sample card JSON plus a placeholder/generated local image without calling Azure AI Foundry; `live` mode uses real Azure services. The default for local development should be `mock`.

**Rationale:** This avoids cost and onboarding friction while keeping most UI, persistence, and orchestration work testable on any laptop. Developers only need live Foundry access when validating prompts or integration edges.

**Status:** Proposed default — proceeding unless you object

## 8. Cost guardrails

**Proposed decision:** Implement **three layers of control**: (1) Azure Budget alerts on the subscription/resource group, initially targeted around **dev ≈ €50/month** and **prod ≈ €200/month**; (2) application-side per-user and per-IP generation quotas; and (3) an **environment variable kill switch** (`GENERATION_ENABLED=false`) that immediately disables new generations without redeploying. Also log estimated per-request cost and surface a daily usage summary in App Insights / dashboards.

**Rationale:** Azure budgets warn but do not reliably hard-stop every pay-as-you-go service, so the application must own the real emergency brake. Combining budget alerts with quotas and a kill switch is the smallest operationally credible MVP posture.

**Status:** Proposed — confirm or override → **REJECTED 2026-08-26:** User decided against all cost guardrails for MVP (no budget alerts, no kill switch, no hard quotas beyond existing rate limiting). Risk explicitly accepted (per Benoit Moussaud).

## 9. Moderation workflow

**Proposed decision:** Enforce moderation in **two stages**: pre-generation prompt screening and post-generation output screening. If prompt screening fails, block the request immediately with a user-facing explanation. If output screening fails, mark the job `blocked`, do not publish the image, retain only minimal forensic metadata, and show the user a generic failure message. No manual review queue in MVP; blocked content stays blocked.

**Rationale:** A concrete block path is safer and simpler than warnings-only, and a human-review workflow would add operational burden before the product has support processes.

**Status:** Proposed — confirm or override → **CONFIRMED 2026-08-26** (per Benoit Moussaud)

## 10. Retention and deletion

**Proposed decision:** Cards persist until the user deletes them. A user can delete an individual card or their whole account/library. Card deletion should hard-delete the Cosmos record and enqueue blob deletion immediately; account deletion should perform the same for all owned cards. Keep only a **minimal audit trail** (request ID, timestamps, moderation outcome, cost estimate, no full prompt/output bodies) for **30 days**, then purge it automatically.

**Rationale:** This gives users an understandable deletion story without forcing long-lived storage of sensitive creative inputs. Short-lived minimal audits preserve enough operational evidence for debugging and abuse response.

**Status:** Proposed — confirm or override → **CONFIRMED 2026-08-26** (per Benoit Moussaud)

## 11. Sync vs async generation

**Proposed decision:** Treat generation as an **asynchronous job** from day one. The POST/create route writes a `jobs` document, returns `202 Accepted` plus job ID, and the UI polls for status via HTMX until the card is ready. The same pattern should handle retries and moderation failures.

**Rationale:** Image generation latency is the least predictable part of the stack, and async jobs fit Container Apps better than gambling on request timeouts. Starting async now avoids a later architectural cutover.

**Status:** Proposed — confirm or override → **OVERRIDDEN 2026-08-26:** User chose **synchronous** generation instead (single blocking request/response, no `jobs` container/polling for MVP). Risk of request timeouts on slow generations explicitly accepted (per Benoit Moussaud). Note: this simplifies the Cosmos DB data model in section 1 — the `jobs` container may be unnecessary unless reintroduced later.

## 12. Dependency and container security scanning

**Proposed decision:** In CI, run **`uv lock --check` / dependency sync validation**, **`pip-audit`** for Python dependencies, and **Microsoft Defender for DevOps or Trivy** for container image scanning, failing the pipeline on high/critical findings unless explicitly waived.

**Rationale:** This is lightweight to add, works with a Python container workflow, and gives us a credible minimum supply-chain posture without a large platform investment.

**Status:** Proposed default — proceeding unless you object

## 13. API boundary and versioning

**Proposed decision:** Keep server-rendered page routes and HTMX partial routes under normal web paths, but define a small explicit JSON API namespace at **`/api/v1`** for domain operations such as job status, card metadata, and deletion. Treat HTMX endpoints as UI-coupled and non-public; treat `/api/v1` as the stable programmatic boundary.

**Rationale:** This avoids pretending every HTML fragment route is a public API while still creating a clean seam for future mobile/JS clients or test tooling.

**Status:** Proposed default — proceeding unless you object

## 14. Domain, TLS, and cookie/session config

**Proposed decision:** For deployed environments, use **HTTPS-only ingress** on Azure Container Apps, attach a **custom domain** with a managed certificate for `prod`, and keep environment-specific hosts for `dev`/`staging`. Trust forwarded headers from Container Apps, set session cookies to `Secure`, `HttpOnly`, `SameSite=Lax`, and scope cookies to the exact application host rather than a wildcard parent domain.

**Rationale:** This is the simplest secure baseline for a server-rendered app with OIDC redirects and avoids cross-subdomain cookie surprises in MVP.

**Status:** Proposed default — proceeding unless you object

## 15. Backup / disaster recovery

**Proposed decision:** For MVP, **do not build active disaster recovery**. Rely on managed service defaults plus enabling Cosmos DB point-in-time restore (where available/configured), Blob soft delete + versioning, and infrastructure/application source control so the stack can be recreated. Recovery objective is "restore service and user library from platform backups/manual redeploy," not zero-downtime failover.

**Rationale:** Full DR would be disproportionate for a brand-new MVP, but explicit restore-oriented defaults prevent "no plan at all." This is enough until the product has meaningful production usage.

**Status:** Proposed default — proceeding unless you object

## 16. Legal / ToS / IP posture

**Proposed decision:** Publish the MVP with a **conservative acceptable-use and output notice**: users must not request copyrighted logos/characters they do not have rights to use, must not request living-artist imitation, and should treat generated output as potentially non-exclusive. The service may store prompts/output only as needed for operation and deletion handling. For MVP, position generated cards as **personal-use / prototype content unless separate commercial terms are later added**.

**Rationale:** This is the safest posture while the product and moderation policy are still maturing, and it aligns with the need to reduce obvious IP/style-infringement risk before broader release.

**Status:** Proposed — confirm or override → **CONFIRMED 2026-08-26** (per Benoit Moussaud)

## 2026-08-26 — Commit message convention: Conventional Commits

All commits in this repo must follow the **Conventional Commits** specification: `<type>[optional scope]: <description>`.

Examples:
- `feat(auth): add Entra External ID sign-in`
- `fix(cards): correct schema validation`
- `chore(ci): add pip-audit step`

Common types:
- `feat`
- `fix`
- `docs`
- `style`
- `refactor`
- `perf`
- `test`
- `chore`
- `build`
- `ci`

Breaking changes use `!` after the type/scope or a `BREAKING CHANGE:` footer.

This applies to all squad members and to `@copilot` PRs.

Standing team-wide convention: every agent enforces it on their own commits; Gandalf and reviewers check it during code review.
