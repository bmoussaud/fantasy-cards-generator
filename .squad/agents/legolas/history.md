# Project Context

- **Owner:** Benoit Moussaud
- **Project:** Web application running on Azure, using AI Services provided by Azure AI Foundry
- **Stack:** TBD — Azure AI Foundry (models/agents), Azure hosting (App Service/Container Apps), frontend framework TBD
- **Created:** 2026-08-26T09:07:51Z

## Learnings

<!-- Append new learnings below. Each entry is something lasting about the project. -->
📌 Team update (2026-08-26T09:17:30Z): Initial architecture decisions were proposed in .squad/decisions.md (Azure App Service hosting; Azure AI Foundry text + image model flow), pending user confirmation on open questions.
📌 Role update (2026-08-26T09:36:32Z): The frontend is now Python-served/server-rendered, so Legolas should work within the Python UI layer (templates, HTMX-style interactivity, CSS, UX) alongside Aragorn instead of expecting a separate JS/TS frontend app.
📌 Team policy (2026-08-26T10:05:50Z): Use Conventional Commits for every future commit in this repo (`<type>[optional scope]: <description>`).
📌 Issue #59 fix (2026-09-03T08:15:45Z): My Cards images returned HTTP 403 because the library's SAS-URL image links can't reach a private-endpoint-only storage account. Replaced client-direct SAS URLs with the existing `/cards/{card_id}/image` backend-proxy route, removed `AzureBlobSasUrlSigner`/`create_asset_url_signer` and the unused Storage Blob Delegator RBAC role, and refreshed `tests/test_library.py` for the proxy route. PR: https://github.com/bmoussaud/fantasy-cards-generator/pull/60. Decision inbox note filed superseding my 2026-09-02 SAS decision.
📌 Team update (2026-09-03T08:46:26+0000): Issue #61 / PR #62 humanizes `/my/cards` timestamps with a reusable `format_card_timestamp` helper. Keep raw ISO-8601 values in `<time datetime>` attributes and render friendly UTC labels in templates so future server-rendered metadata follows the same convention.
📌 Saved-photo fix (2026-09-03T14:13:09+0000): The generator's saved-photo path is safest when HTMX request payloads are written explicitly at submit time (`htmx:configRequest`) instead of relying on toggling a hidden `saved_photo_id` input disabled/enabled. Regression coverage now includes a multipart UI submission that reaches `generate_image_edit()` with a saved photo and no fresh upload.
📌 Team update (2026-09-04T13:10:31.843+00:00): Gandalf published an architecture spec at docs/architecture-agents-foundry.md proposing a Foundry-hosted card-orchestrator agent (Microsoft Agent Framework); review for your domain's implications (backend integration / infra RBAC+Bicep / UI states / test strategy).
