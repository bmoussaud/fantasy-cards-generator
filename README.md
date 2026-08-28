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
uv run pytest -q
```

## Card generation configuration

The single synchronous card-generation flow supports two local modes:

- `AI_MODE=mock` + `PERSISTENCE_MODE=memory` for deterministic development/tests
- `AI_MODE=live` + `PERSISTENCE_MODE=azure` for Azure AI Foundry + Cosmos DB + Blob Storage

See `docs/card-generation-api.md` for the API contract, moderation policy, and
runtime settings.

## Azure deployment

Provision the application with Azure Developer CLI:

```bash
azd env new dev
azd env set AZURE_LOCATION eastus2
azd env set LEGACY_COSMOS_IP_RULE 20.10.253.231
azd up
```

`azd up` provisions Azure Container Registry and Azure Container Apps, builds the
production Docker image from `Dockerfile`, pushes it to the provisioned registry,
and deploys the `web` service to Container Apps on port 8000.

The current dev/MVP rollout uses a workload-profile Container Apps environment
behind a dedicated VNet subnet + NAT Gateway so Cosmos DB can allowlist the
stable NAT public IP instead of a platform-assigned ACA outbound IP. Keep
`LEGACY_COSMOS_IP_RULE=20.10.253.231` for the first parallel cutover deploy so
the existing incident stopgap remains in place until the NAT-backed environment
passes smoke tests; clear it afterward with:

```bash
azd env set LEGACY_COSMOS_IP_RULE ""
azd provision
```

See `infra/README.md` for the NAT cutover, rollback, operational checks, and
the manual Entra redirect verification required when the replacement Container
Apps domain changes.

The default Foundry deployment aliases remain `gpt-5-5` and `gpt-image-2` for
application compatibility. In `eastus2`, they target `gpt-5.5`
(`2026-04-24`, `GlobalStandard`) and `gpt-image-2`
(`2026-04-21`, `GlobalStandard`), respectively.

Model names, versions, SKUs, and capacities are parameters in
`infra/main.bicep`. Confirm alternatives in the target region's live Azure AI
Foundry catalog before overriding them because availability and quota vary by
subscription and region.
