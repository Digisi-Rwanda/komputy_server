# Ledgerline Engine

Headless calculation and accounting service for the Ledgerline web app (Next.js, `../komputy_web`):
IFRS 9 expected credit loss (PD / LGD / EAD / ECL) and IFRS 16 lease accounting. No users, passwords or UI —
it trusts short-lived RS256 tokens minted by the web app and verifies them against the web app's JWKS.

The calculation code in `src/ledgerline/ecl/domain` and `src/ledgerline/leases/domain` is the approved engine,
carried over unchanged with its tests (`tests/domain`, 130 tests).

## Architecture

| Process | Command | Role |
|---|---|---|
| API | `uvicorn ledgerline.app:app` | Private REST API (`/api/v1`, OpenAPI at `/api/v1/openapi.json`) |
| Worker | `procrastinate --app=ledgerline.queue.app worker --queues=io,calc` | Validation, compute, workbooks, lease imports, periodic jobs |

- **Queue:** Procrastinate on Postgres (no broker). `io` = validation, ingest, exports, imports; `calc` = ECL compute and lease books.
  In production run two workers: `--queues=io --concurrency=4` and `--queues=calc --concurrency=1` (large memory).
- **Database:** the engine owns the `engine` schema (Alembic, `alembic/`). It only reads the web app's tables
  (`users`, `tenants`, `tenant_memberships`, `member_service_roles`) and appends to `audit_logs`.
  Row-level security on every tenant table (bulk-loaded result tables excepted, see the migration).
- **Files:** S3 API — MinIO locally, Cloudflare R2 in production. Browsers upload straight to storage
  (presigned multipart); downloads use short-lived signed links.
- **Events:** the engine writes `engine.outbox_events` and NOTIFYs `engine_events`; the web app streams live
  progress (SSE) and turns events into notifications and emails using each person's preferences.

## Run locally

```bash
make dev        # MinIO on :9010 (uses the web app's Postgres on :5440)
make migrate    # engine schema + queue tables
make api        # http://127.0.0.1:8100  (web app: ENGINE_URL=http://127.0.0.1:8100)
make worker     # both queues
```

`.env.example` lists every setting. The engine DB role needs: ownership of schema `engine`, `SELECT` on the web
tables, `INSERT` on `public.audit_logs`, `UPDATE (is_read, read_at)` on `public.user_notifications`.
For production set `WEB_DB_ROLE` when migrating so the web app can consume engine events.

## Tests

```bash
uv run pytest -q          # 141: domain maths + integration (ECL flow with review, all lease journeys, isolation)
```

Integration tests use their own database (`ledgerline_engine_test`, created once) and MinIO. Live browser
journeys across both apps: `../komputy_web/scripts/e2e-engine.mjs`, `e2e-leases.mjs`, `e2e-platform.mjs`.

## Deploy (Render)

One image (`Dockerfile`), three services: **api** (private service, health check `/healthz`, pre-deploy
`alembic upgrade head`), **worker-io**, **worker-calc**. Postgres: Render Postgres. Files: Cloudflare R2
(bucket CORS must allow `PUT` from the web origin and expose the `ETag` header). Secrets: `DATABASE_URL`,
`STORAGE_*`, `SERVICE_TOKEN`, `WEB_JWKS_URL`, `SENTRY_DSN` (optional).
