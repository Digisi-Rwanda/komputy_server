"""Scheduled jobs for the leases module (Procrastinate periodic).

- lease_daily_alerts: notice deadlines within 90 days, leases ending within 60 days, and periods whose
  month has ended without a close: one notification per (lease, alert) thanks to lease_alerts_sent.
- lease_verify_books: recompute every approved chain and flag leases whose stored books differ
  (engine change or data tampering), notifying administrators.
Both coroutines are importable for tests; the queue tasks are thin wrappers.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from komputy.db import AsyncSessionLocal
from komputy.queue import app as queue_app


async def daily_alerts(db: AsyncSession, today: date | None = None) -> int:
    from komputy.apps.leases.common import notify
    from komputy.apps.leases.models import Lease, LeaseAlertSent, LeasePeriod, LeaseVersion
    from komputy.leases.domain.dates import month_end

    d = today or date.today()
    sent = 0
    seen = {(a.lease_id, a.key) for a in (await db.execute(select(LeaseAlertSent))).scalars()}

    async def once(tenant_id: str, lease_id: str, key: str, to: str, title: str, body: str, href: str) -> None:
        nonlocal sent
        if (lease_id, key) in seen:
            return
        seen.add((lease_id, key))
        db.add(LeaseAlertSent(tenant_id=tenant_id, lease_id=lease_id, key=key))
        await notify(db, tenant_id, to, title, body, href)
        sent += 1

    leases = {lz.id: lz for lz in (await db.execute(select(Lease))).scalars()}
    latest: dict[str, LeaseVersion] = {}
    for v in (
        await db.execute(select(LeaseVersion).where(LeaseVersion.status == "approved").order_by(LeaseVersion.n))
    ).scalars():
        latest[v.lease_id] = v
    for lid, v in latest.items():
        lz = leases.get(lid)
        if lz is None or v.event == "termination":
            continue
        end = v.terms.get("endDate") or ""
        for o in (v.assessment or {}).get("options") or []:
            nd = o.get("noticeDate")
            if nd and d.isoformat() <= nd <= (d + timedelta(days=90)).isoformat():
                await once(
                    lz.tenant_id,
                    lid,
                    f"notice:{o.get('id')}:{nd}",
                    lz.owner_id,
                    f"Notice deadline {nd}",
                    f"{lz.code} · {lz.name}: decide on the {o.get('months')}-month {o.get('type')} option",
                    f"/leases/{lid}",
                )
        if end and d.isoformat() <= end <= (d + timedelta(days=60)).isoformat():
            await once(
                lz.tenant_id,
                lid,
                f"expiry:{end}",
                lz.owner_id,
                f"Lease ends {end}",
                f"{lz.code} · {lz.name}: renew, vacate or record a holdover",
                f"/leases/{lid}",
            )
    for p in (await db.execute(select(LeasePeriod).where(LeasePeriod.status == "open"))).scalars():
        if month_end(p.key) < d.isoformat():
            await once(
                p.tenant_id,
                f"period:{p.tenant_id}"[:26],
                f"close:{p.key}",
                "*",
                f"Start the {p.key} close",
                "The month has ended; lease journals are waiting to be generated.",
                f"/leases/close/{p.key}",
            )
    await db.commit()
    return sent


async def verify_books(db: AsyncSession) -> int:
    from komputy.apps.leases.common import checksum, day_count, notify, segment_inputs
    from komputy.apps.leases.models import Lease, LeaseBooks, LeaseVersion
    from komputy.leases.domain.engine import ENGINE_VERSION

    flagged = 0
    for books in (await db.execute(select(LeaseBooks))).scalars():
        chain = list(
            (
                await db.execute(
                    select(LeaseVersion)
                    .where(LeaseVersion.lease_id == books.lease_id, LeaseVersion.status == "approved")
                    .order_by(LeaseVersion.n)
                )
            ).scalars()
        )
        dc = await day_count(db, books.tenant_id)
        expected = checksum({"inputs": segment_inputs(chain), "dayCount": dc, "engine": ENGINE_VERSION})
        lz = await db.get(Lease, books.lease_id)
        bad = expected != books.chain_checksum
        if lz and lz.verification_failed != bad:
            lz.verification_failed = bad
            if bad:
                flagged += 1
                await notify(
                    db,
                    books.tenant_id,
                    "*",
                    f"{lz.code}: stored schedule no longer matches its inputs",
                    "Recalculate and reapprove, or contact support if this is unexpected.",
                    f"/leases/{lz.id}",
                )
    await db.commit()
    return flagged


# 06:00 UTC = 08:00 Kigali. Periodic jobs are de-duplicated by Procrastinate (one per tick across workers).
@queue_app.periodic(cron="0 6 * * *", periodic_id="lease_daily_alerts")
@queue_app.task(name="leases.daily_alerts", queue="io", queueing_lock="leases.daily_alerts")
async def lease_daily_alerts(timestamp: int) -> int:
    async with AsyncSessionLocal() as db:
        return await daily_alerts(db)


@queue_app.periodic(cron="30 2 * * *", periodic_id="lease_verify_books")
@queue_app.task(name="leases.verify_books", queue="calc", queueing_lock="leases.verify_books")
async def lease_verify_books(timestamp: int) -> int:
    async with AsyncSessionLocal() as db:
        return await verify_books(db)


@queue_app.task(name="leases.import", queue="io", lock="{batch_id}", retry=False)
async def import_job(batch_id: str) -> None:
    """Validate or commit a lease import batch in chunks (progress is committed after each chunk)."""
    from komputy.apps.leases.imports import run_job

    await run_job(batch_id)
