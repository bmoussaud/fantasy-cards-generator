# fantasy-cards-generator

## Architecture

[![Implemented architecture: FastAPI on Azure Container Apps, optional Foundry
orchestration, and private data services](docs/images/architecture.png)](docs/images/architecture.svg)

FastAPI owns authentication, moderation, image generation, and persistence.
Text generation uses direct Azure OpenAI calls by default, with an opt-in
Foundry hosted orchestrator. Cosmos DB, Blob Storage, and Key Vault are reached
through private endpoints; browsers receive artwork through the backend.

[Editable draw.io source](docs/architecture.drawio) |
[Self-contained SVG](docs/images/architecture.svg) |
[Architecture details and source evidence](docs/architecture.md)

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
uv run uvicorn app.entrypoint:app --reload
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

Signed-in users can open `/app` to generate cards and `/my/cards` to browse
their own saved cards. In Azure persistence mode, the library reads card
metadata from Cosmos DB and streams Blob artwork through the same
backend-proxy image route (`GET /cards/{card_id}/image`) used by the card
generation flow, so the browser never talks to Blob Storage directly.

## Azure deployment

Provision the application with Azure Developer CLI:

```bash
azd env new dev
azd env set AZURE_LOCATION eastus2
azd up
```

`azd up` provisions Azure Container Registry and Azure Container Apps, builds the
production Docker image from `Dockerfile`, pushes it to the provisioned registry,
and deploys the `web-nat` service to Container Apps on port 8000.

The workload-profile Container Apps environment uses the delegated `aca-infra`
subnet and a NAT Gateway with a static public IP for public-service egress.
Cosmos DB, Blob Storage, and Key Vault have public network access disabled and
use private endpoints in the `private-endpoints` subnet, with private DNS zones
linked to the VNet. NAT allowlisting is not the Cosmos connectivity path.

See `infra/README.md` for infrastructure configuration, historical NAT cutover notes, and
the manual Entra redirect verification required when the replacement Container
Apps domain changes.

Application telemetry is disabled locally by default and enabled in Azure through
the existing Application Insights connection-string secret. Monitoring resources,
safe defaults, alert routing, privacy exclusions, KQL, cost controls, and rollback
are documented in [`docs/operational-monitoring.md`](docs/operational-monitoring.md).
Azure SDK GenAI tracing is experimental; deployed runtimes set
`AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING=true`, and local developers can set the
same value in their ignored `.env` only when exercising telemetry.

The default Foundry deployment aliases remain `gpt-5-5` and `gpt-image-2` for
application compatibility. In `eastus2`, they target `gpt-5.5`
(`2026-04-24`, `GlobalStandard`) and `gpt-image-2`
(`2026-04-21`, `GlobalStandard`), respectively.

Model names, versions, SKUs, and capacities are parameters in
`infra/main.bicep`. Confirm alternatives in the target region's live Azure AI
Foundry catalog before overriding them because availability and quota vary by
subscription and region.
