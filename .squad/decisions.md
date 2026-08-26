# Squad Decisions

## Active Decisions

No decisions recorded yet.

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
