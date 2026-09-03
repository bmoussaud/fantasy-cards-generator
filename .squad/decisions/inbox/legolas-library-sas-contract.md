# 2026-09-02 — Authenticated library uses 5-minute user-delegation Blob SAS URLs

## Context

Issue #8 adds a signed-in "My Cards" library backed by Cosmos metadata and
private Blob artwork. The browser needs direct, time-bounded access to image
assets without making the container app a permanent image proxy.

## Decision

- Library pages mint **read-only user-delegation SAS URLs** for card artwork.
- SAS expiry is fixed at **5 minutes**, staying well inside the storage
  account's existing 15-minute SAS policy window.
- The Container App runtime identity keeps container-scoped Blob Data
  Contributor for uploads and gains account-scoped **Storage Blob Delegator**
  only for SAS signing.
- Cross-user detail lookups fail closed as **404** from the authenticated
  user's partition rather than proving another owner's card exists.

## Why

- User-delegation SAS keeps storage private, avoids Shared Key access, and
  matches the repo's Entra-first posture.
- Five minutes is long enough for normal page loads/reloads but short enough to
  limit replay value if a URL leaks.
- Returning 404 on non-owned card IDs avoids existence disclosure while still
  enforcing strict per-user scoping.
