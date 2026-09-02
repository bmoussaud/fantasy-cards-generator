# Project Context

- **Owner:** Benoit Moussaud
- **Project:** Web application running on Azure, using AI Services provided by Azure AI Foundry
- **Stack:** TBD — Azure AI Foundry (models/agents), Azure hosting (App Service/Container Apps), frontend framework TBD
- **Created:** 2026-08-26T09:07:51Z

## Learnings

<!-- Append new learnings below. Each entry is something lasting about the project. -->
📌 Team update (2026-08-26T09:17:30Z): Initial architecture decisions were proposed in .squad/decisions.md (Azure App Service hosting; Azure AI Foundry text + image model flow), pending user confirmation on open questions.
📌 Team policy (2026-08-26T10:05:50Z): Use Conventional Commits for every future commit in this repo (`<type>[optional scope]: <description>`).
📌 Work update (2026-09-01T17:19:00.962+00:00): Updated deployment-config regression test coverage for preprovision hook safety and Bicep secret-gating logic. Test suite: 17/17 passing. Coverage includes hook file structure/safety guards, azure.yaml wiring, and double-gate verification (ACA native secret + env secretRef both gated by same non-empty condition). All tests redacted; no secret values exposed in test output or fixtures.
