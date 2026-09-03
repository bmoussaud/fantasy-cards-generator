### 2026-09-03: Harden blank optional card-generation form fields
**By:** Aragorn
**What:** Normalize blank or whitespace-only card-generation form strings to `None` before validation, and add a matching `CardGenerateBody` validator for `idempotencyKey`, `csrfToken`, and `savedPhotoId`.
**Why:** Issue #69 showed that an always-present hidden `saved_photo_id` field could submit `""` and trigger a 422 for every generation request. Hardening both form parsing and the request model covers `saved_photo_id`, `photo_label`, `idempotency_key`, `csrf_token`, and `quality` consistently while preserving CSRF failures for missing or blank tokens.
