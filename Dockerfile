# One image, four roles (set the command per Render service):
#   api:         uvicorn ledgerline.app:app --host 0.0.0.0 --port $PORT --workers 2 --proxy-headers
#   worker-io:   procrastinate --app=ledgerline.queue.app worker --queues=io --concurrency=4
#   worker-calc: procrastinate --app=ledgerline.queue.app worker --queues=calc --concurrency=1
#   (periodic jobs run inside the workers; no separate scheduler process)
# Pre-deploy (api): alembic upgrade head (first deploy also: procrastinate --app=ledgerline.queue.app schema --apply)
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8000
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz')" || exit 1
CMD ["sh", "-c", "uvicorn ledgerline.app:app --host 0.0.0.0 --port ${PORT} --workers 2 --proxy-headers --forwarded-allow-ips='*'"]
