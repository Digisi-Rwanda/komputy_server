.PHONY: dev migrate api worker test lint contracts
dev:        ## local services (MinIO); reuses the web app's Postgres on :5440
	docker compose up -d minio
migrate:    ## engine schema + queue tables
	uv run alembic upgrade head && uv run procrastinate --app=ledgerline.queue.app schema --apply 2>/dev/null || true
api:
	uv run uvicorn ledgerline.app:app --reload --port 8100
worker:     ## both queues in one process (dev)
	uv run procrastinate --app=ledgerline.queue.app worker --queues=io,calc --concurrency=2
test:
	uv run pytest -q
lint:
	uv run ruff check src tests && uv run ruff format --check src tests
contracts:  ## OpenAPI → typed client in the web app
	uv run python -c "import json;from ledgerline.app import app;print(json.dumps(app.openapi(),indent=1))" > contracts/openapi.json
	cd ../komputy_web && npx openapi-typescript ../komputy_server/contracts/openapi.json -o src/server/engine/schema.d.ts
