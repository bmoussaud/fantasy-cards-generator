# Project Context

- **Owner:** Benoit Moussaud
- **Project:** Web application running on Azure, using AI Services provided by Azure AI Foundry
- **Stack:** TBD — Azure AI Foundry (models/agents), Azure hosting (App Service/Container Apps), frontend framework TBD
- **Created:** 2026-08-26T09:07:51Z

## Learnings

<!-- Append new learnings below. Each entry is something lasting about the project. -->
📌 Team update (2026-08-26T09:30:49Z): Benoit Moussaud explicitly overrode the initial hosting recommendation; the architecture now standardizes on Azure Container Apps instead of Azure App Service.
📌 Team policy (2026-08-26T09:34:19Z): All Azure resources must be provisioned through Bicep, preferring Azure Verified Modules (AVM) when suitable and using native/custom Bicep only as fallback.
📌 Team policy (2026-08-26T09:36:32Z): Python is now the primary application language for backend/API and project tooling; use `uv` workflows and keep Python config centered on `pyproject.toml`, with secrets staying in env/Key Vault/deployment config.
📌 Architecture update (2026-08-26T09:36:32Z): The frontend direction is now Python-served/server-rendered; keep UI architecture inside the Python app rather than planning a separate JS/TS frontend unless a later decision explicitly reopens that choice.
📌 Team policy (2026-08-26T09:39:08Z): Use Azure Developer CLI (`azd`) as the standard Azure provisioning/deployment workflow; ensure `azure.yaml` and an azd-compatible `infra/` structure sit on top of the Bicep + AVM baseline when infra work starts.
📌 Team policy (2026-08-26T09:42:05Z): Every CLI entry point must load local `.env` via `python-dotenv` for development convenience, while deployed runtime config still comes from injected env vars / Key Vault and `.env` must stay gitignored.
📌 Architecture draft (2026-08-26T09:50:16Z): Added a detailed MVP operating-decisions proposal covering 16 implementation gaps, with six items explicitly flagged for Benoit's confirmation: auth, cost caps, moderation, retention/deletion, async generation, and legal/IP posture.
📌 Team policy (2026-08-26T10:05:50Z): All future commits must use Conventional Commits (`<type>[optional scope]: <description>`); Gandalf and reviewers enforce this repo-wide.
📌 Team update (2026-08-28T08:36:20Z): Reviewed PR #29 and PR #33 in two passes each (request changes, then approve), and recorded that ACA-native secret mirroring is an acceptable tactical restore-deployability workaround but not the target Key Vault design.
📌 Team update (2026-08-28T08:36:20Z): Today's deploy incident traced to Azure Container Apps failing to resolve Key Vault secretRefs despite correct managed-identity and RBAC wiring, so future reviews should treat direct Key Vault runtime rotation claims as unproven until the platform path is re-validated.
📌 Work update (2026-08-31T12:12:46.587+00:00): Reviewed least-privilege architecture and acceptance criteria for azd deployer read access to Cosmos DB and Blob Storage; confirmed this security-critical access-control work must route to Gimli rather than @copilot.
📌 Team update (2026-09-02T14:10:23+0000): PR #57 for issue #52 shipped successfully against `main`, and the PR body notes this repo currently has no `dev` base branch despite the shared git-workflow skill assuming one — possible skill/doc drift to reconcile.
📌 Team update (2026-09-02T16:31:07.441+00:00): Issue #8 established a fail-closed library access pattern: per-user Cosmos scoping plus 404 on cross-user card lookups, with short-lived user-delegation Blob SAS URLs for direct browser image reads.

📌 Architecture spec (2026-09-04T13:10:31.843+00:00): Produced the Foundry + Microsoft Agent Framework design spec at `docs/architecture-agents-foundry.md`, recommending a hosted card-orchestrator agent while keeping auth, persistence, and image retry in the existing app.
📌 Team update (2026-09-04T13:51:08+00:00): Hosted-agent region/runtime research for issue #96 landed in `docs/architecture-agents-foundry.md` and PR #105; eastus2 is the likely target region with medium-high confidence on Agent Service + `gpt-5.5`/`gpt-image-2` support, but quota remains unresolved and needs a live Azure check before implementation proceeds. — decided by Gimli
