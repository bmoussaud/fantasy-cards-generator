from __future__ import annotations

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("ENTRA_CLIENT_ID", "client-id")
os.environ.setdefault("ENTRA_CLIENT_SECRET", "client-secret")
os.environ.setdefault("ENTRA_AUTHORITY", "https://login.microsoftonline.com/organizations/v2.0")
os.environ.setdefault("ENTRA_REDIRECT_URI", "https://testserver/auth/callback")
os.environ.setdefault("ENTRA_POST_LOGOUT_REDIRECT_URI", "https://testserver/")
