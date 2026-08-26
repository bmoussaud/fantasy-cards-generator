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