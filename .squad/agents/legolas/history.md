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
