#!/bin/sh
# Render pre-deploy for the engine API: runs before new code takes traffic. Idempotent.
set -eu
alembic upgrade head
# `procrastinate schema --apply` is not idempotent: install the queue schema only on a fresh database.
# shortcut: procrastinate upgrades (uv.lock pins it) need their SQL migrations applied by hand, revisit when bumping procrastinate.
if python -c "import sys,psycopg;from ledgerline.db import sync_dsn;sys.exit(0 if psycopg.connect(sync_dsn()).execute(\"select to_regclass('public.procrastinate_jobs')\").fetchone()[0] else 1)"; then
  echo "procrastinate schema present"
else
  procrastinate --app=ledgerline.queue.app schema --apply
fi
