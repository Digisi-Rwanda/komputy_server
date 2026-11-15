"""Lease portfolio read model for the web UI: register, overview figures, roll-forward, maturity, expiries.

Everything is derived from `lease_books.rows` (the monthly schedule written on every approval), so the overview,
the journals and the disclosures all come from the same numbers. Money stays Decimal until the response.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from komputy.apps.leases.models import Lease, LeaseBooks, LeaseCounterparty, LeaseEntity, LeaseVersion
from komputy.auth.deps import DbSession, Membership, require_tenant_member

router = APIRouter(prefix="/tenants/{tenant_id}/leases/views", tags=["leases"])
Member = Annotated[Membership, Depends(require_tenant_member)]
D0 = Decimal("0")
UI_STATUS = {"draft": "draft", "returned": "draft", "in_review": "submitted", "approved": "approved"}


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v)) if v not in (None, "") else D0
    except Exception:
        return D0


def _add_months(key: str, n: int) -> str:
    y, m = int(key[:4]), int(key[5:7]) + n
    y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


async def _load(
    db: Any, tenant_id: str
) -> tuple[list[Lease], dict[str, list[LeaseVersion]], dict[str, LeaseBooks], dict[str, str], LeaseEntity | None]:
    leases = (await db.execute(select(Lease).where(Lease.tenant_id == tenant_id))).scalars().all()
    versions: dict[str, list[LeaseVersion]] = defaultdict(list)
    for v in (
        await db.execute(select(LeaseVersion).where(LeaseVersion.tenant_id == tenant_id).order_by(LeaseVersion.n))
    ).scalars():
        versions[v.lease_id].append(v)
    books = {
        b.lease_id: b for b in (await db.execute(select(LeaseBooks).where(LeaseBooks.tenant_id == tenant_id))).scalars()
    }
    cps = dict(
        (
            await db.execute(
                select(LeaseCounterparty.id, LeaseCounterparty.name).where(LeaseCounterparty.tenant_id == tenant_id)
            )
        ).all()
    )
    entity = (await db.execute(select(LeaseEntity).where(LeaseEntity.tenant_id == tenant_id))).scalar_one_or_none()
    return list(leases), versions, books, cps, entity


@router.get("/portfolio")
async def portfolio(
    tenant_id: str, db: DbSession, _m: Member, period: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$")
) -> dict[str, Any]:
    leases, versions, books, cps, entity = await _load(db, tenant_id)
    key = period or date.today().strftime("%Y-%m")
    fy_start = int(getattr(entity, "fy_start_month", 1) or 1) if entity else 1
    fy_first = f"{int(key[:4]) - (1 if int(key[5:7]) < fy_start else 0):04d}-{fy_start:02d}"
    next12 = _add_months(key, 12)
    tot = defaultdict(lambda: D0)
    by_class: dict[str, Decimal] = defaultdict(lambda: D0)
    rou_class: dict[str, list[Decimal]] = defaultdict(lambda: [D0, D0])  # [ROU carrying, depreciation YTD]
    maturity = {"< 1 yr": D0, "1-2 yrs": D0, "2-5 yrs": D0, "> 5 yrs": D0}
    roll = defaultdict(lambda: D0)
    due30, due30n = D0, 0
    register = []
    for lz in leases:
        vs = versions.get(lz.id, [])
        latest = vs[-1] if vs else None
        approved = [v for v in vs if v.status == "approved"]
        terms = (approved[-1] if approved else latest).terms if (approved or latest) else {}
        b = books.get(lz.id)
        rows = {r["month"]: r for r in (b.rows if b else [])}
        cur = rows.get(key)
        liab = _d(cur["liabClosing"]) if cur else D0
        rou = _d(cur["rouClosing"]) if cur else D0
        if b and approved:
            tot["liability"] += liab
            tot["rou"] += rou
            fut = rows.get(next12)
            tot["current"] += liab - (_d(fut["liabClosing"]) if fut else D0)
            by_class[lz.asset_class] += liab
            rou_class[lz.asset_class][0] += rou
            for m, r in rows.items():
                if fy_first <= m <= key:
                    for c in ("additions", "interest", "payments", "remeasurement", "derecognised", "depreciation"):
                        roll[c] += _d(r.get(c))
                    rou_class[lz.asset_class][1] += _d(r.get("depreciation"))
                    if m == fy_first:
                        roll["opening"] += _d(r.get("liabOpening"))
                if m > key:
                    y = (int(m[:4]) - int(key[:4])) * 12 + int(m[5:7]) - int(key[5:7])
                    bucket = "< 1 yr" if y <= 12 else "1-2 yrs" if y <= 24 else "2-5 yrs" if y <= 60 else "> 5 yrs"
                    maturity[bucket] += _d(r.get("payments"))
            nxt = rows.get(_add_months(key, 1))
            if nxt and _d(nxt.get("payments")):
                due30 += _d(nxt.get("payments"))
                due30n += 1
        end = (terms or {}).get("endDate") or (lz.contract_end_date.isoformat() if lz.contract_end_date else None)
        status = UI_STATUS.get(latest.status, latest.status) if latest else "draft"
        if latest and latest.status == "approved" and latest.event not in (None, "", "initial", "new"):
            status = "modified"
        if latest and latest.event == "termination" and latest.status == "approved":
            status = "terminated"
        if status == "approved" and end and end[:7] <= _add_months(key, 6):
            status = "expiring"
        rate = (approved[-1] if approved else latest).rate if (approved or latest) else None
        register.append(
            {
                "id": lz.id,
                "code": lz.code,
                "name": lz.name,
                "counterparty": cps.get(lz.counterparty_id, ""),
                "assetClass": lz.asset_class,
                "entity": entity.name if entity else "",
                "start": (terms or {}).get("commencementDate"),
                "end": end,
                "liability": float(liab) if b else None,
                "rouAsset": float(rou) if b else None,
                "rate": float(_d((rate or {}).get("value"))) if rate else None,
                "status": status,
                "exempt": "short-term" if b and b.exempt else None,
                "createdBy": latest.created_by if latest else "",
                "currency": lz.currency,
                "verificationFailed": lz.verification_failed,
                "updatedAt": lz.updated_at.isoformat(),
            }
        )
    active = [r for r in register if r["status"] in ("approved", "modified", "expiring")]
    expiring = sorted([r for r in active if r["end"] and r["end"][:7] <= next12], key=lambda r: r["end"])
    closing = (
        roll["opening"]
        + roll["additions"]
        + roll["interest"]
        - roll["payments"]
        + roll["remeasurement"]
        - roll["derecognised"]
    )
    return {
        "data": {
            "period": key,
            "currency": entity.currency if entity else "USD",
            "register": register,
            "overview": {
                "liability": float(tot["liability"]),
                "rou": float(tot["rou"]),
                "current": float(tot["current"]),
                "nonCurrent": float(tot["liability"] - tot["current"]),
                "due30": float(due30),
                "due30Count": due30n,
                "expiring12": len(expiring),
                "active": len(active),
                "entities": 1 if entity else 0,
                "byClass": sorted([[k, float(v)] for k, v in by_class.items()], key=lambda x: -x[1]),
                "rollForward": [
                    {"label": "Opening", "value": float(roll["opening"]), "kind": "total"},
                    {"label": "Additions", "value": float(roll["additions"])},
                    {"label": "Interest", "value": float(roll["interest"])},
                    {"label": "Payments", "value": -float(roll["payments"])},
                    {"label": "Modifications", "value": float(roll["remeasurement"] - roll["derecognised"])},
                    {"label": "Closing", "value": float(closing), "kind": "total"},
                ],
                "expiries": [
                    [r["code"], r["name"], r["counterparty"], r["end"], r["liability"] or 0] for r in expiring[:10]
                ],
                "maturity": [[k, float(v)] for k, v in maturity.items()],
                "rouByClass": sorted(
                    [[k, float(v[0]), float(v[1])] for k, v in rou_class.items()], key=lambda x: -x[1]
                ),
                "depreciationYtd": float(roll["depreciation"]),
                "interestYtd": float(roll["interest"]),
                "paymentsYtd": float(roll["payments"]),
                "fyFrom": fy_first,
            },
        }
    }


@router.get("/lease/{lease_id}")
async def lease_view(tenant_id: str, lease_id: str, db: DbSession, _m: Member) -> dict[str, Any]:
    """One lease for the detail page: contract, versions, documents, the approved schedule (monthly + annual by FY)
    and its journals, all from the stored books."""
    from komputy.apps.leases.common import ser_lease
    from komputy.apps.leases.models import LeaseDocument
    from komputy.kernel.errors import ECLException
    from komputy.leases.domain.engine import group_schedule

    lz = (
        await db.execute(
            select(Lease).where(Lease.tenant_id == tenant_id, (Lease.id == lease_id) | (Lease.code == lease_id))
        )
    ).scalar_one_or_none()
    if not lz:
        raise ECLException("NOT_FOUND", "Lease not found.", 404)
    vs = (
        (await db.execute(select(LeaseVersion).where(LeaseVersion.lease_id == lz.id).order_by(LeaseVersion.n)))
        .scalars()
        .all()
    )
    docs = (await db.execute(select(LeaseDocument).where(LeaseDocument.lease_id == lz.id))).scalars().all()
    b = (await db.execute(select(LeaseBooks).where(LeaseBooks.lease_id == lz.id))).scalar_one_or_none()
    entity = (await db.execute(select(LeaseEntity).where(LeaseEntity.tenant_id == tenant_id))).scalar_one_or_none()
    cp = (await db.execute(select(LeaseCounterparty.name).where(LeaseCounterparty.id == lz.counterparty_id))).scalar()
    rows = b.rows if b else []
    return {
        "data": {
            "lease": ser_lease(lz, list(vs), list(docs)),
            "counterparty": cp,
            "entity": entity.name if entity else None,
            "exempt": bool(b.exempt) if b else None,
            "engineVersion": b.engine_version if b else None,
            "computedAt": b.computed_at.isoformat() if b else None,
            "monthly": rows,
            "annual": group_schedule([dict(r) for r in rows], "year", int(entity.fy_start_month) if entity else 1)
            if rows
            else [],
            "journals": b.journals if b else [],
        }
    }
