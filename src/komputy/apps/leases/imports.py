"""Bulk lease import: upload (CSV/XLSX) → map → validate (job) → accept warnings → commit (job, one
savepoint per lease) → report → retry failed / submit all for review. Same row rules as the web.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from komputy.apps.leases.common import (
    audit,
    checksum,
    conflict,
    curve_rate,
    day_count,
    entity_of,
    forbidden,
    in_force,
    invalid,
    iso,
    locked_keys,
    not_found,
    notify,
    now,
    require,
    require_setup,
    segment_inputs,
    setup_versions,
    term_years,
)
from komputy.apps.leases.models import (
    Lease,
    LeaseCounterparty,
    LeaseEntity,
    LeaseImportBatch,
    LeaseImportRow,
    LeaseVersion,
)
from komputy.apps.leases.service import blank_assessment, chain_of, pending_of, touch
from komputy.auth.deps import Membership as TenantMembership
from komputy.kernel.db_hooks import run_after_commit
from komputy.kernel.ids import new_ulid
from komputy.leases.domain.dates import is_iso_date
from komputy.leases.domain.engine import ENGINE_VERSION, compute
from komputy.log import get_logger

_log = get_logger("app.leases.imports")
TEMPLATE_VERSION = "LL-LEASES-1.0"
COLUMNS = [
    ("lease_ref", "Lease reference", True),
    ("name", "Lease name", True),
    ("asset_class", "Asset class", True),
    ("lessor", "Lessor", True),
    ("location", "Location", False),
    ("currency", "Currency", True),
    ("commencement_date", "Commencement date", True),
    ("end_date", "End date", True),
    ("payment_amount", "Payment amount", True),
    ("frequency", "Frequency", True),
    ("timing", "Timing", True),
    ("rent_free_months", "Rent-free months", False),
    ("escalation_pct", "Escalation %", False),
    ("escalation_every_months", "Escalation every (months)", False),
    ("discount_rate_pct", "Discount rate %", False),
    ("initial_direct_costs", "Initial direct costs", False),
    ("incentives", "Incentives received", False),
    ("restoration_estimate", "Restoration estimate", False),
    ("exemption", "Exemption", False),
]
MAX_ROWS, MAX_BYTES, CHUNK = 10_000, 20 * 1024 * 1024, 100
_jobs: set[asyncio.Task[None]] = set()


def template_rows() -> list[list[str]]:
    return [
        [c[0] for c in COLUMNS],
        [
            "HQ-01",
            "Head office Kigali",
            "property",
            "Vision City Estates",
            "Kigali",
            "RWF",
            "2027-01-01",
            "2031-12-31",
            "4500000",
            "monthly",
            "advance",
            "2",
            "5",
            "12",
            "",
            "1200000",
            "0",
            "8000000",
            "none",
        ],
        [
            "FLEET-07",
            "Toyota Hilux ×4",
            "vehicles",
            "Rwanda Motor Leasing",
            "Kigali",
            "RWF",
            "2027-02-01",
            "2030-01-31",
            "1850000",
            "monthly",
            "arrears",
            "0",
            "",
            "",
            "14.5",
            "0",
            "0",
            "0",
            "none",
        ],
    ]


def parse_file(name: str, raw: bytes) -> list[list[str]]:
    if name.lower().endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        rows = []
        for r in ws.iter_rows(values_only=True):
            vals = [
                ""
                if v is None
                else (
                    v.date().isoformat()
                    if hasattr(v, "date") and not isinstance(v, date)
                    else v.isoformat()
                    if isinstance(v, date)
                    else str(v)
                )
                for v in r
            ]
            if any(x.strip() for x in vals):
                rows.append(vals)
        return rows
    if name.lower().endswith(".csv"):
        text = raw.decode("utf-8-sig", errors="replace")
        return [r for r in csv.reader(io.StringIO(text)) if any(x.strip() for x in r)]
    raise invalid("Upload a .csv or .xlsx file.")


def _norm(x: str) -> str:
    return "".join(ch for ch in x.lower() if ch.isalnum())


async def create(db: AsyncSession, m: TenantMembership, file_name: str, raw: bytes) -> LeaseImportBatch:
    require(m, "administrator", "analyst")
    await require_setup(db, m.tenant_id)
    if len(raw) > MAX_BYTES:
        raise invalid("File is larger than 20 MB.")
    sha = hashlib.sha256(raw).hexdigest()
    dup = (
        await db.execute(
            select(LeaseImportBatch).where(
                LeaseImportBatch.tenant_id == m.tenant_id,
                LeaseImportBatch.sha256 == sha,
                LeaseImportBatch.status == "committed",
            )
        )
    ).scalar_one_or_none()
    if dup:
        raise conflict(
            f"This exact file was already imported on {dup.committed_at.date().isoformat() if dup.committed_at else ''} (batch {dup.code})."
        )
    rows = parse_file(file_name, raw)
    if len(rows) < 2:
        raise invalid("The file has no data rows.")
    if len(rows) > MAX_ROWS + 1:
        raise invalid("Up to 10,000 leases per file.")
    e = (
        await db.execute(select(LeaseEntity).where(LeaseEntity.tenant_id == m.tenant_id).with_for_update())
    ).scalar_one()
    headers = [h.strip() for h in rows[0]]
    mapping = dict(e.import_mapping or {})
    for key, label, _ in COLUMNS:
        hit = next((h for h in headers if _norm(h) in (_norm(key), _norm(label))), None)
        if hit:
            mapping[key] = hit
        elif mapping.get(key) not in headers:
            mapping.pop(key, None)
    e.import_seq += 1
    b = LeaseImportBatch(
        id=new_ulid(),
        tenant_id=m.tenant_id,
        code=f"IMP-{e.import_seq:04d}",
        file_name=file_name,
        sha256=sha,
        size=len(raw),
        status="mapping",
        progress=0,
        headers=headers,
        mapping=mapping,
        template_version=TEMPLATE_VERSION,
        created_by=m.user_id,
    )
    db.add(b)
    await db.flush()
    for i, r in enumerate(rows[1:]):
        db.add(
            LeaseImportRow(
                batch_id=b.id,
                row=i + 2,
                values={h: (r[j].strip() if j < len(r) else "") for j, h in enumerate(headers)},
                issues=[],
                action="create",
            )
        )
    await audit(db, m.tenant_id, m.user_id, "import.upload", b.code, file_name)
    return b


async def get(db: AsyncSession, tenant_id: str, code: str, lock: bool = False) -> LeaseImportBatch:
    q = select(LeaseImportBatch).where(LeaseImportBatch.tenant_id == tenant_id, LeaseImportBatch.code == code)
    b = (await db.execute(q.with_for_update() if lock else q)).scalar_one_or_none()
    if b is None:
        raise not_found("Import not found.")
    return b


def _schedule(db: AsyncSession, batch_id: str) -> None:
    """Validate/commit runs on the `io` queue after the request commits (one job per batch at a time)."""

    def go() -> None:
        from komputy.apps.leases.jobs import import_job

        t = asyncio.get_event_loop().create_task(
            import_job.configure(queueing_lock=f"import:{batch_id}").defer_async(batch_id=batch_id)
        )
        _jobs.add(t)
        t.add_done_callback(_jobs.discard)

    run_after_commit(db, go)


async def set_mapping(db: AsyncSession, m: TenantMembership, code: str, mapping: dict[str, str]) -> None:
    b = await get(db, m.tenant_id, code, lock=True)
    missing = [label for key, label, req in COLUMNS if req and not mapping.get(key)]
    if missing:
        raise invalid(f"Map the required column{'s' if len(missing) > 1 else ''}: {', '.join(missing)}.")
    if any(v and v not in b.headers for v in mapping.values()):
        raise invalid("Mapping refers to a column that isn't in the file.")
    b.mapping, b.status, b.progress = mapping, "validating", 0
    e = await entity_of(db, m.tenant_id)
    e.import_mapping = mapping
    _schedule(db, b.id)


def _dec(s: str) -> Decimal | None:
    try:
        return Decimal(s.replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        return None


async def _validate_rows(
    db: AsyncSession, b: LeaseImportBatch, rows: list[LeaseImportRow], all_rows_refs: dict[str, int]
) -> None:
    ent = await entity_of(db, b.tenant_id)
    locked = await locked_keys(db, b.tenant_id)
    leases = list((await db.execute(select(Lease).where(Lease.tenant_id == b.tenant_id))).scalars())
    for r in rows:
        issues: list[dict[str, Any]] = []

        def v(k: str, r: LeaseImportRow = r) -> str:
            return (r.values.get(b.mapping.get(k) or "", "") or "").strip()

        def err(c: str, msg: str, r: LeaseImportRow = r, issues: list = issues) -> None:
            issues.append({"row": r.row, "column": c, "severity": "error", "message": msg})

        def warn(c: str, msg: str, r: LeaseImportRow = r, issues: list = issues) -> None:
            issues.append({"row": r.row, "column": c, "severity": "warning", "message": msg})

        r.ref, r.name, r.action = v("lease_ref"), v("name"), "create"
        for key, label, req in COLUMNS:
            if req and not v(key):
                err(key, f"{label} is required.")
        if r.ref and all_rows_refs.get(r.ref.lower(), 0) > 1:
            err("lease_ref", "Duplicate reference in this file.")
        cls = v("asset_class").lower()
        if cls and cls not in ("property", "vehicles", "equipment", "it", "land"):
            err(
                "asset_class",
                f'Unknown asset class "{cls}". Use property, vehicles, equipment, it or land.',
            )
        cur = v("currency").upper()
        if cur and ent and cur != ent.currency:
            err("currency", f"Only {ent.currency} leases are supported (got {cur}).")
        cd, ed = v("commencement_date"), v("end_date")
        if cd and not is_iso_date(cd):
            err(
                "commencement_date",
                f"Looks like an Excel date number ({cd}). Format the column as YYYY-MM-DD."
                if cd.isdigit()
                else f'"{cd}" isn\'t a valid date. Use YYYY-MM-DD.',
            )
        if ed and not is_iso_date(ed):
            err("end_date", f'"{ed}" isn\'t a valid date. Use YYYY-MM-DD.')
        if is_iso_date(cd) and is_iso_date(ed) and ed <= cd:
            err("end_date", "End date must be after commencement.")
        if is_iso_date(cd) and cd[:7] in locked:
            warn(
                "commencement_date",
                "Commences in a locked period: entries will post as catch-ups.",
            )
        amt = _dec(v("payment_amount"))
        if v("payment_amount") and (amt is None or amt <= 0):
            err("payment_amount", f'"{v("payment_amount")}" isn\'t a positive amount.')
        if v("frequency") and v("frequency").lower() not in (
            "monthly",
            "quarterly",
            "semiannual",
            "annual",
        ):
            err("frequency", "Use monthly, quarterly, semiannual or annual.")
        if v("timing") and v("timing").lower() not in ("advance", "arrears"):
            err("timing", "Use advance or arrears.")
        for k in ("rent_free_months", "escalation_every_months"):
            if v(k) and not v(k).isdigit():
                err(k, "Use a whole number of months.")
        for k in ("escalation_pct", "initial_direct_costs", "incentives", "restoration_estimate"):
            if v(k) and (_dec(v(k)) is None or _dec(v(k)) < 0):
                err(k, f'"{v(k)}" isn\'t a valid number.')
        rate = v("discount_rate_pct")
        if rate:
            rd = _dec(rate)
            if rd is None:
                err("discount_rate_pct", f'"{rate}" isn\'t a number.')
            elif 0 < rd < 1:
                warn(
                    "discount_rate_pct",
                    f"{rate} looks like a decimal. Rates are percentages, so it will be read as {rate}%: did you mean {rd * 100}%?",
                )
            elif rd <= 0 or rd > 40:
                err("discount_rate_pct", "Rate must be between 0% and 40%.")
        else:
            warn("discount_rate_pct", "No rate given: the discount-rate curve will be used.")
        ex = v("exemption").lower()
        if ex and ex not in ("none", "short_term", "low_value"):
            err("exemption", "Use none, short_term or low_value.")
        existing = next(
            (lz for lz in leases if lz.code.lower() == r.ref.lower() or (r.name and lz.name.lower() == r.name.lower())),
            None,
        )
        if existing:
            approved = (
                await db.execute(
                    select(LeaseVersion.id)
                    .where(LeaseVersion.lease_id == existing.id, LeaseVersion.status == "approved")
                    .limit(1)
                )
            ).first()
            if approved:
                err(
                    "lease_ref",
                    f"Already on the books as {existing.code}. Record changes as an event on the lease instead.",
                )
            else:
                r.action, r.lease_id = "update", existing.id
                warn("lease_ref", f"Updates draft {existing.code}.")
        r.issues = issues
        if any(i["severity"] == "error" for i in issues):
            r.action = "skip"


async def _commit_row(db: AsyncSession, b: LeaseImportBatch, r: LeaseImportRow) -> None:
    v = lambda k: (r.values.get(b.mapping.get(k) or "", "") or "").strip()  # noqa: E731
    ent = (
        await db.execute(select(LeaseEntity).where(LeaseEntity.tenant_id == b.tenant_id).with_for_update())
    ).scalar_one()
    cp_name = v("lessor")
    cp = (
        await db.execute(
            select(LeaseCounterparty).where(
                LeaseCounterparty.tenant_id == b.tenant_id,
                func.lower(LeaseCounterparty.name) == cp_name.lower(),
            )
        )
    ).scalar_one_or_none()
    if cp is None:
        cp = LeaseCounterparty(id=new_ulid(), tenant_id=b.tenant_id, name=cp_name)
        db.add(cp)
    cd, ed = v("commencement_date"), v("end_date")
    ex = v("exemption").lower() or "none"
    rate_pct = v("discount_rate_pct")
    if rate_pct:
        rate = {
            "value": str(Decimal(rate_pct) / 100),
            "source": "override",
            "reason": "Rate supplied in import file",
        }
    else:
        curve = in_force(await setup_versions(db, b.tenant_id, "curves"), cd)
        yrs = term_years(cd, ed)
        rate = {
            "value": curve_rate(curve, ent.currency, yrs) or "",
            "source": "curve",
            "curveVersion": curve.version if curve else None,
            "termYears": yrs,
        }
    terms = {
        "commencementDate": cd,
        "endDate": ed,
        "manualLines": [],
        "nonLeasePerPayment": "0",
        "separateNonLease": True,
        "prepaidBeforeCommencement": "0",
        "initialDirectCosts": str(_dec(v("initial_direct_costs")) or 0),
        "incentivesReceived": str(_dec(v("incentives")) or 0),
        "restorationEstimate": str(_dec(v("restoration_estimate")) or 0),
        "paymentRule": {
            "amount": str(_dec(v("payment_amount"))),
            "frequency": v("frequency").lower(),
            "timing": v("timing").lower(),
            "rentFreeMonths": int(v("rent_free_months") or 0),
            "escalation": {
                "type": "fixed_pct",
                "pct": str(Decimal(v("escalation_pct")) / 100),
                "everyMonths": int(v("escalation_every_months") or 12),
            }
            if v("escalation_pct")
            else {"type": "none"},
        },
    }
    assessment = {
        **blank_assessment(),
        "identifiedAsset": True,
        "rightToControl": True,
        "exemption": ex,
        "exemptionRationale": "" if ex == "none" else "Per import file: confirm in review",
        "termRationale": "Non-cancellable term per contract (import).",
    }
    if r.action == "update" and r.lease_id:
        lease = await db.get(Lease, r.lease_id)
        if lease is None or lease.tenant_id != b.tenant_id:
            raise ValueError("Draft no longer exists.")
        vs = list((await db.execute(select(LeaseVersion).where(LeaseVersion.lease_id == lease.id))).scalars())
        if chain_of(vs):
            raise ValueError("Lease was approved meanwhile: record an event instead.")
        for old in vs:
            await db.delete(old)
        await db.flush()
        touch(lease, None)
        lease.name, lease.counterparty_id = r.name or lease.name, cp.id
    else:
        ent.lease_seq += 1
        lease = Lease(
            id=new_ulid(),
            tenant_id=b.tenant_id,
            code=f"L-{ent.lease_seq:04d}",
            name=v("name"),
            asset_class=v("asset_class").lower(),
            counterparty_id=cp.id,
            entity_id=ent.id,
            location=v("location"),
            currency=v("currency").upper(),
            owner_id=b.created_by,
            cost_centre="",
            wizard_step=4,
            rev=1,
            import_batch_id=b.id,
        )
        db.add(lease)
        await db.flush()
    ver = LeaseVersion(
        id=new_ulid(),
        tenant_id=b.tenant_id,
        lease_id=lease.id,
        n=1,
        status="draft",
        event="initial",
        effective_date=date.fromisoformat(cd),
        reason=f"Imported from {b.file_name}",
        terms=terms,
        assessment=assessment,
        rate=rate,
        comments=[],
        created_by=b.created_by,
    )
    from komputy.apps.leases.service import validate_version

    errs = await validate_version(db, lease, ver, [])
    if errs:
        raise ValueError(next(iter(errs.values())))
    dc = await day_count(db, b.tenant_id)
    if ex == "none":
        c = compute(segment_inputs([ver]), dc)
        if not c.ok:
            raise ValueError("Integrity checks failed for this lease.")
        ver.calc_liability, ver.calc_rou = (
            c.segments[0].L0.quantize(Decimal("0.01")),
            c.segments[0].R0.quantize(Decimal("0.01")),
        )
    else:
        ver.calc_liability = ver.calc_rou = Decimal(0)
    ver.calc_checks_ok, ver.calc_at, ver.engine_version = True, now(), ENGINE_VERSION
    ver.input_checksum = checksum({"inputs": segment_inputs([ver]), "dayCount": dc})
    db.add(ver)
    r.lease_id, r.outcome, r.outcome_message = (
        lease.id,
        "updated" if r.action == "update" else "created",
        lease.code,
    )


async def run_job(batch_id: str) -> None:
    """Validate or commit in chunks, committing progress after each chunk. Never raises."""
    from komputy.db import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            b = await db.get(LeaseImportBatch, batch_id)
            if b is None or b.status not in ("validating", "committing"):
                return
            rows = list(
                (
                    await db.execute(
                        select(LeaseImportRow).where(LeaseImportRow.batch_id == b.id).order_by(LeaseImportRow.row)
                    )
                ).scalars()
            )
            if b.status == "validating":
                refs: dict[str, int] = {}
                for r in rows:
                    k = (r.values.get(b.mapping.get("lease_ref") or "", "") or "").strip().lower()
                    refs[k] = refs.get(k, 0) + 1
                for i in range(0, len(rows), CHUNK):
                    await _validate_rows(db, b, rows[i : i + CHUNK], refs)
                    b.progress = round(min(len(rows), i + CHUNK) / len(rows) * 100)
                    await db.commit()
                b.status, b.progress = "validated", 100
                await audit(db, b.tenant_id, b.created_by, "import.validate", b.code)
                await notify(
                    db,
                    b.tenant_id,
                    b.created_by,
                    f"Import {b.code} is validated",
                    f"{b.file_name}: review results and commit",
                    f"/leases/imports/{b.code}",
                )
                await db.commit()
                return
            for i, r in enumerate(rows):
                if r.action == "skip":
                    r.outcome = "skipped"
                elif not (b.retry_only and r.outcome != "failed") and r.outcome not in (
                    "created",
                    "updated",
                ):
                    try:
                        async with db.begin_nested():  # one lease = one savepoint; a failure rolls back only this row
                            await _commit_row(db, b, r)
                    except Exception as exc:
                        r.outcome, r.outcome_message = "failed", str(exc)[:500]
                if (i + 1) % CHUNK == 0:
                    b.progress = round((i + 1) / len(rows) * 100)
                    await db.commit()
            b.status, b.progress, b.retry_only = "committed", 100, False
            b.committed_at = now()
            ok = sum(1 for r in rows if r.outcome in ("created", "updated"))
            bad = sum(1 for r in rows if r.outcome == "failed")
            await audit(
                db,
                b.tenant_id,
                b.committed_by or b.created_by,
                "import.commit",
                b.code,
                f"{ok} leases",
            )
            await notify(
                db,
                b.tenant_id,
                b.created_by,
                f"Import {b.code} finished",
                f"{ok} created · {bad} failed",
                f"/leases/imports/{b.code}",
            )
            await db.commit()
    except Exception:
        _log.exception("lease_import_job_failed", batch_id=batch_id)
        async with AsyncSessionLocal() as db:
            b = await db.get(LeaseImportBatch, batch_id)
            if b and b.status in ("validating", "committing"):
                b.status = "failed"
                await db.commit()


async def accept_warnings(db: AsyncSession, m: TenantMembership, code: str) -> None:
    b = await get(db, m.tenant_id, code, lock=True)
    b.warnings_accepted, b.warnings_accepted_by = True, m.user_id
    await audit(db, m.tenant_id, m.user_id, "import.accept_warnings", b.code)


async def start_commit(db: AsyncSession, m: TenantMembership, code: str) -> None:
    require(m, "administrator")
    b = await get(db, m.tenant_id, code, lock=True)
    if b.status != "validated":
        raise invalid("Validate the file first.")
    rows = list((await db.execute(select(LeaseImportRow).where(LeaseImportRow.batch_id == b.id))).scalars())
    if (
        any(r.action != "skip" and any(i["severity"] == "warning" for i in r.issues) for r in rows)
        and not b.warnings_accepted
    ):
        raise invalid("Review and accept the warnings first.")
    if not any(r.action != "skip" for r in rows):
        raise invalid("No valid rows to import.")
    b.status, b.progress, b.committed_by = "committing", 0, m.user_id
    _schedule(db, b.id)


async def retry_failed(db: AsyncSession, m: TenantMembership, code: str) -> None:
    require(m, "administrator")
    b = await get(db, m.tenant_id, code, lock=True)
    if not (
        await db.execute(
            select(LeaseImportRow.id)
            .where(LeaseImportRow.batch_id == b.id, LeaseImportRow.outcome == "failed")
            .limit(1)
        )
    ).first():
        raise invalid("Nothing failed.")
    b.status, b.progress, b.retry_only = "committing", 0, True
    _schedule(db, b.id)


async def cancel(db: AsyncSession, m: TenantMembership, code: str) -> None:
    b = await get(db, m.tenant_id, code, lock=True)
    if b.status == "committed":
        raise invalid("Committed imports can't be cancelled.")
    b.status = "cancelled"


async def submit_all(db: AsyncSession, m: TenantMembership, code: str, approver_id: str) -> int:
    require(m, "administrator", "analyst")
    if approver_id == m.user_id:
        raise forbidden("You can't approve your own leases.")
    from komputy.apps.leases.service import _member_ids

    if approver_id not in await _member_ids(db, m.tenant_id, ("reviewer", "administrator")):
        raise invalid("Choose a reviewer or administrator.")
    b = await get(db, m.tenant_id, code)
    n = 0
    for r in (
        await db.execute(
            select(LeaseImportRow).where(
                LeaseImportRow.batch_id == b.id, LeaseImportRow.outcome.in_(("created", "updated"))
            )
        )
    ).scalars():
        vs = list((await db.execute(select(LeaseVersion).where(LeaseVersion.lease_id == r.lease_id))).scalars())
        v = pending_of(vs)
        if not v or v.status != "draft" or not v.calc_checks_ok:
            continue
        v.status, v.submitted_by, v.submitted_at, v.approver_id = (
            "in_review",
            m.user_id,
            now(),
            approver_id,
        )
        lease = await db.get(Lease, r.lease_id)
        if lease is None:  # discarded after the import
            continue
        touch(lease, None)
        n += 1
    await notify(
        db,
        m.tenant_id,
        approver_id,
        f"{n} imported leases need review",
        f"From {b.file_name}",
        "/leases/inbox",
    )
    return n


async def ser(db: AsyncSession, b: LeaseImportBatch, with_rows: bool = True) -> dict[str, Any]:
    rows = (
        list(
            (
                await db.execute(
                    select(LeaseImportRow).where(LeaseImportRow.batch_id == b.id).order_by(LeaseImportRow.row)
                )
            ).scalars()
        )
        if with_rows
        else []
    )
    return {
        "id": b.code,
        "fileName": b.file_name,
        "sha256": b.sha256,
        "size": b.size,
        "status": b.status,
        "progress": b.progress,
        "headers": b.headers,
        "mapping": b.mapping,
        "warningsAccepted": b.warnings_accepted,
        "createdBy": b.created_by,
        "createdAt": iso(b.created_at),
        "committedBy": b.committed_by,
        "committedAt": iso(b.committed_at),
        "templateVersion": b.template_version,
        "rows": [
            {
                "row": r.row,
                "ref": r.ref,
                "name": r.name,
                "values": r.values,
                "issues": r.issues,
                "action": r.action,
                "leaseId": r.lease_id,
                "outcome": r.outcome,
                "outcomeMessage": r.outcome_message,
            }
            for r in rows
        ],
        "rowCount": len(rows) if with_rows else None,
    }


def error_csv(b: dict[str, Any]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([*b["headers"], "row", "severity", "column", "message"])
    for r in b["rows"]:
        for i in r["issues"]:
            w.writerow(
                [
                    *(r["values"].get(h, "") for h in b["headers"]),
                    r["row"],
                    i["severity"],
                    i["column"],
                    i["message"],
                ]
            )
    return buf.getvalue()
