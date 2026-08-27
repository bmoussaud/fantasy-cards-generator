# fantasy-cards-generator

## Prerequisites

- [uv](https://docs.astral.sh/uv/)
- Python 3.12+

## Local setup

1. Copy the local environment template:
   ```bash
   cp .env.example .env
   ```
2. Sync dependencies:
   ```bash
   uv sync
   ```

## Run the app

```bash
uv run uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000.

Authentication testing requires HTTPS on `https://localhost:8000` because the session cookie is
`Secure` and the default Entra redirect URIs in `.env.example` use HTTPS. Plain HTTP is fine only
for anonymous local UI work.

## Authentication setup

Authentication is implemented with Microsoft Entra ID using the multi-tenant
`/organizations` OIDC authorization code flow + PKCE. Copy `.env.example` to
`.env`, provide the Entra values, and follow `docs/auth-setup.md` for app
registration details.

## Run tests

```bash
uv run pytest
```

## Azure deployment

Provision the application with Azure Developer CLI:

```bash
azd env new dev
azd env set AZURE_LOCATION eastus2
azd up
```

`azd up` provisions Azure Container Registry and Azure Container Apps, builds the
production Docker image from `Dockerfile`, pushes it to the provisioned registry,
and deploys the `web` service to Container Apps on port 8000.

The default Foundry deployment aliases remain `gpt-5-5` and `gpt-image-2` for
application compatibility. In `eastus2`, they target `gpt-5.5`
(`2026-04-24`, `GlobalStandard`) and `gpt-image-2`
(`2026-04-21`, `GlobalStandard`), respectively.

Model names, versions, SKUs, and capacities are parameters in
`infra/main.bicep`. Confirm alternatives in the target region's live Azure AI
Foundry catalog before overriding them because availability and quota vary by
subscription and region.
