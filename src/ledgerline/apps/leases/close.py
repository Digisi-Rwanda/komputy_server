"""Period close: steps, journal batch (normal lines + catch-ups), payment matching, reconciliation,
segregated approval, idempotent posting, lock, and reopen with a linked reversal batch.

Rule that makes back-dated changes safe: batch(P) = books through P − posted through P−1. Anything
beyond P's own month is a catch-up line, so locked months are never rewritten.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ledgerline.apps.leases.common import (
    audit,
    checksum,
    conflict,
    forbidden,
    in_force,
    invalid,
    iso,
    money,
    not_found,
    notify,
    now,
    require,
    setup_versions,
)
from ledgerline.apps.leases.models import (
    Lease,
    LeaseActualPayment,
    LeaseBooks,
    LeaseCloseRun,
    LeaseCounterparty,
    LeaseEntity,
    LeaseJournalBatch,
    LeaseJournalLine,
    LeasePeriod,
    LeasePostedBalance,
    LeaseVersion,
)
from ledgerline.auth.deps import Membership as TenantMembership
from ledgerline.kernel.ids import new_ulid
from ledgerline.leases.domain.dates import month_end, next_month_key

STEPS = ["cutoff", "rates", "events", "generate", "matching", "reconcile", "approve", "post"]
ZERO = Decimal(0)
TOL = Decimal("0.005")


async def _period(db: AsyncSession, tenant_id: str, key: str, lock: bool = False) -> LeasePeriod:
    q = select(LeasePeriod).where(LeasePeriod.tenant_id == tenant_id, LeasePeriod.key == key)
    p = (await db.execute(q.with_for_update() if lock else q)).scalar_one_or_none()
    if p is None:
        raise not_found("Period not open.")
    return p


async def _close(db: AsyncSession, tenant_id: str, key: str) -> LeaseCloseRun | None:
    return (
        await db.execute(select(LeaseCloseRun).where(LeaseCloseRun.tenant_id == tenant_id, LeaseCloseRun.period == key))
    ).scalar_one_or_none()


def _step(c: LeaseCloseRun, sid: str) -> dict[str, Any]:
    return next(s for s in c.steps if s["id"] == sid)


def _set_steps(c: LeaseCloseRun, fn) -> None:
    steps = [dict(s) for s in c.steps]
    fn(steps)
    c.steps = steps  # reassign: JSON columns don't track in-place mutation


async def _batch(db: AsyncSession, tenant_id: str, batch_id: str | None) -> LeaseJournalBatch | None:
    if not batch_id:
        return None
    return (
        await db.execute(
            select(LeaseJournalBatch).where(LeaseJournalBatch.tenant_id == tenant_id, LeaseJournalBatch.id == batch_id)
        )
    ).scalar_one_or_none()


async def _books(db: AsyncSession, tenant_id: str) -> list[LeaseBooks]:
    return list((await db.execute(select(LeaseBooks).where(LeaseBooks.tenant_id == tenant_id))).scalars())


async def source_checksum(db: AsyncSession, tenant_id: str) -> str:
    rows = (
        await db.execute(
            select(LeaseBooks.lease_id, LeaseBooks.chain_checksum)
            .where(LeaseBooks.tenant_id == tenant_id)
            .order_by(LeaseBooks.lease_id)
        )
    ).all()
    return checksum([list(r) for r in rows])


async def _posted(db: AsyncSession, tenant_id: str) -> dict[str, dict[str, Decimal]]:
    out: dict[str, dict[str, Decimal]] = defaultdict(dict)
    for r in (await db.execute(select(LeasePostedBalance).where(LeasePostedBalance.tenant_id == tenant_id))).scalars():
        out[r.lease_id][r.account] = r.amount
    return out


# ─── Start / issues / complete ─────────────────────────────────────────────


async def start(db: AsyncSession, m: TenantMembership, key: str) -> LeaseCloseRun:
    require(m, "administrator", "analyst")
    p = await _period(db, m.tenant_id, key, lock=True)
    if p.status == "locked":
        raise invalid("This period is locked.")
    existing = await _close(db, m.tenant_id, key)
    if existing:
        return existing
    prev_open = (
        await db.execute(
            select(LeasePeriod).where(
                LeasePeriod.tenant_id == m.tenant_id,
                LeasePeriod.key < key,
                LeasePeriod.status != "locked",
            )
        )
    ).first()
    if prev_open:
        raise invalid("Close the previous period first.")
    p.status = "closing"
    c = LeaseCloseRun(
        id=new_ulid(),
        tenant_id=m.tenant_id,
        period=key,
        started_by=m.user_id,
        steps=[{"id": s, "status": "todo" if i == 0 else "locked"} for i, s in enumerate(STEPS)],
    )
    db.add(c)
    await db.flush()
    await audit(db, m.tenant_id, m.user_id, "close.start", key)
    return c


async def step_issues(db: AsyncSession, tenant_id: str, c: LeaseCloseRun, sid: str) -> dict[str, list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    end = month_end(c.period)
    if sid == "cutoff":
        rows = (
            await db.execute(
                select(LeaseVersion).where(
                    LeaseVersion.tenant_id == tenant_id,
                    LeaseVersion.event == "initial",
                    LeaseVersion.status.in_(("draft", "returned", "in_review")),
                )
            )
        ).scalars()
        n = sum(1 for v in rows if (v.terms.get("commencementDate") or "9999") <= end)
        if n:
            warnings.append(
                f"{n} lease{'s' if n > 1 else ''} commencing by {end} {'are' if n > 1 else 'is'} not approved and won't be included."
            )
    elif sid == "rates":
        if not in_force(await setup_versions(db, tenant_id, "policies"), end):
            errors.append("No approved accounting policy is in force.")
        if not in_force(await setup_versions(db, tenant_id, "curves"), end):
            errors.append("No approved discount-rate curve is in force.")
    elif sid == "events":
        rows = (
            await db.execute(
                select(LeaseVersion).where(
                    LeaseVersion.tenant_id == tenant_id,
                    LeaseVersion.event != "initial",
                    LeaseVersion.status.in_(("draft", "returned", "in_review")),
                )
            )
        ).scalars()
        n = sum(1 for v in rows if v.effective_date and v.effective_date.isoformat() <= end)
        if n:
            warnings.append(
                f"{n} change{'s' if n > 1 else ''} effective this period {'are' if n > 1 else 'is'} still pending and will post as catch-ups next period."
            )
    elif sid == "generate":
        b = await _batch(db, tenant_id, c.batch_id)
        if b is None:
            errors.append("Generate the journal batch.")
        elif b.status == "draft" and b.source_checksum != await source_checksum(db, tenant_id):
            errors.append("Leases changed after the batch was generated. Regenerate it.")
    elif sid == "matching":
        n = (
            await db.execute(
                select(LeaseActualPayment.id).where(
                    LeaseActualPayment.close_id == c.id,
                    LeaseActualPayment.match != "matched",
                    LeaseActualPayment.resolution.is_(None),
                )
            )
        ).all()
        if n:
            errors.append(
                f"{len(n)} payment{'s' if len(n) > 1 else ''} still need{'' if len(n) > 1 else 's'} a resolution."
            )
    elif sid == "reconcile":
        for r in await reconciliations(db, tenant_id, c):
            if not r["ok"]:
                errors.append(f"{r['label']} doesn't reconcile (difference {r['diff']}).")
        gl = in_force(await setup_versions(db, tenant_id, "gl"), end)
        unmapped = [k for k, v in ((gl.data.get("accounts") if gl else {}) or {}).items() if not v]
        if not gl:
            errors.append("No approved GL mapping is in force.")
        elif unmapped:
            errors.append(f"{len(unmapped)} GL account{'s are' if len(unmapped) > 1 else ' is'} not mapped.")
    elif sid == "post":
        b = await _batch(db, tenant_id, c.batch_id)
        if b is None or b.status != "posted":
            errors.append("Post the batch and record the ERP reference.")
    return {"errors": errors, "warnings": warnings}


async def complete_step(
    db: AsyncSession,
    m: TenantMembership,
    key: str,
    sid: str,
    note: str = "",
    accept_warning: bool = False,
) -> None:
    if sid not in STEPS or sid in ("generate", "post"):
        raise invalid("Use the dedicated action for this step.")
    await _period(db, m.tenant_id, key, lock=True)
    c = await _close(db, m.tenant_id, key)
    if c is None:
        raise invalid("Start the close first.")
    if _step(c, sid)["status"] == "locked":
        raise invalid("Finish the previous step first.")
    issues = await step_issues(db, m.tenant_id, c, sid)
    if issues["errors"]:
        raise invalid(issues["errors"][0])
    if issues["warnings"] and not accept_warning:
        raise invalid(issues["warnings"][0])
    if issues["warnings"] and not (note or "").strip():
        raise invalid("Give a reason for accepting the warning.", "note")
    if sid == "approve":
        require(m, "administrator", "reviewer")
        b = await _batch(db, m.tenant_id, c.batch_id)
        if b is None:
            raise invalid("Generate the journal batch first.")
        if b.created_by == m.user_id:
            raise forbidden("You generated this batch, so someone else must approve it.")
        b.status, b.approved_by, b.approved_at = "approved", m.user_id, now()

    def apply(steps: list[dict[str, Any]]) -> None:
        i = STEPS.index(sid)
        steps[i].update(
            status="warning" if issues["warnings"] else "done",
            doneBy=m.user_id,
            doneAt=now().isoformat(),
            note=note,
        )
        if i + 1 < len(steps) and steps[i + 1]["status"] == "locked":
            steps[i + 1]["status"] = "todo"

    _set_steps(c, apply)
    await audit(db, m.tenant_id, m.user_id, f"close.{sid}", key, note)


# ─── Batch generation ──────────────────────────────────────────────────────


async def build_batch_lines(db: AsyncSession, tenant_id: str, key: str) -> list[dict[str, Any]]:
    posted = await _posted(db, tenant_id)
    out: list[dict[str, Any]] = []
    for b in await _books(db, tenant_id):
        if not b.journals:
            continue
        through: dict[str, Decimal] = defaultdict(lambda: ZERO)
        normal: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        for j in b.journals:
            if j["month"] > key:
                continue
            signed = Decimal(j["debit"]) - Decimal(j["credit"])
            through[j["account"]] += signed
            if j["month"] == key:
                normal[(j["account"], j["kind"])] += signed
        normal_by_acc: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for (acc, kind), v in normal.items():
            normal_by_acc[acc] += v
            if v:
                out.append(
                    {
                        "leaseId": b.lease_id,
                        "account": acc,
                        "kind": kind,
                        "debit": v if v > 0 else ZERO,
                        "credit": -v if v < 0 else ZERO,
                        "catchUp": False,
                    }
                )
        for acc in set(through) | set(posted.get(b.lease_id, {})):
            due = through[acc] - posted.get(b.lease_id, {}).get(acc, ZERO) - normal_by_acc[acc]
            if abs(due) >= TOL:  # back-dated change affecting locked months
                out.append(
                    {
                        "leaseId": b.lease_id,
                        "account": acc,
                        "kind": "remeasurement",
                        "debit": due if due > 0 else ZERO,
                        "credit": -due if due < 0 else ZERO,
                        "catchUp": True,
                    }
                )
    return out


async def generate(db: AsyncSession, m: TenantMembership, key: str) -> LeaseJournalBatch:
    require(m, "administrator", "analyst")
    await _period(db, m.tenant_id, key, lock=True)
    c = await _close(db, m.tenant_id, key)
    if c is None:
        raise invalid("Start the close first.")
    if _step(c, "generate")["status"] == "locked":
        raise invalid("Finish the earlier steps first.")
    old = await _batch(db, m.tenant_id, c.batch_id)
    if old and old.status != "draft":
        raise invalid("The batch is already approved. Reopen it to regenerate.")
    if old:
        await db.delete(old)
    e = (
        await db.execute(select(LeaseEntity).where(LeaseEntity.tenant_id == m.tenant_id).with_for_update())
    ).scalar_one()
    e.batch_seq += 1
    b = LeaseJournalBatch(
        id=new_ulid(),
        tenant_id=m.tenant_id,
        code=f"JB-{key.replace('-', '')}-{e.batch_seq:03d}",
        period=key,
        type="period",
        status="draft",
        source_checksum=await source_checksum(db, m.tenant_id),
        idempotency_key=f"{m.tenant_id}:{key}:{e.batch_seq}",
        created_by=m.user_id,
    )
    db.add(b)
    await db.flush()
    for ln in await build_batch_lines(db, m.tenant_id, key):
        db.add(
            LeaseJournalLine(
                tenant_id=m.tenant_id,
                batch_id=b.id,
                lease_id=ln["leaseId"],
                account=ln["account"],
                kind=ln["kind"],
                debit=ln["debit"].quantize(Decimal("0.01")),
                credit=ln["credit"].quantize(Decimal("0.01")),
                catch_up=ln["catchUp"],
            )
        )
    c.batch_id = b.id
    if not (
        await db.execute(select(LeaseActualPayment.id).where(LeaseActualPayment.close_id == c.id).limit(1))
    ).first():
        await _expected_payments(db, m.tenant_id, c)

    def apply(steps: list[dict[str, Any]]) -> None:
        g = steps[STEPS.index("generate")]
        g.update(status="done", doneBy=m.user_id, doneAt=now().isoformat())
        nxt = steps[STEPS.index("matching")]
        if nxt["status"] == "locked":
            nxt["status"] = "todo"
        for sid in ("reconcile", "approve", "post"):  # regenerating invalidates later steps
            s = steps[STEPS.index(sid)]
            if s["status"] != "locked":
                s.update(
                    status="todo"
                    if sid == "reconcile" and steps[STEPS.index("matching")]["status"] in ("done", "warning")
                    else "locked",
                    doneBy=None,
                    doneAt=None,
                )

    _set_steps(c, apply)
    await audit(db, m.tenant_id, m.user_id, "close.generate", key, b.code)
    return b


# ─── Payments ──────────────────────────────────────────────────────────────


async def _expected_payments(db: AsyncSession, tenant_id: str, c: LeaseCloseRun) -> None:
    """Scheduled payments this month become 'late' rows until an AP/bank file is uploaded and matched."""
    start, end = f"{c.period}-01", month_end(c.period)
    names = dict(
        (
            await db.execute(
                select(LeaseCounterparty.id, LeaseCounterparty.name).where(LeaseCounterparty.tenant_id == tenant_id)
            )
        ).all()
    )
    for lease, books in (
        await db.execute(
            select(Lease, LeaseBooks)
            .join(LeaseBooks, LeaseBooks.lease_id == Lease.id)
            .where(Lease.tenant_id == tenant_id)
        )
    ).all():
        if books.exempt:
            continue
        from ledgerline.leases.domain.engine import (
            build_cash_flows,  # local: avoids engine import at module load for the router
        )

        last = (
            await db.execute(
                select(LeaseVersion)
                .where(LeaseVersion.lease_id == lease.id, LeaseVersion.status == "approved")
                .order_by(LeaseVersion.n.desc())
                .limit(1)
            )
        ).scalar_one()
        if last.event == "termination":
            continue
        for i, f in enumerate(build_cash_flows(last.terms)):
            if start <= f.date <= end and f.amount:
                db.add(
                    LeaseActualPayment(
                        id=new_ulid(),
                        tenant_id=tenant_id,
                        close_id=c.id,
                        date=date.fromisoformat(f.date),
                        vendor=names.get(lease.counterparty_id, "—"),
                        amount=ZERO,
                        reference=f"EXP-{c.period.replace('-', '')}-{lease.code}-{i}",
                        lease_id=lease.id,
                        expected=f.amount,
                        match="late",
                    )
                )


def _num(s: str) -> Decimal:
    return Decimal((s or "0").replace(",", "").strip() or "0")


async def upload_payments(
    db: AsyncSession, m: TenantMembership, key: str, rows: list[dict[str, str]]
) -> dict[str, int]:
    """AP/bank lines: date, vendor, amount, reference. Auto-match by vendor + amount within ±10 days."""
    require(m, "administrator", "analyst")
    c = await _close(db, m.tenant_id, key)
    if c is None:
        raise invalid("Start the close first.")
    expected = list(
        (
            await db.execute(
                select(LeaseActualPayment).where(
                    LeaseActualPayment.close_id == c.id, LeaseActualPayment.match == "late"
                )
            )
        ).scalars()
    )
    seen_refs = set(
        (await db.execute(select(LeaseActualPayment.reference).where(LeaseActualPayment.close_id == c.id))).scalars()
    )
    matched = added = 0
    for r in rows:
        ref = (r.get("reference") or "").strip()
        if not ref or ref in seen_refs:
            continue  # idempotent re-upload
        try:
            d = date.fromisoformat((r.get("date") or "").strip())
        except ValueError as e:
            raise invalid(f"Payment {ref}: date must be YYYY-MM-DD.") from e
        amt, vendor = _num(r.get("amount", "")), (r.get("vendor") or "").strip()
        hit = next(
            (x for x in expected if x.vendor.lower() == vendor.lower() and abs((x.date - d).days) <= 10),
            None,
        )
        if hit:
            hit.amount, hit.reference, hit.date = amt, ref, d
            hit.match = "matched" if abs(amt - (hit.expected or ZERO)) < Decimal("0.01") else "different"
            expected.remove(hit)
            matched += 1
        else:
            db.add(
                LeaseActualPayment(
                    id=new_ulid(),
                    tenant_id=m.tenant_id,
                    close_id=c.id,
                    date=d,
                    vendor=vendor,
                    amount=amt,
                    reference=ref,
                    match="unmatched",
                )
            )
            added += 1
        seen_refs.add(ref)
    await audit(db, m.tenant_id, m.user_id, "close.payments", key, f"{matched} matched, {added} unmatched")
    return {"matched": matched, "unmatched": added}


async def resolve_payment(
    db: AsyncSession, m: TenantMembership, key: str, pid: str, resolution: str, note: str
) -> None:
    c = await _close(db, m.tenant_id, key)
    p = (
        c
        and (
            await db.execute(
                select(LeaseActualPayment).where(LeaseActualPayment.close_id == c.id, LeaseActualPayment.id == pid)
            )
        ).scalar_one_or_none()
    )
    if not p:
        raise not_found("Payment not found.")
    if resolution not in ("accrue", "event", "not_lease", "accepted"):
        raise invalid("Unknown resolution.")
    if not (note or "").strip() and resolution != "accepted":
        raise invalid("Add a short note.", "note")
    p.resolution, p.resolution_note, p.resolved_by = resolution, note, m.user_id
    await audit(db, m.tenant_id, m.user_id, "close.payment", key, f"{p.reference} → {resolution}")


# ─── Reconciliation ────────────────────────────────────────────────────────


async def reconciliations(db: AsyncSession, tenant_id: str, c: LeaseCloseRun) -> list[dict[str, Any]]:
    end = month_end(c.period)
    liab_books = rou_books = ZERO
    for b in await _books(db, tenant_id):
        row = None
        for r in b.rows:
            if r["periodEnd"] <= end:
                row = r
        if row:
            liab_books += Decimal(row["liabClosing"])
            rou_books += Decimal(row["rouClosing"])
    posted = await _posted(db, tenant_id)
    batch = await _batch(db, tenant_id, c.batch_id)
    lines = (
        list((await db.execute(select(LeaseJournalLine).where(LeaseJournalLine.batch_id == batch.id))).scalars())
        if batch
        else []
    )

    def gl(accs: tuple[str, ...]) -> Decimal:
        v = sum((a for per in posted.values() for k, a in per.items() if k in accs), ZERO)
        return v + sum((ln.debit - ln.credit for ln in lines if ln.account in accs), ZERO)

    liab_gl, rou_gl = -gl(("lease_liability",)), gl(("rou_cost", "rou_accdep"))
    dr, cr = sum((ln.debit for ln in lines), ZERO), sum((ln.credit for ln in lines), ZERO)
    return [
        {
            "id": "liab",
            "label": "Lease liability: subledger vs GL",
            "books": f"{liab_books:.2f}",
            "gl": f"{liab_gl:.2f}",
            "ok": abs(liab_books - liab_gl) < Decimal("0.01"),
            "diff": f"{liab_books - liab_gl:.2f}",
        },
        {
            "id": "rou",
            "label": "ROU asset (net): subledger vs GL",
            "books": f"{rou_books:.2f}",
            "gl": f"{rou_gl:.2f}",
            "ok": abs(rou_books - rou_gl) < Decimal("0.01"),
            "diff": f"{rou_books - rou_gl:.2f}",
        },
        {
            "id": "bal",
            "label": "Batch debits = credits",
            "books": f"{dr:.2f}",
            "gl": f"{cr:.2f}",
            "ok": batch is not None and dr == cr,
            "diff": f"{dr - cr:.2f}",
        },
    ]


# ─── Export / post / lock / reopen ─────────────────────────────────────────


async def export_batch(db: AsyncSession, m: TenantMembership, batch_id: str) -> str:
    b = await _batch(db, m.tenant_id, batch_id)
    if b is None:
        raise not_found("Batch not found.")
    if b.status == "draft":
        raise invalid("Approve the batch before exporting.")
    if b.status == "approved":
        b.status = "exported"
    b.exported_at = now()
    gl = in_force(await setup_versions(db, m.tenant_id, "gl"), month_end(b.period))
    accounts = (gl.data.get("accounts") if gl else {}) or {}
    leases = {lz.id: lz for lz in (await db.execute(select(Lease).where(Lease.tenant_id == m.tenant_id))).scalars()}
    ent = (await db.execute(select(LeaseEntity).where(LeaseEntity.tenant_id == m.tenant_id))).scalar_one()
    from ledgerline.leases.domain.engine import ACCOUNT_LABELS

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "batch_id",
            "period",
            "posting_date",
            "lease_code",
            "entity",
            "account",
            "gl_account",
            "debit",
            "credit",
            "currency",
            "narration",
            "catch_up",
            "idempotency_key",
        ]
    )
    for ln in (
        await db.execute(
            select(LeaseJournalLine).where(LeaseJournalLine.batch_id == b.id).order_by(LeaseJournalLine.id)
        )
    ).scalars():
        lz = leases.get(ln.lease_id)
        w.writerow(
            [
                b.code,
                b.period,
                month_end(b.period),
                lz.code if lz else ln.lease_id,
                ent.name,
                ACCOUNT_LABELS.get(ln.account, ln.account),
                accounts.get(ln.account, ""),
                f"{ln.debit:.2f}",
                f"{ln.credit:.2f}",
                ent.currency,
                f"{lz.name if lz else ''} — {ln.kind}",
                "Y" if ln.catch_up else "",
                f"{b.idempotency_key}:{ln.lease_id}:{ln.account}:{ln.kind}",
            ]
        )
    await audit(db, m.tenant_id, m.user_id, "journal.export", b.code)
    return buf.getvalue()


async def post_batch(db: AsyncSession, m: TenantMembership, batch_id: str, erp_ref: str) -> None:
    require(m, "administrator")
    b = (
        await db.execute(
            select(LeaseJournalBatch)
            .where(LeaseJournalBatch.tenant_id == m.tenant_id, LeaseJournalBatch.id == batch_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if b is None:
        raise not_found("Batch not found.")
    ref = (erp_ref or "").strip()
    if not ref:
        raise invalid("Enter the ERP document reference.", "erpRef")
    if b.status == "posted":
        if b.erp_ref == ref:
            return  # idempotent retry of the same post
        raise conflict(f"Already posted as {b.erp_ref}. It won't be posted twice.")
    if b.status not in ("exported", "approved"):
        raise invalid("Approve and export the batch first.")
    if (
        await db.execute(
            select(LeaseJournalBatch.id).where(
                LeaseJournalBatch.tenant_id == m.tenant_id, LeaseJournalBatch.erp_ref == ref
            )
        )
    ).first():
        raise conflict("That ERP reference is already used by another batch.")
    b.status, b.erp_ref, b.posted_at, b.posted_by = "posted", ref, now(), m.user_id
    await audit(db, m.tenant_id, m.user_id, "journal.post", b.code, ref)


async def _apply_lines(db: AsyncSession, tenant_id: str, batch_id: str, sign: int) -> None:
    """Add (or with sign=-1, remove) a batch's signed amounts to the posted balances."""
    totals: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    for ln in (await db.execute(select(LeaseJournalLine).where(LeaseJournalLine.batch_id == batch_id))).scalars():
        totals[(ln.lease_id, ln.account)] += ln.debit - ln.credit
    existing = {
        (pb.lease_id, pb.account): pb
        for pb in (
            await db.execute(select(LeasePostedBalance).where(LeasePostedBalance.tenant_id == tenant_id))
        ).scalars()
    }
    for (lease_id, account), amt in totals.items():
        pb = existing.get((lease_id, account))
        if pb is None:
            db.add(LeasePostedBalance(tenant_id=tenant_id, lease_id=lease_id, account=account, amount=sign * amt))
        else:
            pb.amount = pb.amount + sign * amt


async def lock(db: AsyncSession, m: TenantMembership, key: str) -> None:
    require(m, "administrator")
    p = await _period(db, m.tenant_id, key, lock=True)
    c = await _close(db, m.tenant_id, key)
    if c is None:
        raise invalid("Start the close first.")
    if any(s["status"] not in ("done", "warning") for s in c.steps if s["id"] != "post"):
        raise invalid("Finish every close step first.")
    b = await _batch(db, m.tenant_id, c.batch_id)
    if b is None or b.status != "posted":
        raise invalid("Post the batch first.")
    await _apply_lines(db, m.tenant_id, b.id, +1)
    _set_steps(
        c,
        lambda steps: steps[STEPS.index("post")].update(status="done", doneBy=m.user_id, doneAt=now().isoformat()),
    )
    p.status, p.locked_by, p.locked_at = "locked", m.user_id, now()
    nk = next_month_key(key)
    if not (
        await db.execute(select(LeasePeriod.id).where(LeasePeriod.tenant_id == m.tenant_id, LeasePeriod.key == nk))
    ).first():
        db.add(LeasePeriod(id=new_ulid(), tenant_id=m.tenant_id, key=nk, status="open", reopen_log=[]))
    await audit(db, m.tenant_id, m.user_id, "period.lock", key)
    await notify(
        db,
        m.tenant_id,
        "*",
        f"{key} is locked",
        "Changes dated in this period now post as catch-ups.",
        "/leases/close",
    )


async def reopen(db: AsyncSession, m: TenantMembership, key: str, reason: str) -> None:
    require(m, "administrator")
    if not (reason or "").strip():
        raise invalid("A reason is required to reopen a period.", "reason")
    p = await _period(db, m.tenant_id, key, lock=True)
    if p.status != "locked":
        raise invalid("Only locked periods can be reopened.")
    last = (
        await db.execute(
            select(LeasePeriod.key)
            .where(LeasePeriod.tenant_id == m.tenant_id, LeasePeriod.status == "locked")
            .order_by(LeasePeriod.key.desc())
            .limit(1)
        )
    ).scalar_one()
    if last != key:
        raise invalid("Only the most recently locked period can be reopened.")
    nk = next_month_key(key)
    if await _close(db, m.tenant_id, nk):
        raise invalid(f"{nk} close has started. Cancel it first.")
    c = await _close(db, m.tenant_id, key)
    b = await _batch(db, m.tenant_id, c.batch_id) if c else None
    if b:
        await _apply_lines(db, m.tenant_id, b.id, -1)
        e = (
            await db.execute(select(LeaseEntity).where(LeaseEntity.tenant_id == m.tenant_id).with_for_update())
        ).scalar_one()
        e.batch_seq += 1
        rev = LeaseJournalBatch(
            id=new_ulid(),
            tenant_id=m.tenant_id,
            code=f"JB-{key.replace('-', '')}-R{e.batch_seq}",
            period=key,
            type="reversal",
            status="posted",
            idempotency_key=f"{b.idempotency_key}:rev:{e.batch_seq}",
            created_by=m.user_id,
            posted_at=now(),
            posted_by=m.user_id,
            erp_ref=f"REV-{b.erp_ref or b.code}-{e.batch_seq}",
            reversal_of=b.id,
        )
        db.add(rev)
        await db.flush()
        for ln in (await db.execute(select(LeaseJournalLine).where(LeaseJournalLine.batch_id == b.id))).scalars():
            db.add(
                LeaseJournalLine(
                    tenant_id=m.tenant_id,
                    batch_id=rev.id,
                    lease_id=ln.lease_id,
                    account=ln.account,
                    kind=ln.kind,
                    debit=ln.credit,
                    credit=ln.debit,
                    catch_up=ln.catch_up,
                )
            )
        b.status, b.reversed_by = "reversed", m.user_id
        c.batch_id = None
    if c:
        await db.execute(delete(LeaseCloseRun).where(LeaseCloseRun.id == c.id))
    await db.execute(
        delete(LeasePeriod).where(
            LeasePeriod.tenant_id == m.tenant_id,
            LeasePeriod.key == nk,
            LeasePeriod.status == "open",
        )
    )
    p.status, p.locked_by, p.locked_at = "open", None, None
    p.reopen_log = [*p.reopen_log, {"by": m.user_id, "at": now().isoformat(), "reason": reason}]
    await audit(db, m.tenant_id, m.user_id, "period.reopen", key, reason)
    await notify(db, m.tenant_id, "*", f"{key} was reopened", reason, "/leases/close")


# ─── Serialization ─────────────────────────────────────────────────────────


async def ser_close(db: AsyncSession, c: LeaseCloseRun) -> dict[str, Any]:
    pays = (
        await db.execute(
            select(LeaseActualPayment).where(LeaseActualPayment.close_id == c.id).order_by(LeaseActualPayment.date)
        )
    ).scalars()
    return {
        "period": c.period,
        "steps": c.steps,
        "batchId": c.batch_id,
        "startedBy": c.started_by,
        "startedAt": iso(c.started_at),
        "payments": [
            {
                "id": p.id,
                "date": p.date.isoformat(),
                "vendor": p.vendor,
                "amount": f"{p.amount:.2f}",
                "reference": p.reference,
                "leaseId": p.lease_id,
                "expected": money(p.expected),
                "match": p.match,
                "resolution": p.resolution,
                "resolutionNote": p.resolution_note,
                "resolvedBy": p.resolved_by,
            }
            for p in pays
        ],
    }


async def ser_batch(db: AsyncSession, b: LeaseJournalBatch, with_lines: bool = True) -> dict[str, Any]:
    lines = (
        list(
            (
                await db.execute(
                    select(LeaseJournalLine).where(LeaseJournalLine.batch_id == b.id).order_by(LeaseJournalLine.id)
                )
            ).scalars()
        )
        if with_lines
        else []
    )
    return {
        "id": b.id,
        "code": b.code,
        "period": b.period,
        "type": b.type,
        "status": b.status,
        "createdAt": iso(b.created_at),
        "createdBy": b.created_by,
        "approvedBy": b.approved_by,
        "approvedAt": iso(b.approved_at),
        "exportedAt": iso(b.exported_at),
        "erpRef": b.erp_ref,
        "postedAt": iso(b.posted_at),
        "postedBy": b.posted_by,
        "reversalOf": b.reversal_of,
        "reversedBy": b.reversed_by,
        "idempotencyKey": b.idempotency_key,
        "sourceRevs": b.source_checksum,
        "lines": [
            {
                "leaseId": ln.lease_id,
                "account": ln.account,
                "kind": ln.kind,
                "debit": f"{ln.debit:.2f}",
                "credit": f"{ln.credit:.2f}",
                "catchUp": ln.catch_up,
            }
            for ln in lines
        ],
    }
