# fantasy-cards-generator

## Architecture

[![Implemented architecture: FastAPI on Azure Container Apps, Foundry hosted-agent
orchestration, and private data services](docs/images/architecture.png)](docs/images/architecture.svg)

FastAPI owns authentication, moderation, image generation, and persistence.
All live card-text generation uses the Foundry hosted `card-orchestrator`;
there is no direct-model path or automatic fallback. Cosmos DB, Blob Storage,
and Key Vault are reached through private endpoints; browsers receive artwork
through the backend.

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

The issue qualification and remote Copilot execution process is documented in
[`docs/issue-to-copilot-workflow.md`](docs/issue-to-copilot-workflow.md).

## Card generation configuration

Application construction always uses the live Foundry agent and image clients;
there is no runtime environment switch for mocks. Automated tests inject their
deterministic clients explicitly. Startup requires valid local agent and
image-model configuration plus working Application Insights telemetry.
Dependency readiness then requires the exact active hosted-agent name/version
and managed-identity access before the revision receives traffic.

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
azd env set CARD_ORCHESTRATOR_VERSION "$(git rev-parse HEAD)"
azd env set CARD_ORCHESTRATOR_ENABLE_PREREQUISITES true
azd env set CARD_ORCHESTRATOR_CREATE_REGISTRY_CONNECTION true
azd up
```

`azd up` performs the clean bootstrap in dependency order: it provisions the
shared Foundry/ACR/Container Apps resources with the public placeholder image,
deploys `card-orchestrator`, copies azd's generated
`AGENT_CARD_ORCHESTRATOR_NAME` and `AGENT_CARD_ORCHESTRATOR_VERSION` values into
the web deployment inputs, re-provisions the Container App with that exact
active agent identity, and only then deploys `web-nat` on port 8000. The
postdeploy hook rejects missing or malformed agent output instead of allowing an
empty `FOUNDRY_AGENT_NAME` or `FOUNDRY_AGENT_VERSION`. The repeated provision
does not rotate an existing managed Entra client secret; clear that azd value
only as part of an explicit credential-rotation operation.

The workload-profile Container Apps environment uses the delegated `aca-infra`
subnet and a NAT Gateway with a static public IP for public-service egress.
Cosmos DB, Blob Storage, and Key Vault have public network access disabled and
use private endpoints in the `private-endpoints` subnet, with private DNS zones
linked to the VNet. NAT allowlisting is not the Cosmos connectivity path.

See `infra/README.md` for infrastructure configuration, historical NAT cutover notes, and
the manual Entra redirect verification required when the replacement Container
Apps domain changes.

For the one-time dev-only Paul Smith and Jane Smith Entra user provisioning
setup, including Graph administrator consent, PIM activation, secure password
configuration, and the manual script flow, see
[`docs/entra-user-provisioning.md`](docs/entra-user-provisioning.md). Do not
commit passwords or place them in `.env` files tracked by source control.

Application telemetry is disabled locally by default and enabled in Azure through
the existing Application Insights connection-string secret. Monitoring resources,
safe defaults, alert routing, privacy exclusions, KQL, cost controls, and rollback
are documented in [`docs/operational-monitoring.md`](docs/operational-monitoring.md).
Azure SDK GenAI tracing is experimental; deployed runtimes set
`AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING=true`, and local developers can set the
same value in their ignored `.env` only when exercising telemetry.

The hosted agent uses the `gpt-5-5` deployment and the web backend uses
`gpt-image-2` for images. In `eastus2`, they target `gpt-5.5`
(`2026-04-24`, `GlobalStandard`) and `gpt-image-2`
(`2026-04-21`, `GlobalStandard`), respectively.

Model names, versions, SKUs, and capacities are parameters in
`infra/main.bicep`. Confirm alternatives in the target region's live Azure AI
Foundry catalog before overriding them because availability and quota vary by
subscription and region.
