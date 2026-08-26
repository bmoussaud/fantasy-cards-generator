# Project Context

- **Owner:** Benoit Moussaud
- **Project:** Web application running on Azure, using AI Services provided by Azure AI Foundry
- **Stack:** TBD — Azure AI Foundry (models/agents), Azure hosting (App Service/Container Apps), frontend framework TBD
- **Created:** 2026-08-26T09:07:51Z

## Learnings

<!-- Append new learnings below. Each entry is something lasting about the project. -->
📌 Team update (2026-08-26T09:17:30Z): Initial architecture decisions were proposed in .squad/decisions.md (Azure App Service hosting; Azure AI Foundry text + image model flow), pending user confirmation on open questions.
📌 Team update (2026-08-26T09:30:49Z): Hosting is now standardized on Azure Container Apps by explicit user override, so IaC/deployment work should target Container Apps rather than Azure App Service.
📌 Team policy (2026-08-26T09:34:19Z): Enforce Bicep for all Azure provisioning work; prefer Azure Verified Modules (AVM) when they fit, and fall back to native/custom Bicep only when AVM coverage is insufficient.
📌 Team policy (2026-08-26T09:36:32Z): CI/CD, container builds, and deployment automation should align with Python `uv` workflows, while secrets/settings stay in env vars, Azure Key Vault, or deployment configuration rather than `pyproject.toml`.
📌 Team policy (2026-08-26T09:39:08Z): Use Azure Developer CLI (`azd`) as the standard orchestration layer for provisioning/deployment; scaffold `azure.yaml` and an azd-compatible `infra/` layout alongside the Bicep + AVM baseline when infra work starts.
📌 Team policy (2026-08-26T09:42:05Z): Ensure `.env` remains gitignored and `.env.sample` / `.env.example` exists for local developer setup, while deployed runtime config continues to come from Azure-injected env vars, `azd`, and Key Vault rather than `.env`.
📌 Team policy (2026-08-26T10:04:13Z): Standardize on exactly two `azd` environments — `dev` and `prod` only — with no staging tier for future provisioning and pipeline work.
📌 Team policy (2026-08-26T10:05:50Z): Use Conventional Commits for every future commit in this repo (`<type>[optional scope]: <description>`).
📌 Team update (2026-08-26T12:05:01Z): Added Bicep modules for Cosmos DB serverless (`cards` container only), private Blob Storage (`card-assets`), and Azure AI Foundry account/project/model deployments with Container Apps managed-identity RBAC; exact Foundry model versions/SKUs still need live quota/catalog confirmation.
