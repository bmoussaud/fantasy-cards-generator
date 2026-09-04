### 2026-09-04: Secret provider foundation lives in `app/secrets.py`
**By:** Aragorn
**What:** Added the runtime secret-provider abstraction in `app/secrets.py`, with logical app secret names (`APP_SESSION_SECRET_KEY`, `ENTRA_CLIENT_SECRET`) mapped to the deployed Key Vault secret names and exposed on `app.state.secret_provider` via FastAPI lifespan.
**Why:** The remaining Key Vault rotation issues (#85/#86/#87) now have a single stable import path and app-level lifecycle hook for reuse without forcing auth/session consumers to know the vault naming details.
