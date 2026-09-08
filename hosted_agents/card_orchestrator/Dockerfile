FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /bin/uv

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1 \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra hosted-agent

COPY app ./app
COPY hosted_agents ./hosted_agents
RUN useradd --create-home --shell /usr/sbin/nologin agent

USER agent
EXPOSE 8088
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8088/readiness', timeout=2)"
CMD ["python", "-m", "hosted_agents.card_orchestrator"]
