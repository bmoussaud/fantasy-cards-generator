

# Squad Decisions

## Active Decisions

### 2026-08-28T14:44:19.626+00:00: Issue #35 NAT Gateway egress implementation
**By:** Gimli
**What:** Added a VNet/delegated ACA subnet, NAT Gateway + static public IP, a workload-profile Container Apps environment wired to that subnet, and Cosmos firewall rules sourced from the NAT public IP. Also switched the azd-targeted app/environment names to parallel `*-nat` resources, added a temporary `LEGACY_COSMOS_IP_RULE` cutover input, and documented rollout/rollback plus Entra redirect verification.
**Why:** ACA VNet mode is effectively create-time, so the safest dev rollout is a parallel NAT-backed environment while keeping the old path available until live smoke tests pass. The NAT output removes the fragile manually copied ACA outbound-IP dependency in source control, but the final validation/removal of the legacy `20.10.253.231` rule still requires a real Azure deploy and authenticated Cosmos smoke test.

### 2026-08-29: Separate text, image, and overall generation timeout budgets
**By:** Aragorn
**What:** Use 20 seconds per text attempt with two retries, 150 seconds for one image attempt with no automatic image retries, and a 225-second overall request budget. If the overall budget expires during image generation after valid text exists, persist and return `awaiting_artwork_retry` instead of a generic 504. Emit content-free request, stage, attempt, elapsed, and budget logs.
**Why:** The deployed 8-second shared stage timeout and 18-second overall timeout allowed the outer timeout to cancel image retry handling before it could return the established partial-success contract. Image calls have materially different latency and duplicate-cost characteristics from text calls, while the 225-second total remains below the default 240-second Azure Container Apps ingress timeout.

### 2026-08-29: Use the supported gpt-5.5 chat-completions contract
**By:** Aragorn
**What:** Keep the deployed gpt-5.5 model on Azure OpenAI `chat/completions` with API version `2025-03-01-preview`, omit non-default `temperature`, and require every strict JSON-schema property, including `schemaVersion`. Parse only Azure's structured error code/message for bounded diagnostics and redact request content before logging.
**Why:** The live deployment advertises both chat-completion and Responses capabilities. Managed-identity probes showed `temperature: 0.2` is rejected, then strict schema was rejected because `schemaVersion` was optional; removing the parameter and making all properties required returned HTTP 200.

### 2026-08-31: Dev redeploy blocked by Key Vault deleted-state name conflict after azd down --purge
**By:** Gimli
**What:** `azd down --purge --force -e dev` completed successfully and deleted `rg-fantasy-card-dev`, but the immediate `azd up -e dev` reprovision failed during Key Vault creation because `kvfcgdev5a7waraj5zp5i` still exists in Azure's deleted state.
**Why:** The team needs the exact operational blocker recorded: the destroy step reported success, yet Azure still rejected the Key Vault name with `ConflictError: A vault with the same name already exists in deleted state. You need to either recover or purge existing key vault.`

### 2026-08-31: Issue 48 deployer read-only data access implemented
**By:** Gimli
**What:** Implemented issue #48 on branch `squad/48-deployer-data-reader-rbac` in commit `8d5fa302068fc14c4cc1ad0bd7096ee6e30e1308`; opened PR https://github.com/bmoussaud/fantasy-cards-generator/pull/49. Changed `infra/main.bicep`, `infra/main.parameters.json`, `infra/modules/cosmos-db.bicep`, `infra/modules/storage.bicep`, `infra/README.md`, `infra/main.json`, and `tests/test_deployment_config.py`.
**Why:** The root deployment now resolves `deployer().objectId` once and grants the caller Cosmos DB Built-in Data Reader and Storage Blob Data Reader while preserving separate runtime contributor grants, explicit ARM principal types, and existing private-network/firewall restrictions. Bicep restore/build passed; 12 targeted deployment tests and all 61 repository tests passed; Ruff, Black, and diff checks passed.

### 2026-08-31: Redeployed web-nat after mixed-content fix merge
**By:** Gimli
**What:** Pulled `main` to merge commit `e62765ce18605676dec785c9f5084fa7ad40d712`, confirmed `azd` 1.32.0 was authenticated against the `dev` environment targeting `fcg-dev-app-nat`, then ran `azd deploy web-nat --no-prompt`. Azure Container Apps reported success, and `az containerapp show` now reports image `fcgdev5a7waraj5zp5iacr.azurecr.io/fantasy-cards-generator/web-nat-dev:azd-deploy-1788176739` with latest revision `fcg-dev-app-nat--azd-1788176748`.
**Why:** The merged PR #46 changed the app shell to use root-relative static asset paths. Post-deploy verification showed the live home page now references `/static/css/app.css` and `/static/js/app.js` with no `http://.../static/...` URLs, and direct HTTPS requests to both assets returned `200 OK`.

### 2026-08-31T12:18:17.759+00:00: Use root deployer() identity with explicit ARM principal type
**By:** Gimli
**What:** Issue #48, https://github.com/bmoussaud/fantasy-cards-generator/issues/48, requires resolving `deployer().objectId` once in root `infra/main.bicep` and passing it to the Cosmos, Storage, and Foundry modules. Remove the obsolete `AZURE_PRINCIPAL_ID` / `deployerPrincipalId` wiring, but retain `AZURE_PRINCIPAL_TYPE` / `deployerPrincipalType` for Storage and Foundry ARM role assignments. Cosmos native SQL role assignments receive only the resolved principal ID because they do not accept `principalType`.
**Why:** Official Bicep documentation confirms that `deployer()` identifies the user, service principal, or managed identity that initiated deployment and exposes `objectId`, but not principal type. Explicit ARM role-assignment principal types avoid intermittent service-principal or managed-identity errors, while root resolution avoids relying on undocumented nested-module identity behavior and preserves least privilege for interactive and federated CI deployments.

### 2026-09-01T15:29:35.056+00:00: Use public placeholder image for initial Container App provision
**By:** Gimli
**What:** Changed `infra/main.bicep` line 331: when `containerImage` param is empty, use `mcr.microsoft.com/azuredocs/containerapps-helloworld:latest` as the fallback instead of `${registry.outputs.registryLoginServer}/fantasy-cards-generator:latest`.
**Why:** On a fresh environment (or after a resource-token change like `namePrefix fcg→fcag`), the private ACR is brand new and empty. Pointing the Container App at `latest` in that empty registry causes `MANIFEST_UNKNOWN` during `azd provision`, which blocks the entire `azd deploy` step that would have pushed the real image. The public placeholder is guaranteed to exist, so ARM can create the Container App successfully; `azd deploy` then pushes the real image and updates the running revision in the same `azd up` invocation.
**Evidence:** Deployment `dev-1788270781` in `rg-fcag-dev` (sub `b8ff3e15`):
- All infra resources succeeded (VNet, LAW, ACR `fcagdevqhg3qc4rlbt4gacr`, App Insights, Key Vault, Container Apps Environment).
- The ACR was brand new (zero repositories) due to the `namePrefix` change.
- `fcag-dev-app-nat` Container App ARM deployment failed with: `ContainerAppOperationError / MANIFEST_UNKNOWN: manifest tagged by "latest" is not found`.
- `az containerapp revision list` returned `[]` — no revision was ever active.

### 2026-09-01T17:19:00.962+00:00: Preprovision hook ensures APP_SESSION_SECRET_KEY presence
**By:** Gimli
**What:** Added `hooks/ensure_session_secret.sh` — an idempotent `preprovision` hook that checks if `APP_SESSION_SECRET_KEY` is set in the azd environment. If absent, generates a cryptographically secure 32-byte hex value via `python3 secrets.token_hex(32)`, stores it via `azd env set` (stdout suppressed, no tracing), and unsets the bash variable. Wired as the first preprovision step in `azure.yaml` so it runs before every `azd up` / `azd provision`. Added three tests in `tests/test_deployment_config.py` covering hook safety, azure.yaml wiring, and Bicep double-gate verification (both ACA native secret and env secretRef gated by same non-empty condition).
**Why:** Root cause: `APP_SESSION_SECRET_KEY` was absent from azd env. `infra/main.parameters.json` uses empty-string default (`${APP_SESSION_SECRET_KEY=}`), so missing value silently passed `""` to Bicep. `infra/modules/container-apps.bicep` gates both the ACA secret and env secretRef on `!empty(appSessionSecretKeyValue)` — correct logic, but when azd env var is unset, the gate condition strips both on the next `azd up`. Symptoms: Container App crash-loop with `RuntimeError: APP_SESSION_SECRET_KEY must be set`. Solution: Preprovision hook ensures the secret is never absent from azd env before ARM provisioning begins.
**Evidence:** Post-remediation — Container App revision Running/Healthy. `GET /healthz` returned HTTP 200. Secret value never printed or logged. Key Vault sync deferred to next `azd provision` (deployer principal handles it via Bicep).
**Applicability & Rules:** (1) Never omit `APP_SESSION_SECRET_KEY` from azd env before provisioning; preprovision hook prevents this unless `--skip-hooks` is used. (2) Both ACA secret and env secretRef must be gated by the same condition; splitting the logic risks ARM deployment failure or app crash. (3) Secret-safe hook conventions apply: no `set -x`, additive paths, suppress stdout for `azd env set`.

### 2026-09-02: Healthz infra wiring uses container-scoped blob RBAC and plain timeout knobs
**By:** Gimli
**What:** Narrowed the Container App's Storage Blob Data Contributor assignment from the storage account to the `card-assets` blob container resource, and wired `HEALTHZ_COSMOS_TIMEOUT_MS` / `HEALTHZ_BLOB_TIMEOUT_MS` through azd+Bicep as plain integer settings defaulting to `1500`.
**Why:** The health probe only needs container metadata access plus the app's existing card-asset read/write/delete path within that one container, so account-wide blob scope was broader than necessary. The timeout knobs are non-secret operational limits and belong in IaC next to the existing ACA probe cadence so probe cost stays bounded and auditable.

### 2026-09-02T14:10:23+0000: ARM float arithmetic must not use `mul()`/`div()` (consolidated)
**By:** Gandalf, Gimli
**What:** Supersede the earlier operand-reordering guidance for ARM/Bicep math. ARM template functions such as `mul()` and `div()` reject Float operands regardless of operand order, so expressions involving decimal values such as `dailyQuotaGb=0.25` must not rely on reordering. For the Application Insights ingestion-threshold case, floating-point arithmetic was moved out of ARM variables and into the KQL query text; future infra code should either move float math to downstream consumers, scale values to integers first, or otherwise avoid ARM float arithmetic entirely. Validate ARM expression changes with `azd provision --preview`, because local Bicep build/lint does not catch ARM runtime type-validation failures.
**Why:** The first fix attempt treated Integer-first operand order as sufficient, but follow-up validation showed the remaining Float operand still fails at ARM runtime. Consolidating the corrected rule removes conflicting guidance while preserving the verified workaround and validation discipline the team should follow for future Bicep changes.

### 2026-09-02T14:10:23+0000: Cosmos connectivity is standardized on Private Endpoint under policy-enforced `publicNetworkAccess: Disabled` (consolidated)
**By:** Gimli, Gandalf, Scribe
**What:** Initial Option B experimentation proved the ACA subnet service-endpoint path but also showed that the real blocker was not Cosmos DB Serverless. A management-group Azure Policy (`CosmosDB_PublicNetwork_Modify` in `MCAPSGovDeployPolicies`) continuously enforces `publicNetworkAccess: Disabled`, which makes public IP rules and VNet service-endpoint filtering non-viable as the durable path in this subscription. The repo and live Azure contract are now standardized on the Private Endpoint topology: `infra/modules/cosmos-private-endpoint.bicep` provisions `privatelink.documents.azure.com`, the VNet link, and a Cosmos `Sql` private endpoint in the `private-endpoints` subnet; related comments/tests were corrected to attribute the behavior to governance policy rather than a Serverless limitation. Benoit's 2026-09-02 smoke test confirmed `azd up` plus authenticated card generation succeeded end to end through Azure Container Apps → Private Endpoint → Cosmos DB.
**Why:** Consolidating the Option B attempt, the root-cause correction, the Private Endpoint deployment, and the user smoke test removes conflicting guidance and leaves one clear network decision for future work. Follow-on cleanup may remove the now-inert NAT IP rule, but all new Cosmos connectivity work should assume `publicNetworkAccess: Disabled` remains policy-enforced and that Private Endpoint is the supported path here.

### 2026-09-02T14:10:23+0000: Image quality is an end-to-end low/medium/high contract (consolidated)
**By:** Aragorn, Legolas
**What:** Standardize image quality as a `low|medium|high` setting, defaulting to `low`, across the UI form, request handling, service layer, settings validation, and the Azure OpenAI `gpt-image-2` image-generation request. The UI submits the selected value, controller/service code forwards it to `CardGenerationService.generate_card()`, invalid values are normalized or rejected consistently, and deployments must wire `IMAGE_QUALITY` through local env examples and Bicep/Container Apps configuration.
**Why:** Quality materially affects latency and cost, so the team needs one repo-wide contract instead of separate frontend and backend assumptions. Consolidating the UI and backend decisions records that the form, handler, service signature, and Azure request parameter must evolve together.

### 2026-09-02: Authenticated library uses 5-minute user-delegation Blob SAS URLs
**By:** Legolas
**Status:** ⚠️ **SUPERSEDED** by 2026-09-03T08:15:45+0000 decision (backend-proxy route).
**What:** Library pages mint read-only user-delegation SAS URLs for card artwork with a fixed 5-minute expiry, staying within the storage account's existing 15-minute SAS policy window. The Container App runtime identity keeps container-scoped Blob Data Contributor for uploads and gains account-scoped Storage Blob Delegator only for SAS signing. Cross-user detail lookups fail closed as 404s from the authenticated user's partition rather than revealing whether another owner's card exists.
**Why:** User-delegation SAS keeps storage private, avoids Shared Key access, and matches the repo's Entra-first posture. Five minutes is long enough for normal page loads and reloads but short enough to limit replay value if a URL leaks. Returning 404 for non-owned card IDs avoids existence disclosure while still enforcing strict per-user scoping.

### 2026-09-03T08:15:45+0000: Authenticated library serves artwork via backend-proxy image route, not SAS URLs

**By:** Legolas

**Supersedes:** the 2026-09-02 decision "Authenticated library uses 5-minute user-delegation Blob SAS URLs."

**What:** Card artwork on `/my/cards` and `/my/cards/{card_id}` is now served through the existing backend-proxy route `GET /cards/{card_id}/image` (`app/main.py` → `card_service.fetch_image()`), which streams blob bytes read by the Container App's managed identity over the private endpoint. `CardLibraryService._resolve_image_url` now returns `record.image_url_path` directly (already set to `/cards/{card_id}/image` at persistence time) instead of minting a signed URL. `AzureBlobSasUrlSigner`, `create_asset_url_signer`, and the `AbstractAssetUrlSigner` protocol were removed from `app/library.py`, and the now-unused **Storage Blob Delegator** RBAC role assignment was removed from `infra/modules/storage.bicep` (confirmed via repo-wide grep that nothing else depended on it). Ownership enforcement (401 unauthenticated, 404 for non-owned cards) is preserved because it already lives in the shared `AbstractCardRepository.get(owner_id, card_id)` scoping used by both the library and the original card-generation image route.

**Why:** The 2026-09-02 SAS approach doesn't work — the storage account is deployed with `networkAcls.defaultAction: Deny` and `publicNetworkAccess: Disabled` (Gimli's private-endpoint-only posture), so a browser on the public internet can never reach `*.blob.core.windows.net` directly. Azure Storage returns HTTP 403 for network-ACL-blocked requests regardless of SAS validity — this was a network-layer denial, not a signing bug. Reusing the pre-existing backend-proxy pattern (the same one the non-library card generation flow already used successfully) avoids reintroducing any client-direct-to-storage dependency and keeps the storage account's network posture untouched, as required. Any future image-delivery work for authenticated views should assume the browser will never talk to Blob Storage directly while `publicNetworkAccess: Disabled` remains in force.

**Evidence:** Issue https://github.com/bmoussaud/fantasy-cards-generator/issues/59, PR https://github.com/bmoussaud/fantasy-cards-generator/pull/60. `python -m pytest tests/` (139 passed), `ruff check .`, `black --check .`, and `az bicep build --file infra/main.bicep` all pass locally.

### 2026-09-03T08:46:26+0000: Library timestamps render as friendly UTC labels
**By:** Legolas
**What:** Authenticated library views should render card timestamps as human-friendly UTC text like `Sep 3, 2026, 8:04 AM UTC` while preserving the original ISO-8601 value in each `<time datetime="...">` attribute.
**Why:** This keeps the UI readable without changing storage or API contracts, and it establishes a reusable presentation convention for future server-rendered metadata in the app until we have a user-specific timezone or locale strategy.

### 2026-09-03: Profile-photo image conditioning is feasible on current GPT-image-2 stack
**By:** Aragorn
**What:** Issue #63 can proceed without an Azure model swap. The current backend already targets the `gpt-image-2` deployment in Azure AI Foundry for artwork generation, and the smallest viable implementation is to keep that deployment, add an optional backend-mediated photo upload, and switch photo-backed generations onto an image-edit/reference-image call while preserving the existing text-only `/images/generations` path as fallback.
**Why:** Repository inspection shows today's flow is text-only end to end (`CardGenerateBody` + URL-encoded form parsing + JSON-only `generate_image()` call), so the gap is API/request-shape support rather than model capability. Microsoft Foundry docs for `gpt-image-2` indicate text+image inputs, edits/variations, and face-preservation support, which matches the issue goal better than reverting to older DALL-E guidance; however, uploads must stay on the existing backend-proxy pattern because Blob Storage is private-endpoint-only and prior SAS-url attempts failed under `publicNetworkAccess: Disabled`.

### 2026-09-03: Profile photo generation backend contract for issue 63
**By:** Aragorn
**What:** The card generation backend now accepts an optional `photo` upload for reference-image generation. Frontend-integrated HTMX requests should submit `POST /ui/cards/generate` as `multipart/form-data` with fields `prompt` (required string), `csrf_token` (required string), `idempotency_key` (optional string), `quality` (optional `low|medium|high`), and `photo` (optional file). The JSON API `POST /api/v1/cards/generate` remains unchanged for text-only requests (`prompt`, `csrfToken`, `idempotencyKey`) and also accepts `multipart/form-data` with `prompt`, `csrfToken`, `idempotencyKey`, and optional `photo`. Accepted `photo` content types are `image/jpeg`, `image/png`, and `image/webp`, with a 5 MB max; invalid type returns `415 unsupported_photo_type`, oversized upload returns `413 photo_too_large`. When `photo` is present, backend generation uses Azure AI Foundry `POST /openai/deployments/{FOUNDRY_IMAGE_DEPLOYMENT}/images/edits?api-version=2025-04-01-preview`; when `photo` is absent, it keeps the existing `/images/generations` flow.
**Why:** This gives Legolas an exact upload contract to build against without changing the existing text-only flow. The uploaded image is read in memory only for the single request and is never persisted to Blob Storage or any durable store, matching the privacy/retention requirement and avoiding browser-facing photo URLs.

### 2026-09-03: Profile photo upload UI for issue 63
**By:** Legolas
**What:** Added an optional profile photo file input to the card generator form, wired the HTMX form for multipart submission, and added inline client-side preview/validation messaging for JPEG, PNG, and WebP uploads up to 5 MB.
**Why:** This keeps the new reference-image capability visible and understandable in the existing server-rendered UI while matching Aragorn's backend upload contract and preserving the current error-panel flow for server-side validation failures.

### 2026-09-03: Fix Azure Foundry bearer auth headers for all live image/text calls
**By:** Aragorn
**What:** `AzureFoundryAIClient._post()` and `_post_multipart()` now send the actual Microsoft Entra access token using an Authorization header built with plain string concatenation, and regression coverage asserts the real bearer value is passed on both the JSON (`/chat/completions`) and multipart (`/images/edits`) request paths.
**Why:** Repository inspection confirmed `_access_token()` has always requested the Cognitive Services scope (`https://cognitiveservices.azure.com/.default`), which matches Azure AI Foundry / Azure OpenAI bearer-token auth, but the outbound header code never interpolated the token. Git history shows the bug has existed since commit `87c51d0` introduced the live Foundry client, so there is no prior working bearer-header implementation in this codebase. This environment can silently rewrite the on-disk display of code using an f-string for this specific header shape, so future fixes should keep the concatenation form instead of an f-string. Local/test workflows defaulting to `AI_MODE=mock`, plus the prior live-mode regression test asserting the placeholder, explain why the breakage escaped detection.

### 2026-09-03: Issue 67 saved-photo library should use dedicated blob storage plus owner-scoped metadata
**By:** Aragorn
**What:** Recommend issue #67 store durable reference photos in a new private `profile-photos` blob container, but keep photo metadata in the existing Cosmos `cards` container under the current `/userId` partition model with a new `documentType` such as `saved-photo`. Saved-photo reads must reuse the backend-proxy pattern (new authenticated image route, no SAS URLs), and card generation should accept either a fresh upload or an owner-scoped `saved_photo_id`, with explicit opt-in (`Save this photo to my library`) and a small per-user cap (recommend 10).
**Why:** The storage account is already private-endpoint-only with `publicNetworkAccess: Disabled`, so browser-direct blob access remains a dead end; the proven `/cards/{card_id}/image` proxy pattern should be extended, not replaced. A dedicated blob container separates longer-lived personal photos from generated card assets and allows least-privilege container-scoped Blob RBAC, while reusing the existing Cosmos container avoids extra data-plane/RBAC sprawl because the app already multiplexes document types (`card`, `generation-audit`) inside the same owner partition. Opt-in persistence with user deletion and pre-persist moderation is the minimum privacy/safety bar now that uploads would become durable instead of #63's in-memory-only flow.

### 2026-09-03: Issue 67 backend API contract for saved photos
**By:** Aragorn
**What:** Implemented backend support for a durable saved-photo library. Authenticated routes are: `POST /my/photos` (`multipart/form-data`: required `photo`, required CSRF as `csrf_token` or `csrfToken`, optional `label` 1-80 chars) returning `201` with `{schemaVersion, photoId, label, createdAt, updatedAt, image:{contentType,sizeBytes,url}, thumbnail:{contentType,url}}`; `GET /my/photos` returning `200` with `{schemaVersion, photos:[...]}` newest-first; `GET /my/photos/{photo_id}/image` and `GET /my/photos/{photo_id}/thumbnail` streaming owner-scoped bytes; and `DELETE /my/photos/{photo_id}` requiring `X-CSRF-Token` (or `csrfToken` query param) and returning `204`. Non-owned or missing saved photos return `404` with `errorCode: saved_photo_not_found`.

Card generation now accepts a saved photo as an alternative reference source. `POST /api/v1/cards/generate` accepts JSON `{prompt, csrfToken, idempotencyKey?, savedPhotoId?}` for saved-photo reuse, and both `POST /api/v1/cards/generate` and `POST /ui/cards/generate` accept multipart fields `prompt`, CSRF (`csrfToken` or `csrf_token`), `idempotencyKey`/`idempotency_key`, optional `photo`, optional `saved_photo_id`, optional `save_photo` boolean, and optional `photo_label`. `photo` and `saved_photo_id`/`savedPhotoId` are mutually exclusive and return `422 photo_reference_conflict` if both are supplied. `save_photo=true` requires a fresh upload and fails with `422 saved_photo_requires_upload` otherwise. When `save_photo=true`, the backend performs Azure AI Content Safety moderation before persistence and rejects the whole request on moderation/configuration/cap failures rather than falling back to ephemeral generation.
**Why:** Legolas needs a stable contract for the frontend library picker and generate-form integration. This record also captures the production caveat: Bicep defaults `CONTENT_SAFETY_ENDPOINT` to the existing Azure AI Services / Foundry account endpoint, but live rollout still needs Azure-side verification that Content Safety is available with sufficient quota in the target region/account before enabling the feature in production.

### 2026-09-03: Issue 67 frontend uses a dedicated My Photos page plus client-side saved-photo picker
**By:** Legolas
**What:** Added a new authenticated UI route at `/my/photos/library` for managing saved reference photos, while keeping the existing `GET /my/photos` JSON endpoint as the shared data source for both the library page and the generator's saved-photo picker. The generator now enforces the upload-vs-saved-photo exclusivity in the browser by clearing the file input when a saved photo is chosen and clearing the saved-photo selection when a fresh upload is chosen.
**Why:** This avoids colliding with Aragorn's API route names, keeps the frontend aligned with the established `/my/cards` navigation pattern, and lets the browser reuse the same owner-scoped API contract for listing and deleting photos without adding new backend HTML partial endpoints.

### 2026-09-03: Harden blank optional card-generation form fields
**By:** Aragorn
**What:** Normalize blank or whitespace-only card-generation form strings to `None` before validation, and add a matching `CardGenerateBody` validator for `idempotencyKey`, `csrfToken`, and `savedPhotoId`.
**Why:** Issue #69 showed that an always-present hidden `saved_photo_id` field could submit `""` and trigger a 422 for every generation request. Hardening both form parsing and the request model covers `saved_photo_id`, `photo_label`, `idempotency_key`, `csrf_token`, and `quality` consistently while preserving CSRF failures for missing or blank tokens.

### 2026-09-03: Issue 69 frontend disables empty saved-photo field submission
**By:** Legolas
**What:** Updated the generator form so the hidden `saved_photo_id` field is rendered disabled by default and is only enabled by `app/static/js/app.js` while a saved photo is actively selected. Clearing the picker selection or switching back to a fresh upload now disables the field again so browsers omit it from form submissions.
**Why:** Aragorn's backend hardening now tolerates blank optional form values, but the saved-photo picker should not submit an empty hidden field in the first place. Keeping the browser-side state aligned with the actual picker selection closes the regression path from #67 and preserves the upload-vs-saved-photo exclusivity flow.

### 2026-09-03: Dev-only Azure Foundry payload debug logging
**By:** Aragorn
**What:** Added a dev-only `DEBUG_LOG_AI_PAYLOADS` setting that auto-enables raw Azure Foundry payload/response debug logging only when `APP_ENV=development`, uses the standard `logging` logger `app.ai_debug`, logs full text request/response bodies for `generate_card()`, and logs metadata-only request/response details for image generation/edit calls without logging reference-image bytes or base64 image payloads.
**Why:** The team needed a local escape hatch to inspect `/chat/completions` input/output while preserving the existing production privacy posture. I hard-blocked payload logging outside `development` even if `DEBUG_LOG_AI_PAYLOADS=true`, so shared/test/production deployments cannot accidentally emit raw prompts or model outputs.

### 2026-09-03: Saved-photo generator submissions should not depend on toggling a hidden field disabled
**By:** Legolas
**What:** Removed the generator form's reliance on dynamically disabling/re-enabling the hidden `saved_photo_id` input, and now force the exact saved-photo state onto every HTMX generate request during `htmx:configRequest` while also deleting any stale `photo` part when a saved photo is selected.
**Why:** Production debug logs from PR #72 proved saved-photo selections were reaching Azure Foundry as plain text-to-image requests, which means the browser request lost `saved_photo_id` before backend parsing. Backend multipart parsing already accepts a non-empty `saved_photo_id` and normalizes blank values to `None`, so the brittle edge was the frontend serialization contract introduced by #69's disabled-hidden-input workaround. Mirroring the selected photo directly into HTMX's outgoing `formData`/`parameters` closes that gap and keeps the upload-vs-saved-photo exclusivity explicit at submission time.

### 2026-09-04T13:10:31.843+00:00: Agent architecture direction (Foundry + Microsoft Agent Framework)
**By:** Gandalf (Lead/Architect)
**What:** Proposed a documentation-first architecture that keeps the FastAPI Container App as the public surface, adds a Foundry-hosted `card-orchestrator` agent built with Microsoft Agent Framework for creative orchestration, and records the full design in `docs/architecture-agents-foundry.md`.
**Why:** Requested by Benoit as a cross-team architecture/specification effort so future agent work uses Microsoft Foundry and the Microsoft Agent Framework without forcing a risky big-bang rewrite.
**Impacts:** Aragorn (backend integration boundary, project-endpoint invocation, fallback path), Gimli (Bicep/RBAC/env-var/`azure.yaml` topology changes), Legolas (possible future UI states for agent-backed generation and fallbacks), Samwise (golden prompts, shadow-eval strategy, contract and safety regression coverage)
