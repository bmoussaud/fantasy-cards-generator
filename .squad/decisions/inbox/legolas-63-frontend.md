### 2026-09-03: Profile photo upload UI for issue 63
**By:** Legolas
**What:** Added an optional profile photo file input to the card generator form, wired the HTMX form for multipart submission, and added inline client-side preview/validation messaging for JPEG, PNG, and WebP uploads up to 5 MB.
**Why:** This keeps the new reference-image capability visible and understandable in the existing server-rendered UI while matching Aragorn's backend upload contract and preserving the current error-panel flow for server-side validation failures.
