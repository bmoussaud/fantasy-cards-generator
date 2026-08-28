# Project Context

- **Owner:** Benoit Moussaud
- **Project:** Web application running on Azure, using AI Services provided by Azure AI Foundry
- **Stack:** TBD — Azure AI Foundry (models/agents), Azure hosting (App Service/Container Apps), frontend framework TBD
- **Created:** 2026-08-26T09:07:51Z

## Learnings

<!-- Append new learnings below. Each entry is something lasting about the project. -->
📌 Team update (2026-08-26T09:17:30Z): Initial architecture decisions were proposed in .squad/decisions.md (Azure App Service hosting; Azure AI Foundry text + image model flow), pending user confirmation on open questions.
📌 Team policy (2026-08-26T10:05:50Z): Use Conventional Commits for every future commit in this repo (`<type>[optional scope]: <description>`).
📌 Team policy (2026-08-26T09:36:32Z): Backend/API implementation standardizes on Python; use `uv` for dependency/env/command workflows and keep Python project/tool configuration in `pyproject.toml`, not scattered config files where avoidable.
📌 Team policy (2026-08-26T09:42:05Z): Backend CLI/application entry points must call `python-dotenv` (for example `load_dotenv()`) on startup for local development, add the dependency with `uv`, and never treat `.env` as a production config source.
📌 Team update (2026-08-26T12:15:37Z): Core data infra landed in PR #13 (`squad/5-provision-core-data-services`) with Bicep/AVM bindings for Cosmos DB, Blob Storage, and Azure AI Foundry; use those new infra modules as the backend integration baseline for issues #6 and #7.
📌 Team update (2026-08-28T08:36:20Z): Verified via Authlib tracing that `httpx` is a required runtime dependency for the Microsoft Entra auth path and supported the day's fixes with pytest validation.
📌 Team update (2026-08-28T08:36:20Z): The live auth entrypoint now redirects correctly after the PR #33 deploy recovery, so backend follow-up can treat the current blocker as infrastructure secret delivery rather than application auth logic.
