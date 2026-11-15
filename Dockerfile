# One image, four roles (set the command per Render service):
#   api:         default CMD (uvicorn; process count from WEB_CONCURRENCY)
#   worker-io:   procrastinate --app=ledgerline.queue.app worker --queues=io --concurrency=4
#   worker-calc: procrastinate --app=ledgerline.queue.app worker --queues=calc --concurrency=1
#   (periodic jobs run inside the workers; no separate scheduler process)
# Pre-deploy (api): sh scripts/predeploy.sh (Alembic + queue schema, idempotent). Workers exit cleanly on SIGTERM.
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
COPY scripts ./scripts
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8000 WEB_CONCURRENCY=2
USER app
EXPOSE 8000
# exec: uvicorn becomes PID 1 so Render's SIGTERM reaches it (graceful shutdown). Health: Render checks /healthz.
CMD ["sh", "-c", "exec uvicorn ledgerline.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*' --timeout-graceful-shutdown 25"]
