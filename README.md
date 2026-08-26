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

The default Foundry deployment aliases remain `gpt-5-5` and `gpt-image-2` for
application compatibility. In `eastus2`, they target `gpt-5.5`
(`2026-04-24`, `GlobalStandard`) and `gpt-image-2`
(`2026-04-21`, `GlobalStandard`), respectively.

Model names, versions, SKUs, and capacities are parameters in
`infra/main.bicep`. Confirm alternatives in the target region's live Azure AI
Foundry catalog before overriding them because availability and quota vary by
subscription and region.
