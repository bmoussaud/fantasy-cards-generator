---
description: Application contracts and durable backend, UI, safety, and test guidance
applyTo: "app/**,hosted_agents/**,tests/**,pyproject.toml"
---

# Application conventions

- Keep authentication, moderation, image generation, persistence, and image retry
  in the FastAPI backend. All live card-text generation uses the Foundry hosted
  `card-orchestrator`; do not restore direct-text fallback or runtime mock switches.
  Deterministic clients are injected only by automated tests.
- Preserve synchronous single-card generation and the existing partial-success
  `awaiting_artwork_retry` contract. Do not repeat successful text generation
  when retrying artwork. Keep stage-specific timeout/retry budgets within the
  overall request budget and avoid automatic duplicate-cost image retries.
- `/livez` is dependency-free liveness. `/healthz` is dependency-aware readiness,
  including exact hosted-agent version/identity and endpoint routing checks.
  Authentication failures are not transient service failures.
- Maintain deny-wins moderation: refused content must not create durable card,
  request, or audit metadata. A refused artwork retry must preserve an existing
  safe partial card. Keep diagnostics content-free and bounded; do not log
  prompts, model output, reference photos, tokens, or exception payloads.
- Use Authlib OIDC authorization code flow with PKCE and multi-tenant Entra ID
  `/organizations`. Preserve tenant-specific issuer validation and secure,
  HTTP-only session cookies; never add a default session signing secret.
  Sign-in testing requires HTTPS, not plain HTTP localhost.
- Scope cards, saved photos, and queries to the authenticated owner's Cosmos
  partition. Return 404 for another owner's resources. Deliver images through
  authenticated backend proxies, not browser-direct Blob/SAS links.
- Cards and audit documents share storage: preserve distinct physical ID
  namespaces (`card:` and `audit:`), public card IDs, and additive schema
  compatibility. Test shared-repository topology, not only split memory stores.
- Saved photos require opt-in persistence, moderation, owner scoping, and
  deletion. Fresh uploads and saved-photo references are mutually exclusive.
  Keep multipart/JSON/HTMX contracts aligned and normalize blank optional form
  values without weakening CSRF checks. Set saved-photo HTMX submission state
  explicitly rather than relying on disabled hidden inputs.
- Image quality is an end-to-end `low|medium|high` contract. Keep UI, request
  validation, service calls, model parameters, and deployment settings aligned.
- Render friendly UTC timestamps while preserving ISO values in `<time datetime>`.
  Keep UI interactions accessible and consistent with existing error/partial states.
- Reuse `app/secrets.py` for runtime secrets, preserving bounded stale fallback
  and credential/session rotation behavior. Do not mirror secret values into
  extra stores as a convenience.
- Assert schema, safety, ownership, refusal, retry, and isolation invariants, not
  exact generated prose/pixels. Update tests for API changes and verify serialized
  HTTP responses as well as internal models.

Read [the generation contract](../../docs/card-generation-api.md),
[safety architecture](../../docs/architecture-agents-safety.md), and
[operational ownership](../../docs/agent-operational-ownership.md) for details.
