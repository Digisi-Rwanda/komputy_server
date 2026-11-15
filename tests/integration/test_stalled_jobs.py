"""A job whose worker died mid-run (deploy SIGKILL, OOM) goes back to the queue on the next reconcile."""

from __future__ import annotations

import psycopg
import pytest

from komputy.db import sync_dsn
from komputy.platform.jobs import reconcile_runs
from komputy.queue import app as queue_app
from tests.integration.conftest import _prepare_db


@pytest.mark.asyncio
async def test_job_of_dead_worker_is_retried() -> None:
    _prepare_db()
    with psycopg.connect(sync_dsn(), autocommit=True) as c:
        dead = c.execute(
            "INSERT INTO procrastinate_workers (last_heartbeat) VALUES (now() - interval '10 minutes') RETURNING id"
        ).fetchone()[0]
        alive = c.execute("INSERT INTO procrastinate_workers (last_heartbeat) VALUES (now()) RETURNING id").fetchone()[
            0
        ]
        stuck = c.execute(
            "INSERT INTO procrastinate_jobs (queue_name, task_name, status, worker_id) VALUES ('calc', 'test.stuck', 'doing', %s) RETURNING id",
            (dead,),
        ).fetchone()[0]
        busy = c.execute(
            "INSERT INTO procrastinate_jobs (queue_name, task_name, status, worker_id) VALUES ('calc', 'test.busy', 'doing', %s) RETURNING id",
            (alive,),
        ).fetchone()[0]
    try:
        async with queue_app.open_async():
            await reconcile_runs(timestamp=0)
        with psycopg.connect(sync_dsn()) as c:
            status = dict(
                c.execute("SELECT id, status FROM procrastinate_jobs WHERE id IN (%s, %s)", (stuck, busy)).fetchall()
            )
        assert status[stuck] == "todo"  # dead worker → retried
        assert status[busy] == "doing"  # live worker (fresh heartbeat) → left alone
    finally:
        with psycopg.connect(sync_dsn(), autocommit=True) as c:
            c.execute("DELETE FROM procrastinate_jobs WHERE id IN (%s, %s)", (stuck, busy))
            c.execute("DELETE FROM procrastinate_workers WHERE id IN (%s, %s)", (dead, alive))
