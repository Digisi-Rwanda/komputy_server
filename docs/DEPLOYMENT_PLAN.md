# Komputy production deployment plan

Deploy everything from scratch: `komputy_web` (Next.js) and `komputy_server` (Komputy engine) on Render (Frankfurt),
files on Cloudflare R2, DNS on Cloudflare, email through Resend, code and CI on GitHub.
We work in phases. Each phase has an exit gate, and the next phase starts only once that gate passes.

Status of access, checked on 2026-11-15: Render MCP, Cloudflare MCP (DNS, R2, account API tokens), Resend API key, and the GitHub token are all available.

**Progress (2026-11-15):** Phases 0-7 done and verified. Live at https://komputy.com (Render, Frankfurt):
web, engine API, io + calc workers, 3 HTTP crons, nightly backup (verified complete on production), R2 storage,
CI-gated auto-deploy. Open: Phase 4 human sign-in check, Phase 8 external uptime check, Phase 9 bank-scale load
test, Phase 10 secret rotation (GitHub token, Resend key, R2 token: all pasted in chat).

---

## 1. Target architecture

```
                     Cloudflare DNS (komputy.com)
                               │  DNS-only record → Render TLS
                               ▼
  Browser ──HTTPS──▶ komputy-web  (Render web service, Node 22, Next 16)
     │                 │  ▲    ▲ private network (no public URL)
     │                 │  │    └── engine fetches JWKS: http://komputy-web:10000/api/auth/jwks
     │                 ▼  │
     │          komputy-engine-api (Render private service, Docker, FastAPI)
     │                 │
     │     ┌───────────┼──────────────┬──────────────────┐
     │     ▼           ▼              ▼                  ▼
     │  komputy-db   komputy-kv   komputy-worker-io   komputy-worker-calc
     │  (Postgres)   (Valkey)     (Procrastinate io)  (Procrastinate calc, big RAM)
     │
     └──presigned PUT/GET──▶ Cloudflare R2  bucket komputy-prod   (+ komputy-backups)
  komputy-cron-* (Render cron, curl) ──POST /api/cron/{events,expire,daily}──▶ komputy-web
  Resend ◀── web app sends email (komputy.com is already verified)
```

**Why this fixes "the server is not on".** The previous engine ran as a single 512 MB web service, with no worker split. It would sleep, run out of memory, or get stuck on long requests. In the new setup:

- Every service is on a **paid instance type, so nothing sleeps**.
- Heavy work never runs inside a web request. It goes onto a **Postgres-backed queue**, and dedicated workers pick it up.
- **The calc worker is isolated**, so a huge bank file can't starve the API, validation, or other tenants:
  - its own large-memory instance;
  - concurrency 1;
  - a per-tenant cap of `MAX_CONCURRENT_RUNS_PER_TENANT=2`.
- Health checks and automatic restarts keep the services up.
- Interrupted jobs are not lost. They are retried with exponential back-off, and the `reconcile_runs` job runs every 10 minutes to repair runs stuck in an in-between state.
- Uploads go **straight from the browser to R2** using presigned multipart uploads, with a 5 GB maximum. Large files never pass through the API.

---

## 2. Inventory: every resource

| Resource | Type | Plan (start) | Scale-up path | Notes |
|---|---|---|---|---|
| `komputy-db` | Render Postgres 17 | **Basic-4gb**, 50 GB disk, Frankfurt | Pro tiers, read replica | PITR on; internal URL only; IP allow-list empty |
| `komputy-kv` | Render Key Value (Valkey) | Starter | Standard | Better Auth secondary storage + rate limits. `maxmemoryPolicy: noeviction` |
| `komputy-web` | Web service, Node | **Standard (1 CPU / 2 GB)** | +instances (manual or autoscale 2-4) | `npm ci && npm run build` / `npm start`; health `/api/healthz` |
| `komputy-engine-api` | Private service, Docker | **Standard** | +instances | `uvicorn … --workers 2`; health `/healthz`; pre-deploy migrations |
| `komputy-worker-io` | Background worker, Docker | **Standard** | +instances | `--queues=io --concurrency=4` |
| `komputy-worker-calc` | Background worker, Docker | **Pro (2 CPU / 4 GB)** | Pro Plus 8 GB → Pro Max 16 GB; +instances | `--queues=calc --concurrency=1`; `maxShutdownDelaySeconds: 300` |
| `komputy-cron-events` | Cron (image `curlimages/curl`) | Starter |: | `* * * * *` → `POST /api/cron/events` |
| `komputy-cron-expire` | Cron | Starter |: | `*/5 * * * *` → `/api/cron/expire` |
| `komputy-cron-daily` | Cron | Starter |: | `0 6 * * *` (08:00 Kigali) → `/api/cron/daily` |
| `komputy-cron-backup` | Cron (image `postgres:17`) | Starter |: | `0 1 * * *` `pg_dump -Fc` → R2 `komputy-backups` |
| Env group `komputy-shared` | Render env group |: |: | Shared secrets (below) |
| R2 `komputy-prod` | Bucket, EU jurisdiction |: |: | Private, CORS for the web origin, lifecycle rule |
| R2 `komputy-backups` | Bucket |: |: | 30-day lifecycle expiry |
| CF API token `komputy-engine-r2` | Account token |: |: | Object read and write on these 2 buckets only → S3 keys |
| DNS `komputy.com`, `www` | Cloudflare |: |: | Vercel → Render (see Phase 6) |

**Estimated monthly cost (USD).** Render prices change, so the exact figure is confirmed as each resource is created.

| | Lean start | **Recommended** | Bank-scale |
|---|---|---|---|
| Web | Standard 25 | Standard 25 | 2× Standard 50 |
| Engine API | Starter 7 | Standard 25 | 2× Standard 50 |
| Worker io | Starter 7 | Standard 25 | Standard 25 |
| Worker calc | Standard 25 | **Pro 85** | Pro Plus 175 |
| Postgres | Basic-1gb ~19 + disk | **Basic-4gb ~75 + disk** | Pro-8gb ~185 + disk |
| Key Value | Starter 10 | Starter 10 | Standard 32 |
| Crons (4) | ~2 | ~2 | ~2 |
| R2 | ~0 (10 GB free) | ~0-5 | ~15 |
| **Total** | **≈ $95** | **≈ $250** | **≈ $535** |

> This corrects the "$40-70" I gave earlier. That figure did not include a large calc worker and database, and those are exactly what large bank files need.
> We start on **Recommended**. We move to Bank-scale only when the Phase 9 measurements show it is needed. Every resize is a one-click change with no downtime.

---

## 3. Configuration: every variable

The secrets listed below are generated during deployment with `openssl rand`, and they are never printed or committed.

**Env group `komputy-shared`** (attached to web, engine API and both workers):

| Var | Value |
|---|---|
| `SERVICE_TOKEN` / `ENGINE_SERVICE_TOKEN` | Same random 48-character value in both. The engine reads the first name and the web app reads the second. |
| `IP_HASH_SALT` | Random, 32 characters |

**`komputy-web`**

| Var | Value |
|---|---|
| `DATABASE_URL` | `fromDatabase komputy-db connectionString` (internal) |
| `REDIS_URL` | `fromService komputy-kv connectionString` |
| `BETTER_AUTH_SECRET` | Random, 64 characters |
| `BETTER_AUTH_URL` | `https://komputy.com` (the final domain; see decision D1) |
| `ENGINE_URL` | `http://komputy-engine-api:10000`, the private-network hostname |
| `CRON_SECRET` | Random, 48 characters |
| `RESEND_API_KEY` | From Resend. **Rotate the key that was pasted in chat.** |
| `EMAIL_FROM` | `Komputy <no-reply@komputy.com>` |
| `TOTP_ENCRYPTION_KEY` | Fernet key (`python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"`) |
| `DB_POOL_MAX` | `10` |
| `NODE_ENV` / `NODE_VERSION` | `production` / `22` |
| `NEXT_TELEMETRY_DISABLED` | `1` |

**`komputy-engine-api` and both workers**

| Var | Value |
|---|---|
| `ENVIRONMENT` / `LOG_FORMAT` / `LOG_LEVEL` | `production` / `json` / `INFO` |
| `DATABASE_URL` | `fromDatabase komputy-db connectionString` |
| `DATABASE_POOL_SIZE` / `DATABASE_MAX_OVERFLOW` | API: `5` / `5` per uvicorn worker. Workers: `3` / `2`. |
| `WEB_JWKS_URL` | `http://komputy-web:10000/api/auth/jwks` (private network) |
| `WEB_JWT_ISSUER` / `WEB_JWT_AUDIENCE` | `komputy-web` / `komputy-engine` (the defaults, must match the web app) |
| `STORAGE_ENDPOINT_URL` | `https://<account_id>.eu.r2.cloudflarestorage.com` |
| `STORAGE_PUBLIC_ENDPOINT_URL` | Same as above. The browser uses this host for presigned URLs. |
| `STORAGE_ACCESS_KEY` / `STORAGE_SECRET_KEY` | Derived from the scoped Cloudflare token: the key ID is the token id, and the secret is the sha256 of the token value |
| `STORAGE_REGION` / `STORAGE_BUCKET_NAME` | `auto` / `komputy-prod` |
| `MAX_CONCURRENT_RUNS_PER_TENANT` | `2` |
| `SENTRY_DSN` | Optional (decision D3) |
| `WEB_DB_ROLE` | Only if we use separate database roles (decision D2) |

**Connection budget.** Basic-4gb allows about 200 connections, and this setup needs about 76:
- web: 10 per instance, so 20 with 2 instances;
- engine API: 2 uvicorn workers × 10 = 20;
- worker-io: about 4 + 5;
- worker-calc: about 1 + 5;
- crons: 2;
- SSE `LISTEN`: 1 per web instance.

That leaves room for 3-4× scale-out before we would need PgBouncer.

---

## 4. Code changes required before deploying (Phase 0)

These are real gaps, found by reading the code. Every one of them gets committed and pushed.

| # | Repo | Change | Why |
|---|---|---|---|
| C1 | web | **Baseline database schema for the web tables.** Dump the 58 `public` tables from dev with `pg_dump --schema-only` and keep only the tables in `schema.ts`. Save the result as a `drizzle/0000_baseline.sql` migration. Add `drizzle-kit migrate` (`db:migrate`) and run it as the web service's **pre-deploy** step. | These tables were created by the **previous engine's Alembic** migrations, which we are no longer using. Without this step the new production database would have no `users` or `tenants` tables, and both apps would crash. From here on, the web app owns its own schema. |
| C2 | web | Add a `/api/healthz` route (fast, checks the database with `select 1`). Fix `health.ts` so it calls engine `/readyz` and reads the field names the new engine actually returns, instead of the old `celery_worker` / `/health`. | Render health checks, and a correct admin status page |
| C3 | web | `next.config.ts`: `serverActions.bodySizeLimit: "25mb"`, `poweredByHeader: false`, security headers (HSTS, `X-Content-Type-Options`, `Referrer-Policy`, `frame-ancestors 'none'`) | Lease imports upload files of up to 20 MB through a server action, but the default limit is 1 MB |
| C4 | web | Default `EMAIL_FROM` → `Komputy <no-reply@komputy.com>`; `start` script respects `$PORT` (`next start -p ${PORT:-3000}`) | The sending domain is verified in Resend; Render injects `PORT` |
| C5 | web | First-admin bootstrap: make `scripts/seed.mts` (or a new `scripts/bootstrap-admin.mts`) idempotent and driven by environment variables, and run it once through a one-off Render job | A fresh production database has no users |
| C6 | server | `scripts/predeploy.sh`: `alembic upgrade head && procrastinate --app=komputy.queue.app schema --apply` **only if** `procrastinate_jobs` does not exist yet; on later deploys run procrastinate's own migrations | `schema --apply` is not idempotent |
| C7 | server | `Dockerfile`: run the CMD through `exec` so SIGTERM reaches uvicorn and procrastinate for graceful shutdown; set the uvicorn worker count from `WEB_CONCURRENCY`; pin `uv` | Clean deploys with no killed jobs |
| C8 | server | Make sure workers handle SIGTERM by finishing the current job, or releasing it so it is retried. Add a test that a calc job interrupted halfway is safe to re-run. | Deploys and restarts happen while a bank calculation is running |
| C9 | server | **Memory guard for very large workbooks.** Parse ingest with `python-calamine`/`polars` and stream it in chunks instead of loading the whole sheet (this is the `shortcut:` already noted). Record peak RSS per job in the logs. | One 500k-row file must not crash the calc worker |
| C10 | both | `render.yaml` Blueprint (web repo or a separate `infra` folder) describing every resource in section 2 | Infrastructure as code: reproducible and reviewable |
| C11 | both | GitHub Actions: **server** runs `ruff` + `pytest` (Postgres 17 + MinIO service containers) → Docker build; **web** runs `npm run check` | Never deploy red code |
| C12 | server | README/Makefile: production runbook pointer | Handover |

---

## 5. Decisions (my defaults; tell me if you want any changed)

| # | Decision | Default |
|---|---|---|
| D1 | **Domain.** `komputy.com` and `www` currently point to **Vercel** (CNAME `…vercel-dns-017.com` plus `_vercel` TXT records). | Serve the Next app (it has its own marketing pages) on **`komputy.com`**, with `www` redirecting to the apex. Remove the Vercel records. **If the Vercel site has to stay**, use `app.komputy.com` instead and leave the apex alone. |
| D2 | One database role or two | **One role** (Render's default). Row-level security uses `FORCE`, so it still applies to the owner. Two roles are an option later if Render's user can `CREATE ROLE`. |
| D3 | Error tracking | Sentry free tier, only if you provide a DSN. Otherwise Render logs and alerts. |
| D4 | Staging environment | **Not at first** (it roughly doubles the cost). Use Render PR previews later if needed. |
| D5 | Cloudflare proxy (orange cloud) | **DNS-only** at first, so Render can issue certificates without trouble and SSE streams are never cut at Cloudflare's 100 s limit. Phase 10 can turn on the proxy for WAF and DDoS protection, with SSL **Full (strict)**. |
| D6 | R2 jurisdiction | **EU** (closest to Frankfurt and the bank's data residency) |

---

## 6. Phases, each ending in an exit gate

### Phase 0: Prepare the code (C1-C12)
1. Make changes C1-C9. Run both test suites locally and the 3 live e2e scripts against a **fresh empty database**, so the C1 baseline is proven to work.
2. Commit each change individually and push to `main` on both repos.
3. Write the `render.yaml` and the GitHub Actions workflows (C10, C11), then push them.

**Gate:** CI is green on both repos. An empty Postgres gets migrated (web baseline + engine Alembic + procrastinate), seeded, and passes the e2e suites.

### Phase 1: Storage (Cloudflare R2)
1. Create the buckets `komputy-prod` and `komputy-backups` (EU jurisdiction).
2. Set CORS on `komputy-prod`:
   - origins: `https://komputy.com`, `https://www.komputy.com`, and the `onrender.com` URL for the smoke test;
   - methods: `PUT`, `GET`, `HEAD`;
   - headers: `*`;
   - expose `ETag`;
   - max age 3600.
3. Lifecycle rules:
   - `komputy-prod`: abort incomplete multipart uploads after 7 days;
   - `komputy-backups`: delete after 30 days.
4. Create the account API token `komputy-engine-r2`, limited to those 2 buckets with Bucket Item Read and Write, then derive the S3 keys.
5. Smoke test: put, get and delete an object, and complete a presigned multipart upload.

**Gate:** a presigned multipart upload works from a browser origin.

### Phase 2: Data layer
1. Create `komputy-db` (Postgres 17, Basic-4gb, Frankfurt, IP allow-list empty) and `komputy-kv` (Starter, `noeviction`).
2. Point-in-time recovery is on (included on paid plans). Note the retention window.
3. Run the migrations through the services' pre-deploy steps in Phase 3/4. There is no manual SQL apart from the D2 role grant, if we choose two roles.

**Gate:** the database is reachable over the internal URL, and Key Value answers `PING`.

### Phase 3: Engine (API and workers)
1. Create the env group, then `komputy-engine-api` (private service) with pre-deploy `scripts/predeploy.sh` and health check `/healthz`.
2. Create `komputy-worker-io` and `komputy-worker-calc`, with `maxShutdownDelaySeconds: 300`.
3. Check:
   - `/readyz` reports the database and storage as OK;
   - the logs show both workers polling their queues;
   - the periodic jobs (`reconcile_runs`, `outbox_retention`, lease alerts) are registered.

**Gate:** the engine is healthy and the workers are idle and waiting. *(The JWKS check completes in Phase 4.)*

### Phase 4: Web
1. Create `komputy-web`:
   - Node 22; build `npm ci && npm run build`;
   - pre-deploy `npm run db:migrate` (C1);
   - start `npm start`; health check `/api/healthz`.
2. Run the one-off bootstrap job (C5) to create the first admin and workspace.
3. Smoke test on `https://komputy-web.onrender.com`:
   - sign up and sign in, MFA, invite;
   - the engine call goes web → engine (private) → engine fetches the web JWKS (private) → 200.

**Gate:** all three e2e suites (`e2e-engine`, `e2e-leases`, `e2e-platform`) pass against the `onrender.com` URL.

### Phase 5: Scheduled jobs and backups
1. Create the 3 HTTP crons (curl with `Authorization: Bearer $CRON_SECRET`) and the backup cron (`pg_dump -Fc | aws s3 cp` to R2).
2. Run each one by hand once. Check the email outbox drains: a real email arrives through Resend.
3. **Restore drill:** restore last night's dump into a throwaway database and count the rows.

**Gate:** notices arrive by email, and a backup restores cleanly.

### Phase 6: Domain cutover (Cloudflare DNS)
1. Add `komputy.com` and `www.komputy.com` as custom domains on `komputy-web` in Render.
2. In Cloudflare:
   - delete the Vercel CNAMEs and the `_vercel` TXT records;
   - add `CNAME komputy.com → komputy-web.onrender.com` (flattened at the apex) and `CNAME www → komputy-web.onrender.com`, both DNS-only.
   - Keep the Resend records (`send`, `rsend`, `resend._domainkey`, `_dmarc`) and the Google verification record **untouched**.
3. Wait for Render to verify the domain and issue certificates.
4. Switch `BETTER_AUTH_URL`, the R2 CORS origins and the trusted origins to the final domain, then redeploy the web service.
5. Turn on Render's "redirect www → apex".

**Gate:** `https://komputy.com` is served by Render with a valid certificate, and the e2e suites pass on the real domain.
**Rollback:** put the old Vercel CNAME back (TTL 300 is set before the switch).

### Phase 7: CI/CD
1. Render auto-deploy is set to **"after CI checks pass"** on `main` (`autoDeployTrigger: checksPass`).
   - Both repos live in the **Digisi-Rwanda** GitHub org (moved there 2026-11-15), where Render's GitHub app is installed.
   - If it isn't connected, I use the fallback: GitHub Actions calls **Render deploy hooks**, stored as repo secrets, after CI passes. That needs nothing from you.
2. Protect the `main` branch: CI must pass and force-pushes are blocked.
3. The order on each deploy is enforced by pre-deploy: **migrations run before new code takes traffic**. Migrations must stay backward compatible (expand, then contract).

**Gate:** a trivial commit flows to production on its own, and a red commit does not.

### Phase 8: Observability and alerts
1. Render: email alerts on deploy failure and service crash for every service. Health-check restarts are on.
2. Logs are JSON on both sides (structlog / Next). Optional log stream (Better Stack free tier) if you want search.
3. Sentry (D3) on web and engine, if a DSN is provided.
4. External uptime check on `https://komputy.com/api/healthz` (Cloudflare health check or a free uptime monitor), alerting by email.
5. Admin "System health" page (C2) shows the database, Redis, engine, workers, storage and the email outbox.

**Gate:** killing a worker on purpose produces an alert, and it recovers by itself.

### Phase 9: Load and big-file testing, then tuning
1. Generate synthetic bank-scale portfolios: 50k, 200k and 500k loans, plus a 10k-row lease import.
2. Measure upload time, validation time, compute time and peak memory on the calc worker, using the logged peak RSS (C9).
3. Run concurrent use: about 50 browser sessions plus 3 tenants computing at once. Watch the database connections and CPU.
4. Tune:
   - calc instance size;
   - io concurrency;
   - database plan;
   - web instance count;
   - turn on autoscaling (2-4) if the workspace plan allows it.
5. Write the measured limits into the runbook: for example, "500k loans needs Pro Plus and takes N minutes".

**Gate:** the biggest realistic file finishes with no OOM and no time-outs, and the API p95 stays under 500 ms while compute runs.

### Phase 10: Hardening and handover
1. Rotate every secret that was ever pasted in chat (Resend, GitHub), plus new random values.
2. Security sweep:
   - headers present;
   - engine has no public URL;
   - database has no external access;
   - R2 buckets private;
   - scoped tokens only;
   - rate limits work through Key Value.
3. Optional: Cloudflare proxy (D5) with SSL Full (strict), WAF managed rules, and bot fight mode. Then re-run the e2e suites; SSE must still stream.
4. Write `plans/RUNBOOK.md` covering:
   - deploy and rollback (Render "rollback to previous deploy");
   - restoring the database (PITR or dump);
   - rotating secrets;
   - scaling;
   - stuck jobs (`procrastinate` shell, retry);
   - re-running a cron;
   - adding a tenant;
   - incident checklist.
5. Final run of all e2e suites on production. Remove the e2e test tenants afterwards.

**Gate:** you sign in on `https://komputy.com`, upload a real-size file, and get results.

---

## 7. Best-practice checklist (verified during the phases)

- [ ] Infrastructure as code (`render.yaml`); no hand-made drift
- [ ] All services in **one region** (Frankfurt); everything uses internal URLs and the private network
- [ ] No secrets in git, logs or chat; env groups for shared secrets; scoped tokens; rotation done
- [ ] Migrations run in pre-deploy, are backward compatible, and are tested on an empty database in CI
- [ ] Health checks on every web/private service; graceful shutdown; job retries and reconciliation
- [ ] Heavy work only on queues; per-tenant run cap; calc isolated on its own instance
- [ ] Direct-to-R2 uploads; presigned URL expiry 15 minutes; CORS limited to our origin
- [ ] Backups: PITR plus nightly off-site dumps to R2; **restore tested**
- [ ] Alerts for deploy failure, crash, health and uptime; JSON logs
- [ ] Load-tested with bank-sized files; limits documented
- [ ] Rollback paths: Render previous deploy, DNS revert, database PITR
- [ ] Row-level security forced on tenant tables; engine private; database not public

## 8. Things only you can do (everything else is automated)

1. **Approve D1-D6**, or just say "defaults".
2. **Rotate the leaked secrets** at the end (Phase 10): the Resend key and the GitHub token. Put the new values in `~/.komputy-secrets`.
3. *Optional:* a Sentry DSN.
4. Final sign-off at the Phase 10 gate.
