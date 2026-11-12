# Security Review: LinkedIn OIDC Pilot

Date: 2026-11-12

Scope: GitHub issue #176 and its planned developer-pilot LinkedIn OIDC
integration. This review covered OAuth replay and owner binding, optional
remote-image retrieval, secret handling, telemetry, and pilot access control.
No application code, live LinkedIn request, Azure operation, credential, or
deployment was tested.

## Finding

### High: OAuth state lacks a server-authoritative single-use contract

The plan currently relies on signed browser-session state for the LinkedIn OAuth
flow and transient creation draft. A copied pre-callback cookie can be replayed,
and client-side state alone cannot guarantee atomic consumption across Container
App replicas.

The implementation must use an expiring server-side single-use record that binds
an opaque state handle to:

- the canonical Entra `<tid>:<oid>` owner;
- nonce and PKCE context;
- the creation-draft identifier;
- explicit photo consent;
- the validated return target;
- the expiry timestamp.

The callback must verify the current Entra owner and atomically consume the
record. Missing, mismatched, expired, owner-mismatched, or consumed records must
fail closed. The signed browser cookie may carry opaque identifiers and bounded
display state, but it must not be the replay or consumption authority.

## Existing Controls

- Entra `<tid>:<oid>` remains the canonical ownership boundary.
- The planned flow uses PKCE, nonce, CSRF protection, exact `openid profile`
  scopes, and field minimization.
- The developer pilot is disabled by default and restricted by a canonical-owner
  allowlist.
- LinkedIn secrets follow the existing Key Vault and Container Apps
  `secretRef` pattern.
- Telemetry remains content-free, and callback query values must be stripped.
- Positive remote-photo retrieval remains disabled until a separately authorized
  contract test establishes an exact host allowlist.

## Disposition

Issue #176 is not security-qualified until its contract requires the
server-authoritative, owner-bound, atomic single-use OAuth state described above.
After that correction, the name-only developer pilot can be qualified; positive
photo retrieval remains separately gated.
