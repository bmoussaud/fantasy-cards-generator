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

## Deployed environments: Key Vault runtime retrieval via managed identity

`.env` is intentionally **not** shipped in the container image (it stays
gitignored and is for local development only). Deployed Container Apps get
their runtime configuration entirely from `infra/main.bicep` /
`infra/modules/container-apps.bicep`:

| Variable | Source in deployed environments |
|---|---|
| `APP_SESSION_SECRET_KEY` | Stored in Key Vault as `app-session-secret-key`; fetched at runtime from Key Vault using the Container App managed identity |
| `ENTRA_CLIENT_SECRET` | Stored in Key Vault as `entra-client-secret`; fetched at runtime from Key Vault using the Container App managed identity |
| `ENTRA_CLIENT_ID` | Plain env var, sourced from the Entra app-registration Bicep module output |
| `ENTRA_REDIRECT_URI` | Plain env var, auto-derived from the deployed Container Apps hostname (`deployedAuthRedirectUri` output) — never set manually |
| `ENTRA_POST_LOGOUT_REDIRECT_URI` | Plain env var, auto-derived the same way |
| `SECRET_PROVIDER_BACKEND`, `KEY_VAULT_URI`, `SECRET_PROVIDER_*` | Plain non-secret env vars injected by Bicep to point the runtime secret provider at Key Vault and control cache/retry/stale behavior |
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
parameters. The deployment writes them to Key Vault only. The Container App
receives the Key Vault URI plus non-secret secret-provider settings, and its
runtime managed identity is granted **Key Vault Secrets User** at vault scope
so the app can read secret values directly.

At runtime, the app resolves `ENTRA_CLIENT_SECRET` via the secret-provider
abstraction from `app/secrets.py`. In Azure that lets the auth flow adopt a
new Key Vault secret version without restarting the application; in local
development the same code path falls back to environment variables.

If `APP_SESSION_SECRET_KEY` or `ENTRA_CLIENT_SECRET` are unset when
`azd provision` runs, the corresponding Key Vault secret is simply not
created. The app then fails closed at startup when its managed-identity
Key Vault lookup cannot load the required value, exactly as incomplete local
`.env` config fails closed.

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
   `./hooks/gen_client_secret.sh ENTRA_CLIENT_ID ENTRA_CLIENT_SECRET
   ENTRA_APP_REGISTRATION_MANAGED`. The authoritative Bicep output
   `ENTRA_APP_REGISTRATION_MANAGED` must be `true` before the hook uses
   `az ad app credential reset` to mint a short-lived client secret (3-month
   expiry) and stores it in the active azd environment as
   `ENTRA_CLIENT_SECRET`.
6. If `deployEntraAppRegistration=false`, Bicep exports
   `ENTRA_APP_REGISTRATION_MANAGED=false` and the hook exits without error,
   even when `ENTRA_CLIENT_ID` contains an external registration ID. An unset
   flag also skips safely; an unexpected value fails without rotating a
   credential.

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

## Direct redeploy with an existing Entra registration (false-mode)

Use this path when you need to redeploy infra **without** re-provisioning the
Entra app registration — for example when the Graph Bicep extension is
unavailable, or when you manage the registration separately.

Use the **direct Azure CLI** commands below for this targeted flow rather than
`azd provision` or `azd up`, so unrelated full-provision effects remain out of
scope. Direct ARM deployments do not run azd hooks. If full azd provisioning is
used separately, the postprovision hook consumes
`ENTRA_APP_REGISTRATION_MANAGED=false` and will not rotate the external
registration even though its resolved client ID is exported as
`ENTRA_CLIENT_ID`. Deploy-only web updates remain a separate operation.

### Safety contract

`deployEntraAppRegistration=false` with a blank or whitespace-only
`entraClientIdOverride` **fails the ARM deployment before any Container App is
mutated**. This is enforced by `infra/modules/entra-auth-guard.bicep`: the
value is trimmed at the guard module call (`trim(entraClientIdOverride)`), so a
whitespace-only input (e.g. an env var that expands to spaces) normalises to
`''` before the `@minLength(1)` ARM
parameter constraint is evaluated. ARM validates this constraint when the nested
deployment is created, before the container-apps module runs. The Container Apps
module is in the compiled dependency graph of the guard's output, so it never
executes if the guard deployment fails. There is no silent auth erase and no
success-shaped fallback.

Two explicit modes are available via the `entraAuthMode` parameter (only
consulted when `deployEntraAppRegistration=false`):

| `entraAuthMode` | Effect |
|---|---|
| `existing` (default) | Preserves auth. Requires non-empty, non-whitespace `entraClientIdOverride`. The value is trimmed before the `minLength:1` check — blank or whitespace-only fails with `InvalidTemplate / minLength`. |
| `disabled` | Explicit operator opt-in to deploy without Entra authentication. No `ENTRA_CLIENT_ID`, `ENTRA_REDIRECT_URI`, or `ENTRA_POST_LOGOUT_REDIRECT_URI` are injected into the Container App. |

### Required non-secret metadata for `existing` mode

Before running a direct false-mode redeploy, gather these values (none are
secrets):

| Value | Where to find it |
|---|---|
| **Application (client) ID** | Azure Portal → Entra ID → App registrations → your app → Overview, or `az ad app list --display-name <name> --query '[0].appId' -o tsv` |
| **Resource group name** | `az group list --query "[?starts_with(name,'rg-')].name" -o tsv` or the value you set during provisioning |
| **Subscription ID** | `az account show --query id -o tsv` |
| **Other param values** | Previous deployment's non-secret parameters, reviewed without displaying secure values |

The deployed redirect URI (`ENTRA_REDIRECT_URI`) is derived automatically from
the Container Apps environment default domain — you do **not** pass it as a
parameter. Verify that the registered redirect URI on your Entra app
registration matches `https://<container-app-domain>/auth/callback`. If the
Container Apps environment was replaced (e.g. during the NAT cutover), update
the registered URI before running this redeploy.

### Safe `az deployment group what-if` command

Run the following as a pre-flight check before any direct false-mode redeploy.
First prepare an ignored, resolved ARM parameter file containing the existing
deployment's non-secret settings, including its container image, resource naming,
network, model, and feature settings. Do not rely on template defaults to preserve
those settings. Use the previous deployment's **parameters**, not its outputs,
as the reference, and omit the two secret-value parameters to avoid rotating them.
Do not copy secret values into this file or print them to the terminal.

The commands below use `resolved.parameters.json` for that operator-prepared file.
Do **not** pass `infra/main.parameters.json` directly to Azure CLI: its `${...}`
expressions are expanded by **azd**, not by `az deployment group`. Unresolved
expressions can fail validation or be sent as literal values, including secret
values. Replace the example variables below with the verified target and client ID.

```bash
az deployment group what-if \
  --resource-group "$RESOURCE_GROUP" \
  --template-file infra/main.bicep \
  --parameters @resolved.parameters.json \
  --parameters \
    deployEntraAppRegistration=false \
    entraAuthMode=existing \
    entraClientIdOverride="$ENTRA_CLIENT_ID"
```

> ⚠️ **PENDING** — no isolated sandbox has been used to validate a full
> false-mode `what-if` and subsequent `deployment group create` + login +
> protected-API smoke test end to end. This section documents the intended
> operator flow. Before using in production, a sandbox approval gate must be
> satisfied: run the `what-if` against an isolated resource group, review the
> diff, and confirm no unexpected auth env var removals appear in the output.

### Operator deployment example (gated on sandbox approval)

After the `what-if` diff has been reviewed and approved in sandbox:

```bash
az deployment group create \
  --resource-group "$RESOURCE_GROUP" \
  --template-file infra/main.bicep \
  --parameters @resolved.parameters.json \
  --parameters \
    deployEntraAppRegistration=false \
    entraAuthMode=existing \
    entraClientIdOverride="$ENTRA_CLIENT_ID"
```

**Do not** pass `appSessionSecretKeyValue` or `entraClientSecretValue` on the
command line or in the resolved file. Their empty Bicep defaults leave the
corresponding existing Key Vault secrets unchanged. This is a full infrastructure
redeploy, not a partial patch: the client ID override does not preserve unrelated
settings automatically. Stop if the preview changes the image, network posture,
authentication metadata, or any other setting unexpectedly.

**If `entraClientIdOverride` is omitted, blank, or whitespace-only**, the guard's
minimum-length constraint is intended to reject the deployment with an error
similar to:
```
InvalidTemplate: Deployment template validation failed: The template parameter
'entraClientIdOverride' has an invalid value: The value '' is below the
minimum length constraint of '1'.
```
The override is trimmed before this check, so a value of `"   "` (spaces only)
is normalised to `""` and fails the same way. No Container App mutation occurs.

### Intentional no-auth deploys (`entraAuthMode=disabled`)

If you deliberately want to deploy the app without Entra authentication (e.g.
during initial infrastructure bootstrapping before any app registration exists):

```bash
az deployment group create \
  --resource-group "$RESOURCE_GROUP" \
  --template-file infra/main.bicep \
  --parameters @resolved.parameters.json \
  --parameters \
    deployEntraAppRegistration=false \
    entraAuthMode=disabled
```

In this mode the Container App receives no `ENTRA_CLIENT_ID`,
`ENTRA_REDIRECT_URI`, or `ENTRA_POST_LOGOUT_REDIRECT_URI` env vars. The app
will start but all authenticated endpoints will be inaccessible until a
subsequent redeploy with `entraAuthMode=existing` and a valid
`entraClientIdOverride`.

### Redirect URI registration requirement

After any redeploy that changes the Container Apps environment (and therefore
the default domain), update the registered redirect URI on the Entra app
registration:

1. Retrieve the new Container Apps URL from deployment outputs:
   ```bash
   az deployment group show -g <rg> -n <deployment-name> \
     --query properties.outputs.deployedAuthRedirectUri.value -o tsv
   ```
2. Update the registration through its owning infrastructure deployment.
3. Include the new `https://<new-domain>/auth/callback` value in its redirect URIs.
4. Verify that `https://<new-domain>/` is also registered as the post-logout
   redirect URI if the app is configured to redirect after logout.

The `ENTRA_REDIRECT_URI` env var in the Container App is auto-derived from the
Container Apps environment default domain — it will be correct for the new
hostname as soon as the deployment completes. The Entra portal registration is
the external artifact that must match.
## Rotating the Entra client secret

This app can adopt a newer Key Vault version at runtime, but it does **not**
create, register, or delete Microsoft Entra application credentials for you.
Rotation therefore requires explicit operator steps:

1. Create a **new client secret credential in Microsoft Entra ID** for the
   existing app registration.
2. Store that new secret value as a **new version** of the Key Vault secret
   `entra-client-secret`.
3. Keep the **previous Entra credential active** until every running app
   replica has time to observe and use the new Key Vault version and the
   overlap window has elapsed.
4. Only then retire/delete the previous credential in Microsoft Entra ID.

Changing Key Vault alone does **not** mint a new Entra credential. If the new
Key Vault value is not already registered on the Entra app registration,
authentication will fail.

The app keeps the previously used OAuth client secret in memory for a short,
bounded overlap window (currently 15 minutes). New login attempts use the
latest Key Vault version as soon as it is observed, while callbacks may fall
back to the previous credential during that overlap to support a controlled
rotation.

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
