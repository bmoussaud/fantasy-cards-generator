FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_DEFAULT_INDEX=https://packagefeedproxy.microsoft.io/pypi/simple/ \
    UV_INDEX_URL=https://packagefeedproxy.microsoft.io/pypi/simple/

COPY pyproject.toml uv.lock ./

RUN uv sync --no-dev --frozen

COPY app ./app

RUN useradd --create-home --shell /usr/sbin/nologin appuser

EXPOSE 8000

USER appuser

CMD ["uv","run","--no-dev","--no-sync","uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
