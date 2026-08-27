# Microsoft Entra External ID authentication setup

This app uses an OpenID Connect authorization code flow with PKCE against Microsoft Entra External ID. The FastAPI app stores only minimal authenticated user claims (`sub`, `name`, `email`) in a signed session cookie and does not persist Entra tokens in the browser session.

## Required environment variables

Add these values to `.env` for local development:

```dotenv
APP_SESSION_SECRET_KEY=<long-random-secret>
ENTRA_EXTERNAL_ID_CLIENT_ID=<application-client-id>
ENTRA_EXTERNAL_ID_CLIENT_SECRET=<application-client-secret>
ENTRA_EXTERNAL_ID_AUTHORITY=https://<tenant-subdomain>.ciamlogin.com/<tenant-id-or-domain>/v2.0
ENTRA_EXTERNAL_ID_REDIRECT_URI=https://localhost:8000/auth/callback
ENTRA_EXTERNAL_ID_POST_LOGOUT_REDIRECT_URI=https://localhost:8000/
ENTRA_EXTERNAL_ID_SCOPES=openid profile email
```

- `APP_SESSION_SECRET_KEY` must be a strong random value generated per environment.
- `ENTRA_EXTERNAL_ID_AUTHORITY` should match the tenant that issues the ID token. For External ID customer tenants, use the `ciamlogin.com` authority, not `login.microsoftonline.com`.
- `ENTRA_EXTERNAL_ID_REDIRECT_URI` must exactly match the app registration.
- `ENTRA_EXTERNAL_ID_SCOPES` defaults to `openid profile email`.

## Register the application in Microsoft Entra External ID

1. Sign in to the [Microsoft Entra admin center](https://entra.microsoft.com/) and switch to your **External tenant**.
2. Go to **Applications** or **App registrations** and create a new **Web** application registration for this app.
3. Add the callback redirect URI for each environment, for example:
   - `https://localhost:8000/auth/callback`
   - `https://<your-app-domain>/auth/callback`
4. Create a **client secret** and copy the secret **value**.
5. Record:
   - **Application (client) ID**
   - **Directory (tenant) ID**
   - your tenant subdomain / primary domain
6. Ensure the sign-in journey requests OpenID Connect scopes `openid profile email`.

## Build the authority URL

For External ID customer tenants, set:

```text
https://<tenant-subdomain>.ciamlogin.com/<tenant-id-or-domain>/v2.0
```

The app derives OIDC discovery from:

```text
https://<tenant-subdomain>.ciamlogin.com/<tenant-id-or-domain>/v2.0/.well-known/openid-configuration
```

This lets Authlib fetch the authorization endpoint, token endpoint, JWKS signing keys, and issuer metadata needed to validate the ID token.

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

Access tokens, refresh tokens, and raw ID tokens are not persisted in the session cookie.

## Logout behavior

- `/auth/logout` always clears the local signed session cookie.
- If `ENTRA_EXTERNAL_ID_POST_LOGOUT_REDIRECT_URI` is configured, the app also sends the browser to the Entra logout endpoint with that redirect target.

## Future extensibility

Social or other federated identity providers are intentionally out of scope for this issue, but External ID can add them later through tenant configuration without changing the app's core OIDC session handling.

## Manual verification still required

The automated test suite mocks the identity provider. Before production use, verify the registration and callback flow against a real Entra External ID tenant to confirm:

- issuer matches the configured `ciamlogin.com` authority
- the registered redirect URIs are exact
- the client secret is valid
- logout redirects behave as expected in each environment
