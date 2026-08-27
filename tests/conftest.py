from __future__ import annotations

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("ENTRA_EXTERNAL_ID_CLIENT_ID", "client-id")
os.environ.setdefault("ENTRA_EXTERNAL_ID_CLIENT_SECRET", "client-secret")
os.environ.setdefault(
    "ENTRA_EXTERNAL_ID_AUTHORITY",
    "https://tenant.ciamlogin.com/tenant-id/v2.0",
)
os.environ.setdefault("ENTRA_EXTERNAL_ID_REDIRECT_URI", "https://testserver/auth/callback")
os.environ.setdefault("ENTRA_EXTERNAL_ID_POST_LOGOUT_REDIRECT_URI", "https://testserver/")
