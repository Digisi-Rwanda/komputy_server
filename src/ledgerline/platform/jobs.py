"""Platform housekeeping jobs: tenant purge, stuck-run reconcile, outbox retention."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, text, update

from ledgerline.db import AsyncSessionLocal, Base
from ledgerline.log import get_logger
from ledgerline.queue import RETRY
from ledgerline.queue import app as queue_app

log = get_logger("platform.jobs")


@queue_app.task(name="platform.purge_tenant", queue="io", retry=RETRY)
async def purge_tenant(tenant_id: str) -> dict[str, int]:
    """Delete every engine row and every stored object of a closed workspace. Idempotent."""
    from ledgerline.storage import delete_prefix

    objects = await delete_prefix(f"{tenant_id}/")
    rows = 0
    async with AsyncSessionLocal() as db:
        # Children before parents: reverse dependency order of the engine schema.
        for table in reversed(Base.metadata.sorted_tables):
            if "tenant_id" in table.c:
                rows += (await db.execute(delete(table).where(table.c.tenant_id == tenant_id))).rowcount or 0
        await db.commit()
    log.info("tenant_purged", tenant_id=tenant_id, objects=objects, rows=rows)
    return {"objects": objects, "rows": rows}


@queue_app.periodic(cron="*/10 * * * *", periodic_id="reconcile_runs")
@queue_app.task(name="platform.reconcile_runs", queue="io", queueing_lock="platform.reconcile_runs")
async def reconcile_runs(timestamp: int) -> int:
    """Re-queue runs left `queued` with no live compute job (enqueue lost between commit and defer, worker crash)."""
    from ledgerline.apps.runs.jobs import compute_run
    from ledgerline.apps.runs.models import Run

    # A worker killed mid-job (deploy past the shutdown delay, OOM) leaves its job `doing` forever: give it back to the
    # queue once its worker has sent no heartbeat for 5 min (heartbeats run every 10 s, also during threaded compute).
    for job in await queue_app.job_manager.get_stalled_jobs(seconds_since_heartbeat=300):
        await queue_app.job_manager.retry_job(job)
        log.warning("stalled_job_retried", job_id=job.id, task=job.task_name)
    cutoff = datetime.now(UTC) - timedelta(minutes=10)
    requeued = 0
    async with AsyncSessionLocal() as db:
        stuck = (
            (
                await db.execute(
                    select(Run.id).where(
                        Run.status.in_(("queued", "pd_running", "lgd_running", "ead_running")), Run.updated_at < cutoff
                    )
                )
            )
            .scalars()
            .all()
        )
        for run_id in stuck:
            live = await db.scalar(
                text(
                    "SELECT count(*) FROM procrastinate_jobs WHERE task_name = 'ecl.compute' AND status IN ('todo','doing') AND args->>'run_id' = :r"
                ),
                {"r": run_id},
            )
            if not live:
                await db.execute(
                    update(Run).where(Run.id == run_id).values(status="queued", updated_at=datetime.now(UTC))
                )
                await db.commit()
                await compute_run.configure(queueing_lock=run_id).defer_async(run_id=run_id)
                requeued += 1
    if requeued:
        log.warning("runs_requeued", count=requeued)
    return requeued


@queue_app.periodic(cron="15 3 * * *", periodic_id="outbox_retention")
@queue_app.task(name="platform.outbox_retention", queue="io", queueing_lock="platform.outbox_retention")
async def outbox_retention(timestamp: int) -> int:
    from ledgerline.events.notify import OutboxEvent

    async with AsyncSessionLocal() as db:
        n = (
            await db.execute(
                delete(OutboxEvent).where(OutboxEvent.consumed_at < datetime.now(UTC) - timedelta(days=30))
            )
        ).rowcount or 0
        await db.execute(
            text(
                "DELETE FROM procrastinate_jobs WHERE status IN ('succeeded','failed','cancelled','aborted') AND id IN (SELECT job_id FROM procrastinate_events WHERE type IN ('succeeded','failed','cancelled','aborted') AND at < now() - interval '14 days')"
            )
        )
        await db.commit()
    return n
