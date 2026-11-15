# Komputy server

**Naming:** the product is **Komputy** (komputy.com). "ECL" names only the IFRS 9 Expected Credit Loss module, never the product. No em or en dashes in text, code or docs (CI enforces both).

Headless calculation and accounting service for the Komputy web app (Next.js, `../komputy_web`):
IFRS 9 expected credit loss (PD / LGD / EAD / ECL) and IFRS 16 lease accounting. No users, passwords or UI:
it trusts short-lived RS256 tokens minted by the web app and verifies them against the web app's JWKS.

The calculation code in `src/komputy/ecl/domain` and `src/komputy/leases/domain` is the approved engine,
carried over unchanged with its tests (`tests/domain`, 130 tests).

## Architecture

| Process | Command | Role |
|---|---|---|
| API | `uvicorn komputy.app:app` | Private REST API (`/api/v1`, OpenAPI at `/api/v1/openapi.json`) |
| Worker | `procrastinate --app=komputy.queue.app worker --queues=io,calc` | Validation, compute, workbooks, lease imports, periodic jobs |

- **Queue:** Procrastinate on Postgres (no broker). `io` = validation, ingest, exports, imports; `calc` = ECL compute and lease books.
  In production run two workers: `--queues=io --concurrency=4` and `--queues=calc --concurrency=1` (large memory).
- **Database:** the engine owns the `engine` schema (Alembic, `alembic/`). It only reads the web app's tables
  (`users`, `tenants`, `tenant_memberships`, `member_service_roles`) and appends to `audit_logs`.
  Row-level security on every tenant table (bulk-loaded result tables excepted, see the migration).
- **Files:** S3 API: MinIO locally, Cloudflare R2 in production. Browsers upload straight to storage
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

Integration tests use their own database (`komputy_engine_test`, created once) and MinIO. Live browser
journeys across both apps: `../komputy_web/scripts/e2e-engine.mjs`, `e2e-leases.mjs`, `e2e-platform.mjs`.

## Deploy (Render)

Everything is in `render.yaml` (Blueprint: Postgres, Key Value, web, engine API, two workers, crons, nightly backup to R2)
and explained phase by phase in `docs/DEPLOYMENT_PLAN.md`; operations (rollback, restore, scaling, stuck jobs) in
`docs/RUNBOOK.md`. CI (`.github/workflows/ci.yml`) must pass before Render deploys `main`.
