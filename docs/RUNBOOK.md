# Komputy production runbook

Where things are, and what to do when something goes wrong. Architecture and the reasons behind it: `DEPLOYMENT_PLAN.md`.

## Map

| What | Where |
|---|---|
| Blueprint (all infra) | `render.yaml` in this repo → Render Blueprint `komputy` (auto-sync on push to `main`) → Render project **Komputy**, environment `production`. Render added the suffix `-woqc` to every name when the Blueprint was created; it is part of each resource's identity, so leave it (renaming in the dashboard would make the Blueprint create duplicates). |
| Web | Render web `komputy-web-*` · repo `Digisi-Rwanda/komputy_web` · health `/api/healthz` |
| Engine API | Render private service `komputy-engine-api-*` · health `/healthz`, readiness `/readyz` (db, storage, live workers) |
| Workers | `komputy-worker-io-*` (validation, ingest, exports, imports, housekeeping), `komputy-worker-calc-*` (compute, lease books) |
| Crons | `komputy-cron-events` (every min), `-expire` (5 min), `-daily` (06:00 UTC), `-backup` (01:00 UTC) |
| Database | Render Postgres 17 `komputy-db-*` (private network only; PITR on) |
| Key Value | `komputy-kv-*` (Better Auth sessions cache + rate limits, `noeviction`) |
| Files | Cloudflare R2 `komputy-prod` (uploads, results), `komputy-backups` (nightly dumps, 30-day expiry) |
| DNS | Cloudflare zone `komputy.com` |
| Email | Resend, domain `komputy.com` (records `send`, `rsend`, `resend._domainkey`, `_dmarc`: never delete) |
| CI | GitHub Actions `ci` in both repos; Render deploys `main` only after it passes |

## Deploy

Push to `main`. CI runs → Render builds → **pre-deploy** runs migrations (web: `npm run predeploy`; engine:
`sh scripts/predeploy.sh`) → new instances pass their health check → traffic switches. A failed pre-deploy or health
check leaves the old version serving. Migrations must be backward compatible (add first, remove in a later release).

New web table/column: add `db/migrations/NNNN_name.sql` (next number), then `npm run db:pull` locally to refresh
`schema.ts`. New engine table/column: Alembic revision in this repo.

## Roll back

- **Code:** Render dashboard → service → Events → pick the last good deploy → *Rollback*. (Auto-deploy keeps running on
  the next push, so also revert the commit on `main`.)
- **DNS:** Cloudflare → `komputy.com` → DNS: restore the previous record (TTL is 300 s). Today `komputy.com` and
  `www` are DNS-only CNAMEs to `komputy-web-woqc.onrender.com` (switched 2026-11-15). Before that they pointed at
  Vercel (`9cd10d4a143c0fd3.vercel-dns-017.com`, with `_vercel` TXT records `vc-domain-verify=komputy.com,cfd09411ae9b3ab6a2f8`
  and `vc-domain-verify=www.komputy.com,4e8294d7071f277f2a3a`); restoring those three brings the old site back.
  Never touch the Resend records (`send`, `rsend`, `resend._domainkey`, `_dmarc`) or the Google verification TXT.
- **Data:** see Restore.

## Restore the database

1. **Point in time (preferred, minutes of loss at most):** Render → `komputy-db` → *Recovery* → choose a time →
   creates a new database. Point `DATABASE_URL` of every service at it (Blueprint: change `fromDatabase` name) and deploy.
2. **From the nightly dump (R2 `komputy-backups/postgres/YYYY/MM/DD/…dump`):**
   ```sh
   aws s3 cp s3://komputy-backups/<key> db.dump --endpoint-url $STORAGE_ENDPOINT_URL
   createdb restore && pg_restore --no-owner -d <restore-url> db.dump
   ```
   Restore drill (do quarterly): restore into a scratch database and compare row counts of `users`, `tenants`,
   `engine.runs`, `engine.leases`.

## Scale

| Symptom | Action |
|---|---|
| Calc jobs OOM / `peak_rss_mb` near the instance limit | `komputy-worker-calc` plan: pro (4 GB) → pro plus (8 GB) → pro max (16 GB) |
| Calc queue backs up (many tenants computing) | `numInstances` of `komputy-worker-calc` 1 → 2+ (each still runs one job) |
| Validation/imports slow | `komputy-worker-io` instances +1 or `--concurrency` 4 → 6 |
| Web p95 slow / CPU high | `komputy-web` numInstances 1 → 2+ |
| DB CPU/connections high | Postgres plan up; connections budget: `DEPLOYMENT_PLAN.md` §3 |

Change the value in `render.yaml` and push (Blueprint applies it), so the file stays the truth.

## Stuck or failed jobs

- Runs stuck in `queued`/`*_running` > 10 min are re-queued automatically by `reconcile_runs` (every 10 min); jobs of a
  dead worker (no heartbeat for 5 min) are retried by the same job.
- Inspect: Render shell on `komputy-engine-api` → `procrastinate --app=komputy.queue.app shell` → `list_jobs`,
  `retry <id>`, `cancel <id>`.
- Logs: Render → service → Logs (JSON; filter by `run_id`, `stage`, `peak_rss_mb`).

## Re-run a scheduled job

Render → `komputy-cron-*` → *Trigger run*. All jobs are idempotent.

## Rotate a secret

| Secret | How |
|---|---|
| `SERVICE_TOKEN`, `IP_HASH_SALT`, `CRON_SECRET` | Render env group → generate new value → redeploy web + engine (+ crons pick it up next run) |
| `BETTER_AUTH_SECRET` | Web → Environment → new value → deploy (signs everyone out) |
| R2 keys | Cloudflare → R2 → API tokens: create a new token for the two buckets, set `STORAGE_ACCESS_KEY` (= token id) and `STORAGE_SECRET_KEY` (= sha256 of token value) on engine API, both workers and the backup cron, deploy, then delete the old token |
| `RESEND_API_KEY` | Resend → API keys → create, set on web, deploy, delete the old key |

## Add the first admin / recover admin access

`BOOTSTRAP_ADMIN_EMAIL` (web) creates a platform admin on deploy if that email doesn't exist yet, with an unknown random
password: use **Forgot password** on the sign-in page to set one.

## Incident checklist

1. Admin → *System health* (database, Redis, engine, workers, storage, email outbox) or `GET /api/healthz`.
2. Render dashboard: which service is red? Logs + Events (deploy failed? OOM? health check failing?).
3. Recent deploy? Roll back first, investigate after.
4. Database: Render → `komputy-db` → Metrics (CPU, connections, disk).
5. R2: Cloudflare status; `/readyz` shows `storage`.
6. Write down what happened and the fix here.
