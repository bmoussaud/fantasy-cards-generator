# Microsoft Entra ID multi-tenant authentication setup

This app uses an OpenID Connect authorization code flow with PKCE against
Microsoft Entra ID. Sign-in is intentionally configured for
**organizational/work accounts from any Entra tenant** by using the
multi-tenant `/organizations` endpoint. Personal Microsoft accounts are not in
scope for this flow.

The FastAPI app stores only the minimal authenticated user claims needed for
session display and stable ownership derivation (`sub`, `name`, `email`,
`tenant_id`, `object_id`, `owner_id`) in a signed session cookie and does not
persist Entra tokens in the browser session.

## Required environment variables

Add these values to `.env` for local development:

```dotenv
APP_SESSION_SECRET_KEY=<long-random-secret>
ENTRA_CLIENT_ID=<application-client-id>
ENTRA_CLIENT_SECRET=<application-client-secret>
ENTRA_AUTHORITY=https://login.microsoftonline.com/organizations/v2.0
ENTRA_REDIRECT_URI=https://localhost:8000/auth/callback
ENTRA_POST_LOGOUT_REDIRECT_URI=https://localhost:8000/
ENTRA_SCOPES=openid profile email
```

- `APP_SESSION_SECRET_KEY` must be a strong random value generated per
  environment.
- Local development must still set `APP_SESSION_SECRET_KEY`; the app refuses to
  start if it is missing.
- `ENTRA_AUTHORITY` defaults to
  `https://login.microsoftonline.com/organizations/v2.0`.
- `ENTRA_REDIRECT_URI` must exactly match the app registration.
- `ENTRA_SCOPES` defaults to `openid profile email`.
- Authentication testing on localhost requires HTTPS because the session cookie
  is marked `Secure`, and the default Entra redirect URIs use
  `https://localhost:8000/...`. Plain HTTP is fine only for anonymous pages
  that do not exercise sign-in.

## Deployed environments: Key Vault + Container Apps secret wiring

`.env` is intentionally **not** shipped in the container image (it stays
gitignored and is for local development only). Deployed Container Apps get
their runtime configuration entirely from `infra/main.bicep` /
`infra/modules/container-apps.bicep`:

| Variable | Source in deployed environments |
|---|---|
| `APP_SESSION_SECRET_KEY` | Stored in Key Vault as `app-session-secret-key`, then mirrored into the Container App's own secret set as `app-session-secret-key` |
| `ENTRA_CLIENT_SECRET` | Stored in Key Vault as `entra-client-secret`, then mirrored into the Container App's own secret set as `entra-client-secret` |
| `ENTRA_CLIENT_ID` | Plain env var, sourced from the Entra app-registration Bicep module output |
| `ENTRA_REDIRECT_URI` | Plain env var, auto-derived from the deployed Container Apps hostname (`deployedAuthRedirectUri` output) — never set manually |
| `ENTRA_POST_LOGOUT_REDIRECT_URI` | Plain env var, auto-derived the same way |
| `ENTRA_AUTHORITY`, `ENTRA_SCOPES` | Not injected; the app's code defaults are used in every environment |

To populate the two deployment-time secret values before `azd provision`:

```bash
azd env set APP_SESSION_SECRET_KEY "$(openssl rand -base64 48)"
# ENTRA_CLIENT_SECRET is normally set automatically by the postprovision hook
# (hooks/gen_client_secret.sh) after the first `azd provision` run when
# deployEntraAppRegistration=true. Re-run `azd provision` afterwards so the
# secret is written into Key Vault and wired into the Container App.
```

Both values flow into Bicep via `infra/main.parameters.json`
(`appSessionSecretKeyValue` / `entraClientSecretValue`) as `@secure()`
parameters. The deployment writes them to Key Vault and also into Azure
Container Apps' secret store so the app does not depend on the platform's
Key Vault `secretRef` resolution path during revision provisioning.

If `APP_SESSION_SECRET_KEY` or `ENTRA_CLIENT_SECRET` are unset when
`azd provision` runs, the corresponding Key Vault secret (and therefore the
corresponding Container App env var) is simply not created — the app then
fails closed at startup exactly as it does locally when `.env` is incomplete.

## Register the application in Microsoft Entra ID

The primary, best-understood fallback path is still the manual portal flow
below. This repo now also includes an **optional** Graph-based Bicep module at
`infra/modules/app-registration.bicep` that can create the multi-tenant app
registration declaratively when `infra/main.bicep` is deployed with
`deployEntraAppRegistration=true`, and `azd provision` can now generate the
client secret automatically after provisioning. Keep the manual steps handy as
an alternative for operators who do not use the Bicep-based flow.

1. Sign in to the
   [Microsoft Entra admin center](https://entra.microsoft.com/) in your home
   tenant.
2. Go to **App registrations** and create a new **Web** application
   registration for this app.
3. Set **Supported account types** to
   **Accounts in any organizational directory (Any Microsoft Entra ID tenant - Multitenant)**.
4. Add the callback redirect URI for each environment, for example:
   - `https://localhost:8000/auth/callback`
   - `https://<your-app-domain>/auth/callback`
5. Create a **client secret** and copy the secret **value**.
6. Record the **Application (client) ID** and the **client secret**.
7. Ensure the sign-in flow requests OpenID Connect scopes `openid profile
   email`.

### Optional: provision the app registration via Bicep

If you prefer Infrastructure as Code over portal setup, this repo now ships a
Graph-backed Bicep module that provisions the app registration with:

- **Supported account types** = `AzureADMultipleOrgs` (any Entra organization)
- standard web-platform redirect URIs only; no custom exposed API scopes
- implicit grant disabled for both ID and access tokens
- redirect URIs for:
  - local development (`https://localhost:8000/auth/callback` by default)
  - the deployed Azure Container Apps URL plus `/auth/callback`

To use it:

1. Ensure the root `bicepconfig.json` is present so the `graphBeta` extension
   alias resolves to the Microsoft Graph Bicep extension.
2. Set `deployEntraAppRegistration=true` when deploying `infra/main.bicep`.
   With `azd`, run `azd env set deployEntraAppRegistration true` before
   `azd provision` so the toggle flows through `infra/main.parameters.json`.
3. Optionally override `entraAppRegistrationName`,
   `entraAppRegistrationDescription`, `entraLocalRedirectUri`, or
   `entraRedirectPath`.
   When the Container Apps environment is replaced in parallel (for example the
   NAT-backed `*-cae-nat` rollout), the deployed redirect URI changes with the
   new default domain.
4. Read the deployment outputs for `entraClientId`, `entraAppObjectId`,
   `entraServicePrincipalId`, and `ENTRA_CLIENT_ID`.
5. If you use `azd provision`, the `postprovision` hook automatically runs
   `./hooks/gen_client_secret.sh ENTRA_CLIENT_ID ENTRA_CLIENT_SECRET`. That
   hook uses `az ad app credential reset` to mint a short-lived client secret
   (21-day expiry) and stores it in the active azd environment as
   `ENTRA_CLIENT_SECRET`.
6. If `deployEntraAppRegistration=false`, the hook detects that
   `ENTRA_CLIENT_ID` is absent and exits without error.

If you are **not** using the Bicep-managed app registration, treat the
replacement Container Apps hostname as a manual follow-up: after the first live
deploy, update the registered web redirect URI to the new
`https://<container-app-domain>/auth/callback` value and verify sign-in against
that exact host.

> `ENTRA_CLIENT_SECRET` still is not created **declaratively** by the Bicep
> module itself because Microsoft Graph rejects declarative
> `passwordCredentials` for this flow. The automated azd hook closes the gap
> for the IaC path, but you can still create the secret manually if you prefer:
>
> ```bash
> az ad app credential reset --id <appId>
> ```
>
> Then store the secret securely, ideally in Azure Key Vault, before wiring it
> into your runtime environment.

No External ID tenant, CIAM user flow, or tenant allow-list is required for the
current MVP. Any partner organization's Entra work account can sign in as long
as the ID token passes standard OIDC validation.

## Authority, discovery, and logout endpoints

Use this authority:

```text
https://login.microsoftonline.com/organizations/v2.0
```

The app derives OIDC discovery from:

```text
https://login.microsoftonline.com/organizations/v2.0/.well-known/openid-configuration
```

Logout uses:

```text
https://login.microsoftonline.com/organizations/oauth2/v2.0/logout
```

Authlib uses discovery metadata to fetch the authorization endpoint, token
endpoint, JWKS signing keys, and issuer metadata needed to validate the ID
token. In a multi-tenant flow, the validated token issuer will be the
signing tenant's Entra issuer (for example
`https://login.microsoftonline.com/<tenant-id>/v2.0`), not the literal
`/organizations` authority string. Microsoft publishes the discovery issuer for
`/organizations` as the template
`https://login.microsoftonline.com/{tenantid}/v2.0`, so the app substitutes the
signed token's `tid` claim into that template before doing issuer validation.
This preserves strict issuer validation while still allowing real partner-tenant
sign-ins to pass.

## What the app implements

- OIDC authorization code flow
- PKCE (`S256`)
- session-backed `state` handling via Authlib
- explicit `nonce` generation and ID-token validation
- signed session cookie with:
  - `Secure`
  - `HttpOnly`
  - `SameSite=Lax`

Only these user claims are stored in the cookie session:

- `sub`
- `name`
- `email`
- `tenant_id`
- `object_id`
- `owner_id`

Access tokens, refresh tokens, raw ID tokens, and tenant IDs are not persisted
in the session cookie.

## Logout behavior

- `/auth/logout` always clears the local signed session cookie.
- If `ENTRA_POST_LOGOUT_REDIRECT_URI` is configured, the app also sends the
  browser to the Entra logout endpoint with that redirect target.

## Manual verification still required

The automated test suite mocks the identity provider. Before production use,
verify the registration and callback flow against a real multi-tenant Entra ID
app registration to confirm:

- a work/school account from a different Entra tenant can sign in
- personal Microsoft accounts are rejected by the `/organizations` flow
- the registered redirect URIs are exact
- the client secret is valid
- logout redirects behave as expected in each environment
