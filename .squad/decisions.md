

# Squad Decisions

## Active Decisions

# 2026-08-27 — Created issue #21 for real ACA application image

## Decision

- Opened GitHub issue #21 ("Build and deploy the real app container to Azure Container Apps") to track adding a production app Dockerfile, registry publishing, and azd/ACA deployment wiring so Container Apps stops running the placeholder hello-world image.

## Why

- The deployed infra succeeds today, but `infra/main.bicep` still defaults `containerImage` to `mcr.microsoft.com/azuredocs/containerapps-helloworld:latest`, and the repo currently lacks a production image build/push path.

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

# 2026-08-27T08:23:16Z — Close issue #18 as duplicate of #19

## Decision

- Closed GitHub issue #18 as a duplicate of #19 after static verification that PR #20 fixed the underlying Foundry managed-identity bug.

## Why

- Issue #18 and #19 describe the same `azd up` failure (`BadRequest: Unsupported configuration. To create projects, you must enable a managed identity on your resource.`).
- Current `infra/modules/ai-foundry.bicep` shows `identity: { type: 'SystemAssigned' }` on both the parent `foundryAccount` and child `aiFoundryProject`, matching the fix merged in PR #20.
- Live Azure verification was not possible in this environment because no authenticated `az`/`azd` access was available.

# 2026-08-27T08:30:46Z — Issue #21 triaged to Gimli and marked high priority

## Decision

- Mark GitHub issue #21 ("Build and deploy the real app container to Azure Container Apps") with `priority:high`.
- Reassign the issue from `squad:legolas` to `squad:gimli`.
- Remove the stale `go:needs-research` label.

## Why

- Repository inspection shows the gap is operational deployment work, not frontend work: `infra/main.bicep` still defaults `containerImage` to the hello-world placeholder, `infra/modules/container-apps.bicep` wires that image into the live Container App, `azure.yaml` defines infra only, the current GitHub workflow validates Bicep/Python but does not build or push an application image, and no ACR resource exists in `infra/`.
- The running app is a small Python/FastAPI service (`app/main.py`), so the missing work is primarily Docker packaging, ACR provisioning, and `azd`/CI deployment automation. Per `routing.md`, that belongs with **Gimli (Infra & Deployment)** rather than **Legolas (Frontend / Web UI)**.
- `go:needs-research` is a generic workflow label in this repo, not a Go-language marker, but issue #21 already contains the necessary investigation and the repository state confirms the implementation path. The label was no longer adding useful triage signal.

# 2026-08-27T12:23:00Z — Issue #21 azd dev deploy unblocked and validated

## Decision

- Ran `azd provision --environment dev --no-prompt` in the repo root. The first run created the new Azure Container Registry but failed while updating `fcg-dev-app` because the target image tag did not exist in ACR yet.
- Populated `.azure/dev/.env` with `AZURE_CONTAINER_REGISTRY_ENDPOINT` and `AZURE_CONTAINER_REGISTRY_NAME`, then ran `azd deploy --environment dev --no-prompt` to build, push, and deploy the real application image.
- Re-ran `azd provision --environment dev --no-prompt` after the image existed so the Bicep deployment could converge cleanly, then re-ran `azd deploy --environment dev --no-prompt`, which succeeded.
- Verified `fcg-dev-app` is now running image `fcgdev5a7waraj5zp5iacr.azurecr.io/fantasy-cards-generator/web-dev:azd-deploy-1787833266` instead of the placeholder hello-world image, and the app endpoint serves the FastAPI/Jinja scaffold page.

## Why

- The original `azd deploy` failure was caused by a stale `.azure/dev/.env` that was missing the ACR endpoint/name introduced by the newer Bicep outputs.
- Provisioning alone did not settle cleanly on the first pass because the Container App template had already been switched to the real ACR image path, but that image had not been pushed yet; once `azd deploy` published the image, a second `azd provision` completed successfully.
- This confirms the `azd` path for issue #21 is now operational in `dev`: infra includes ACR, the environment has the required registry variables, and the live Container App is serving the repository app rather than the placeholder image.

# 2026-08-27T12:40:46Z — Issue #6 re-routed from @copilot to Aragorn

## Decision

- Removed `@copilot` (`Copilot`) as an assignee from GitHub issue #6 and removed the `squad:copilot` label.
- Added the `squad:aragorn` label so the backend/auth implementation routes to **Aragorn**.
- Recorded that **Gandalf** will provide the architecture and code review oversight for this security-critical authentication work.

## Why

- Issue #6 is security-critical authentication foundation work, which `team.md` explicitly marks as 🔴 outside `@copilot`'s capability profile.
- `routing.md` assigns backend/API/auth foundation work to **Aragorn**, while security-sensitive cross-cutting review belongs with **Gandalf**.
- The reassignment confirms the capability-profile guardrail worked as intended: `@copilot` declined correctly, and triage moved the work to the right squad members instead of bypassing policy.

# 2026-08-27T12:44:06Z — Issue #6 Entra External ID auth foundation implemented in PR #24

## Decision

- Implemented Microsoft Entra External ID authentication foundation in PR #24: https://github.com/bmoussaud/fantasy-cards-generator/pull/24
- Chose **Authlib** for OIDC authorization code flow handling and ID-token validation via OIDC discovery/JWKS instead of hand-rolled token validation.
- Chose FastAPI/Starlette **SessionMiddleware** backed by `itsdangerous` for the signed session cookie, storing only minimal user claims (`sub`, `name`, `email`) and no Entra tokens.
- Added an authenticated app shell baseline, login/callback/logout routes, a reusable auth-required dependency, focused mocked auth tests, and `docs/auth-setup.md` for Entra External ID registration guidance.
- Recorded a release caveat: automated tests mock the identity provider, so a real Entra External ID tenant still needs manual end-to-end verification before production rollout.

## Why

- The issue required secure OIDC code-flow foundations with PKCE, state/nonce validation, signed session cookies, and app-registration documentation for a server-rendered FastAPI app.
- Authlib is a well-vetted OIDC client for FastAPI/Starlette and lets the app validate issuer, audience, expiry, and signing keys through standard provider metadata instead of custom JWT code.
- SessionMiddleware met the MVP requirement for a signed cookie session while keeping the stored session footprint minimal and auditable.

# 2026-08-27T12:53:40Z — PR #24 auth review requests changes

## Decision

- Reviewed PR #24 (`feat(auth): implement Entra External ID authentication foundation`) against the auth/security checklist and **requested changes** rather than approving it.
- Confirmed the implementation correctly uses Authlib OIDC discovery/JWKS, PKCE (`S256`), Authlib-managed `state`, explicit `nonce`, fail-closed callback handling, minimal signed session claims, and local session clearing on logout.
- Rejected the PR on one blocking issue: `load_auth_settings()` falls back to the hardcoded session signing key `dev-session-secret-change-me` whenever `APP_SESSION_SECRET_KEY` is unset and `APP_ENV` is unset/defaults to `development`.
- Per reviewer protocol, **Aragorn is locked out of this revision cycle for this auth artifact**. The follow-up fix must be owned by **Gandalf** or escalated to another backend-capable reviewer, not by Aragorn.

## Why

- A predictable session signing key allows forged authentication cookies if the app is deployed without an explicit secret, and the risk is amplified because the current default path is active when `APP_ENV` is omitted.
- Security-critical auth foundation code must fail closed on missing secrets in all real environments; a convenience default is acceptable in isolated tests, but not as the process default.
- The remaining concerns are advisory rather than blocking: current tests mock the OAuth client and therefore do not truly exercise Authlib's persisted `state` / `code_verifier` path or logout behavior, and the local docs still need to reconcile HTTPS-required auth cookies with the plain HTTP dev-server instructions.

# 2026-08-27T13:01:00Z — PR #24 auth revision fails closed on missing session secret

## Decision

- Removed the development/test fallback that injected the hardcoded session signing key `dev-session-secret-change-me` when `APP_SESSION_SECRET_KEY` was unset.
- `load_auth_settings()` now raises `RuntimeError("APP_SESSION_SECRET_KEY must be set before starting the application.")` in every environment, so auth/session configuration fails closed at startup.
- Added a regression test that verifies `create_app()` raises that clear error when `APP_SESSION_SECRET_KEY` is missing.
- Added test bootstrap environment defaults in `tests/conftest.py` so application-importing tests remain explicit and stable after the fail-closed change.
- Clarified the local-auth docs: plain HTTP localhost is fine for anonymous pages, but sign-in testing requires HTTPS because the session cookie is `Secure` and the documented redirect URIs use `https://localhost:8000`.
- Pushed the revision to `squad/6-entra-external-id-auth-foundation`, commented on PR #24 with the validation summary, and recorded that GitHub blocked self-approval for the revision owner identity.

## Why

- A fixed default session secret creates a cookie-forgery risk whenever configuration is incomplete, so the application must not start without an explicit secret.
- The regression test protects the exact reviewer finding on PR #24 from silently returning in a future refactor.
- The doc update resolves the mismatch between secure-cookie auth behavior and the existing plain-HTTP quick-start note, reducing local setup confusion for the next revision cycle.
- The explicit PR note closes the reviewer loop without bypassing GitHub's self-approval guardrail; merge remains a maintainer decision after CI settles.

# 2026-08-27T13:32:16Z — Partner-org corporate logins require multi-tenant Entra ID, not External ID

## Decision

- Supersede the earlier auth-product choice for this use case: the clarified requirement is **partner-organization corporate/work-account sign-in**, so PR #24's **Microsoft Entra External ID (CIAM)** implementation should be migrated to a **plain multi-tenant Microsoft Entra ID** app registration.
- Standardize the OIDC authority on the workforce multi-tenant endpoint `https://login.microsoftonline.com/organizations/v2.0` for this scenario, rather than a tenant-specific `ciamlogin.com` authority.
- Update app registration guidance to use a normal app registration in the home Entra ID tenant with **Supported account types = Accounts in any organizational directory**.
- Treat partner-tenant restriction as an application authorization concern: if sign-in must be limited to specific partner organizations, enforce an allow-list against the `tid` claim rather than assuming Entra will restrict this automatically.

## Why

- External ID is the wrong identity product when the goal is to let users authenticate with their own employer-managed Entra ID tenants; multi-tenant workforce sign-in is the native Entra ID pattern for that requirement.
- Using `/organizations` matches the confirmed "corporate/work account only" scope and avoids accidentally opening the app to personal Microsoft accounts via `/common`.
- Recording the `tid` allow-list consideration now prevents the next implementation pass from conflating authentication ("can sign in") with partner authorization ("which tenant IDs are allowed").

# 2026-08-27T13:36:22Z — Issue #25 migration delivered in PR #26

## Decision

- Completed the auth migration for issue #25 in PR #26: https://github.com/bmoussaud/fantasy-cards-generator/pull/26
- Switched runtime/docs/test defaults from Entra External ID (CIAM) to multi-tenant Entra ID using `https://login.microsoftonline.com/organizations/v2.0`.
- Kept sign-in intentionally unrestricted to any Entra organizational tenant for MVP; no `tid` allow-list was added.

## Why

- This aligns the shipped implementation with the clarified partner-organization corporate login requirement.
- Recording the PR link here gives the squad a stable pointer to the exact migration work and validation status.

# 2026-08-27T14:13:34Z — PR #26 multi-tenant Entra callback issuer validation fixed after live sign-in failure

## Decision

- Confirmed the post-PR-#26 live sign-in failure was caused by Authlib's default OIDC issuer check using the cached discovery `issuer` value verbatim; for `https://login.microsoftonline.com/organizations/v2.0` Microsoft publishes the template `https://login.microsoftonline.com/{tenantid}/v2.0`, so Authlib rejected every real tenant-specific `iss` claim.
- Updated the callback flow to load server metadata, pass Authlib a custom `claims_options["iss"]["validate"]` hook, and validate the concrete issuer as `https://login.microsoftonline.com/<tid>/v2.0` where `<tid>` comes from the signed token's `tid` claim and must be a valid tenant GUID.
- Added server-side exception logging for the generic `/auth/callback` failure path and regression coverage that accepts a valid tenant-specific Microsoft issuer while rejecting spoofed domains and tenant/issuer mismatches.
- Opened follow-up PR #27 for review: https://github.com/bmoussaud/fantasy-cards-generator/pull/27

## Why

- This preserves strict issuer validation instead of disabling it, while handling Microsoft's standard multi-tenant issuer-template quirk correctly.
- The added logging closes the diagnosability gap that turned this incident into guesswork during live testing after PR #26 merged.

# 2026-08-27T14:53:05Z — Issue #28 adds optional Graph-based Bicep provisioning for the Entra app registration

## Decision

- Added `infra/modules/app-registration.bicep` and wired it into `infra/main.bicep` behind `deployEntraAppRegistration`, so the existing multi-tenant Entra ID web-app registration can now be provisioned declaratively as IaC.
- Standardized the module on `signInAudience: 'AzureADMultipleOrgs'`, standard web redirect URIs only, and disabled implicit grant because this app uses the OIDC authorization code flow with PKCE rather than an exposed custom API.
- Added `bicepconfig.json` with the `graphBeta` extension alias and documented that `ENTRA_CLIENT_SECRET` still cannot be created declaratively because Microsoft Graph rejects declarative `passwordCredentials`.
- Closed the manual-secret gap in PR #29 by wiring `azure.yaml` `postprovision` to `hooks/gen_client_secret.sh`, which reads `ENTRA_CLIENT_ID`, mints a short-lived 21-day secret via `az ad app credential reset`, stores it as `ENTRA_CLIENT_SECRET` in the active azd environment, and cleanly no-ops when app-registration deployment is disabled.
- Exposed Container Apps FQDN/URL outputs so the deployed callback URI can be registered automatically, and aligned `.env.example` with the HTTPS localhost redirect used by the auth docs/tests.
- Validation scope for this change is limited to local Bicep compilation; live Graph-backed provisioning was intentionally not exercised from this environment because it requires tenant permissions/consent outside this session.
- Tracked in issue #28 and opened for review in PR #29: https://github.com/bmoussaud/fantasy-cards-generator/pull/29

## Why

- This keeps the team's "everything as code" direction for Azure resources while preserving the confirmed auth requirements from issues #25/#26/#27: unrestricted organizational multi-tenant sign-in via `/organizations`.
- The deployment toggle keeps existing infra safer by default until the Graph extension path is more battle-tested, while still making the declarative option available for tenants that grant the necessary Microsoft Graph deployment permissions.
- Reusing the same post-provision secret-generation pattern from `bmoussaud/mcp-azure-apim` gives operators an auditable automated path today, while making the 21-day secret lifetime an explicit operational follow-up rather than a silent manual step.

# 2026-08-28 — ACA secret mirroring is an acceptable tactical workaround, not the target design

## Decision

- For PR #33, treating Azure Container Apps native secrets as the runtime copy of `APP_SESSION_SECRET_KEY` and `ENTRA_CLIENT_SECRET` is acceptable to restore deployability.
- Tests should reflect the new contract, and the team should treat Key Vault secretRefs as a deferred platform follow-up rather than as fixed.

## Why

- The values still enter Bicep as secure parameters and are not committed to git, but the app now stores a second copy in ACA and no longer benefits from direct Key Vault reference rotation at runtime.
- The design solves the broken deployment path, not the Azure platform limitation itself, so the team should document that distinction clearly.

# 2026-08-28 — Deployed merged PR #33 follow-up to dev with ACA-native secret mirroring

## Decision

- Updated local `main` and ran `azd up --environment dev --no-prompt` successfully.
- Post-deploy checks show revision `fcg-dev-app--azd-1787905916` is `active: true`, `state: Running`, `health: Healthy`.
- `/healthz` returned HTTP 200 and `/auth/login` returned HTTP 302 to Microsoft Entra instead of a 404/500.

## Why

- PR #33 exists to replace the broken Container Apps Key Vault `secretRef` path with ACA-native secret mirroring.
- The team needs a recorded operational confirmation that the merged change now deploys cleanly on `dev` and that the live app is healthy at both the health probe and auth entrypoint.

# 2026-08-28 — Dev azd deployment succeeded with app-registration toggle still disabled

## Decision

- Ran a non-interactive `azd up --environment dev --no-prompt` from `main` successfully against the existing `dev` environment.
- The deployment converged and the app endpoint responded, but `deployEntraAppRegistration` was not enabled in the active azd environment, so the Graph-backed Entra app-registration module did not run and `ENTRA_CLIENT_ID` remained empty in azd outputs.

## Why

- This tells the team the merged PR #29 code is deployable through the current azd workflow, while also making clear that exercising the new Entra IaC path still requires an explicit parameter/configuration choice rather than happening automatically on the existing `dev` environment.

# 2026-08-28 — Dev azd up blocked by Container Apps Key Vault secretRef resolution

## Decision

- While deploying `main` after PR #32 to the `dev` azd environment, `APP_SESSION_SECRET_KEY` was missing from azd env and had to be seeded before provisioning.
- The subsequent `azd up --environment dev --no-prompt` failed with `ContainerAppOperationError`: the `fcg-dev-app` revision could not fetch Key Vault secrets `app-session-secret-key` and `entra-client-secret` using the `fcg-dev-acr-pull` user-assigned managed identity.

## Why

- This is an operational blocker for future dev deployments using the new Key Vault-backed `secretRef` wiring.
- Post-failure inspection confirmed the Key Vault RBAC assignment (`Key Vault Secrets User`) exists for that identity and the secret ARM resources exist, so the actionable fact for the team is the exact deployment failure mode and the required pre-seeding of `APP_SESSION_SECRET_KEY` in azd env.

# 2026-08-28 — Regenerating APP_SESSION_SECRET_KEY did not fix dev Key Vault secretRef failure

## Decision

- Re-seeded the `dev` azd environment's `APP_SESSION_SECRET_KEY` with a fresh base64 value and re-ran `azd up --environment dev --no-prompt`.
- The deployment failed again on `fcg-dev-app` with the same `ContainerAppOperationError`: Container Apps could not fetch Key Vault secrets `app-session-secret-key` and `entra-client-secret` using the user-assigned managed identity `fcg-dev-acr-pull`.

## Why

- This rules out the session secret's value format as the blocker.
- The failure is still in secret reference resolution, so the next investigation should focus on Key Vault RBAC propagation timing versus a Bicep/ARM role-assignment or scope bug affecting the Container App secret fetch path.

# 2026-08-28 — Secret-safe postprovision hooks

## Decision

- Hook scripts that mint or persist secrets must not enable shell tracing and must avoid destructive credential resets on reprovision.
- Use non-tracing shell options and append/additive secret creation paths instead.

## Why

- Postprovision hooks run in automation where stdout/stderr can be captured, so traced commands can leak live secrets.
- Re-runnable infra automation also must not revoke working credentials implicitly during routine reprovisioning.

# 2026-08-28 — Stop dev deploys from depending on Container Apps Key Vault secretRefs

## Decision

- Verified that `fcg-dev-acr-pull` was the identity attached to `fcg-dev-app`, the Key Vault was in RBAC mode, and the identity had `Key Vault Secrets User` on the vault scope.
- Confirmed that both ARM deployment and direct `az containerapp secret set ... keyvaultref:` calls still failed to resolve `app-session-secret-key` and `entra-client-secret`.
- Updated the Container App contract so it now consumes those two values as regular Container Apps secrets while Key Vault remains the durable store of record.

## Why

- The live failure was in Azure Container Apps' Key Vault secretRef resolution path, not in the repository's principal wiring.
- Mirroring the same secure inputs into Container Apps removes the broken runtime dependency and made `azd up --environment dev --no-prompt` succeed again.

# 2026-08-28 — Use a slug for Graph app-registration uniqueName

## Decision

- Split the Entra app-registration Graph payload into a human display name and a separate slugged `uniqueName`, using `fantasy-cards-generator-{environment}` for the unique value.

## Why

- Microsoft Graph rejected the previous `uniqueName` because it reused the display name (`Fantasy Cards Generator (DEV)`), and that value is not valid for the Graph `uniqueName` field.

# 2026-08-28 — Wire azd env into Entra app-registration toggle

## Decision

- Added `deployEntraAppRegistration` to `infra/main.parameters.json` so `azd env set deployEntraAppRegistration true` actually reaches `infra/main.bicep` during `azd provision`.

## Why

- The Bicep parameter existed, but azd was only passing `AZURE_ENV_NAME` and `AZURE_LOCATION`, so forced reprovision still deployed with `deployEntraAppRegistration=false` and left the Entra outputs empty.

## 2026-08-28 — PR title convention: must match Conventional Commits

**Decision:** Every pull request title must itself follow the **Conventional Commits** format used for commit messages: `<type>[optional scope]: <description>` (see the "Commit message convention" entry above for types and examples).

**Rationale:** PR titles are what show up in release notes, squash-merge commit messages, and the repo's history at a glance. Keeping the PR title in the same format as commit messages avoids a squash-merge silently producing a non-conventional commit on `main`, and keeps the whole history consistent regardless of whether a PR is squash-merged, rebased, or merge-committed.

**Applies to:** all squad members and `@copilot` PRs.

**Status:** CONFIRMED 2026-08-28 (per Benoit Moussaud)

### 2026-08-28: Namespaced card vs audit IDs in shared Cosmos storage
**By:** Samwise
**What:** Fixed the single-card generation flow so card reservations and generation-audit records no longer share the same physical Cosmos document ID when deployed in Azure. Cards now persist under a `card:` document-id namespace and audits under `audit:` while keeping the public `cardId` stable for API behavior and replay logic.
**Why:** `create_services()` wires Azure mode to one `AzureCosmosCardRepository` instance for both cards and audits, so the previous shared `id=card_id` keyspace let an audit overwrite the reserved card document and then get deleted by card cleanup on early failures (for example rate limiting). I also added explicit shared-repository test coverage because the old split in-memory repositories masked this class of bug; that mismatch is worth keeping as a standing testing convention whenever deployed topology shares storage.

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
