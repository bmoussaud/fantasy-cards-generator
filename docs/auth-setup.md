# Microsoft Entra ID multi-tenant authentication setup

This app uses an OpenID Connect authorization code flow with PKCE against
Microsoft Entra ID. Sign-in is intentionally configured for
**organizational/work accounts from any Entra tenant** by using the
multi-tenant `/organizations` endpoint. Personal Microsoft accounts are not in
scope for this flow.

The FastAPI app stores only minimal authenticated user claims (`sub`, `name`,
`email`) in a signed session cookie and does not persist Entra tokens in the
browser session.

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

## Register the application in Microsoft Entra ID

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
`/organizations` authority string.

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
