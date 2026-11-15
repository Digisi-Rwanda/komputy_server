"""Shared helpers for the leases services: errors, members, setup lookups, books, serialization.

Serialized objects use the exact camelCase shapes of the web app (src/lib/leases/types.ts), so the
client can render server data with its existing models.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledgerline.apps.leases.models import (
    Lease,
    LeaseBooks,
    LeaseDocument,
    LeaseEntity,
    LeasePeriod,
    LeaseSetupVersion,
    LeaseVersion,
)
from ledgerline.events.audit_service import log_event
from ledgerline.events.notify import notify as publish_notice
from ledgerline.kernel.errors import ECLException
from ledgerline.leases.domain.dates import add_days, months_between
from ledgerline.leases.domain.engine import ENGINE_VERSION, compute, exempt_expense
from ledgerline.web import TenantMembership, User

PENDING = ("draft", "in_review", "returned")


# ─── Errors (codes the web maps to its LeaseError kinds) ───────────────────


def invalid(msg: str, field: str | None = None) -> ECLException:
    return ECLException("LEASE_INVALID", msg, 422, field=field)


def conflict(msg: str) -> ECLException:
    return ECLException("LEASE_CONFLICT", msg, 409)


def forbidden(msg: str) -> ECLException:
    return ECLException("LEASE_FORBIDDEN", msg, 403)


def not_found(msg: str = "Not found.") -> ECLException:
    return ECLException("LEASE_NOT_FOUND", msg, 404)


def now() -> datetime:
    return datetime.now(UTC)


def iso(v: date | datetime | None) -> str | None:
    return v.isoformat() if v else None


def money(v: Decimal | None) -> str | None:
    return None if v is None else f"{v:.2f}"


def today() -> str:
    return date.today().isoformat()


# ─── Members (signed-in users and their roles) ───────────────────────────


async def members(db: AsyncSession, tenant_id: str) -> list[dict[str, Any]]:
    """Active members with their Lease Accounting role (from the web app's tables, read-only)."""
    from ledgerline.web import MemberServiceRole

    rows = (
        await db.execute(
            select(User.id, User.name, TenantMembership.workspace_role, MemberServiceRole.role.label("lr"))
            .join(TenantMembership, TenantMembership.user_id == User.id)
            .outerjoin(
                MemberServiceRole,
                (MemberServiceRole.membership_id == TenantMembership.id) & (MemberServiceRole.service == "leases"),
            )
            .where(TenantMembership.tenant_id == tenant_id, TenantMembership.status == "active")
        )
    ).all()
    titles = {"administrator": "Administrator", "analyst": "Preparer", "reviewer": "Reviewer", "viewer": "Viewer"}
    from ledgerline.auth.deps import LEASES_MAP

    out = []
    for r in rows:
        role = "administrator" if r.workspace_role in ("owner", "admin") else LEASES_MAP.get(r.lr or "", "viewer")
        out.append({"id": r.id, "name": r.name, "initials": "".join(w[0] for w in r.name.split()[:2]).upper() or "U",
                    "role": role, "title": titles.get(role, role)})
    return out


def role_of(m: TenantMembership) -> str:
    return m.role


def require(m: TenantMembership, *roles: str) -> None:
    if m.role not in roles:
        raise forbidden(f"Your role ({m.role}) can't do this.")


async def audit(
    db: AsyncSession,
    tenant_id: str,
    user_id: str,
    action: str,
    target: str,
    detail: str | None = None,
) -> None:
    await log_event(
        db,
        f"lease.{action}"[:50],
        user_id=user_id,
        tenant_id=tenant_id,
        details={"target": target, "detail": detail},
    )


async def notify(
    db: AsyncSession, tenant_id: str, to: str | list[str], title: str, body: str, href: str
) -> None:
    """to: a user id, a list of ids, '*' (all members) or '*reviewer' (reviewers + admins)."""
    if isinstance(to, str) and to.startswith("*"):
        ms = await members(db, tenant_id)
        ids = [m["id"] for m in ms if to == "*" or m["role"] in ("reviewer", "administrator")]
    else:
        ids = [to] if isinstance(to, str) else to
    if ids:
        await publish_notice(db, tenant_id=tenant_id, user_ids=ids, event="approvals", kind="leases", title=title,
                             body=body, href=href, action="Open")


# ─── Setup lookups ─────────────────────────────────────────────────────────


async def entity_of(db: AsyncSession, tenant_id: str) -> LeaseEntity | None:
    return (
        await db.execute(select(LeaseEntity).where(LeaseEntity.tenant_id == tenant_id))
    ).scalar_one_or_none()


async def setup_versions(
    db: AsyncSession, tenant_id: str, kind: str | None = None
) -> list[LeaseSetupVersion]:
    q = select(LeaseSetupVersion).where(LeaseSetupVersion.tenant_id == tenant_id)
    if kind:
        q = q.where(LeaseSetupVersion.kind == kind)
    return list((await db.execute(q.order_by(LeaseSetupVersion.version))).scalars())


def in_force(versions: list[LeaseSetupVersion], on: str | None = None) -> LeaseSetupVersion | None:
    """Latest approved version effective on/before the date, else the first approved one."""
    ok = [v for v in versions if v.status == "approved"]
    d = on or today()
    eff = [v for v in ok if v.effective_from.isoformat() <= d]
    return eff[-1] if eff else (ok[0] if ok else None)


async def setup_status(db: AsyncSession, tenant_id: str) -> dict[str, bool]:
    vs = await setup_versions(db, tenant_id)
    has = lambda k: any(v.kind == k and v.status == "approved" for v in vs)  # noqa: E731
    periods = (
        await db.execute(select(LeasePeriod.id).where(LeasePeriod.tenant_id == tenant_id).limit(1))
    ).first()
    return {
        "entity": (await entity_of(db, tenant_id)) is not None,
        "periods": periods is not None,
        "policy": has("policies"),
        "rates": has("curves"),
        "gl": has("gl"),
    }


async def require_setup(
    db: AsyncSession,
    tenant_id: str,
    need: tuple[str, ...] = ("entity", "periods", "policy", "rates"),
) -> None:
    s = await setup_status(db, tenant_id)
    missing = [k for k in need if not s[k]]
    if missing:
        raise invalid(f"Finish Leases setup first ({', '.join(missing)}).")


async def day_count(db: AsyncSession, tenant_id: str) -> str:
    p = in_force(await setup_versions(db, tenant_id, "policies"))
    return (p.data.get("dayCount") if p else None) or "periodic"


def curve_rate(curve: LeaseSetupVersion | None, currency: str, term_years: float) -> str | None:
    """Linear interpolation on the curve for the currency; flat beyond the ends."""
    pts = sorted(
        (p for p in (curve.data.get("points") if curve else []) or [] if p["currency"] == currency),
        key=lambda p: p["termYears"],
    )
    if not pts:
        return None
    if term_years <= pts[0]["termYears"]:
        return str(pts[0]["rate"])
    if term_years >= pts[-1]["termYears"]:
        return str(pts[-1]["rate"])
    i = next(i for i, p in enumerate(pts) if p["termYears"] >= term_years)
    a, b = pts[i - 1], pts[i]
    w = Decimal(str(term_years - a["termYears"])) / Decimal(str(b["termYears"] - a["termYears"]))
    return str(
        (
            Decimal(str(a["rate"])) + (Decimal(str(b["rate"])) - Decimal(str(a["rate"]))) * w
        ).quantize(Decimal("0.000001"))
    )


def term_years(frm: str, end_date: str) -> float:
    return round(float(months_between(frm, add_days(end_date, 1))) / 12, 2)


async def current_period(db: AsyncSession, tenant_id: str) -> LeasePeriod | None:
    return (
        await db.execute(
            select(LeasePeriod)
            .where(LeasePeriod.tenant_id == tenant_id, LeasePeriod.status != "locked")
            .order_by(LeasePeriod.key)
            .limit(1)
        )
    ).scalar_one_or_none()


async def locked_keys(db: AsyncSession, tenant_id: str) -> set[str]:
    return set(
        (
            await db.execute(
                select(LeasePeriod.key).where(
                    LeasePeriod.tenant_id == tenant_id, LeasePeriod.status == "locked"
                )
            )
        ).scalars()
    )


# ─── Versions → engine inputs, books ───────────────────────────────────────


def is_exempt(v: LeaseVersion | None) -> bool:
    return bool(v) and (v.assessment.get("exemption") or "none") not in ("none", None)


def segment_inputs(versions: list[LeaseVersion]) -> list[dict[str, Any]]:
    out = []
    for i, v in enumerate(versions):
        out.append(
            {
                "start": v.terms["commencementDate"] if i == 0 else v.effective_date.isoformat(),
                "terms": v.terms,
                "rate": (v.rate or {}).get("value") or "0",
                "event": "initial" if i == 0 else v.event,
                "scopeDecrease": str(v.scope_decrease) if v.scope_decrease is not None else None,
                "terminationPenalty": str(v.termination_penalty)
                if v.termination_penalty is not None
                else None,
            }
        )
    return out


def checksum(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


async def write_books(
    db: AsyncSession, lease: Lease, chain: list[LeaseVersion], dc: str
) -> LeaseBooks:
    """(Re)compute and store the approved chain's schedule + journals. Called on every approval."""
    inputs = segment_inputs(chain)
    cks = checksum({"inputs": inputs, "dayCount": dc, "engine": ENGINE_VERSION})
    if is_exempt(chain[0]):
        rows, journals, segs = [], exempt_expense(chain[-1].terms), []
    else:
        c = compute(inputs, dc)
        if not c.ok:
            raise invalid("The approved chain fails its integrity checks; nothing was saved.")
        rows, journals, segs = c.rows, c.journals, [s.to_json() for s in c.segments]
    books = await db.get(LeaseBooks, lease.id)
    if books is None:
        books = LeaseBooks(lease_id=lease.id, tenant_id=lease.tenant_id)
        db.add(books)
    books.chain_checksum, books.engine_version, books.exempt = (
        cks,
        ENGINE_VERSION,
        is_exempt(chain[0]),
    )
    books.rows, books.journals, books.segments = rows, journals, segs
    books.computed_at = now()
    return books


# ─── Serialization (web shapes) ────────────────────────────────────────────


def ser_version(v: LeaseVersion) -> dict[str, Any]:
    return {
        "n": v.n,
        "status": v.status,
        "event": v.event,
        "effectiveDate": iso(v.effective_date) or "",
        "reason": v.reason,
        "terms": v.terms,
        "assessment": v.assessment,
        "rate": v.rate,
        "scopeDecrease": str(v.scope_decrease) if v.scope_decrease is not None else None,
        "terminationPenalty": money(v.termination_penalty),
        "createdBy": v.created_by,
        "createdAt": iso(v.created_at),
        "submittedBy": v.submitted_by,
        "submittedAt": iso(v.submitted_at),
        "approverId": v.approver_id,
        "decidedBy": v.decided_by,
        "decidedAt": iso(v.decided_at),
        "comments": v.comments or [],
        "calculated": None
        if v.calc_at is None
        else {
            "at": iso(v.calc_at),
            "liability": money(v.calc_liability) or "",
            "rou": money(v.calc_rou) or "",
            "checksOk": bool(v.calc_checks_ok),
        },
    }


def ser_doc(d: LeaseDocument) -> dict[str, Any]:
    return {
        "id": d.id,
        "name": d.name,
        "size": d.size,
        "sha256": d.sha256,
        "kind": d.kind,
        "uploadedBy": d.uploaded_by,
        "uploadedAt": iso(d.uploaded_at),
        "versionN": d.version_n,
    }


def ser_lease(
    lease: Lease, versions: list[LeaseVersion], docs: list[LeaseDocument]
) -> dict[str, Any]:
    return {
        "id": lease.id,
        "code": lease.code,
        "name": lease.name,
        "assetClass": lease.asset_class,
        "counterpartyId": lease.counterparty_id,
        "entityId": lease.entity_id,
        "location": lease.location,
        "currency": lease.currency,
        "ownerId": lease.owner_id,
        "costCentre": lease.cost_centre,
        "contractTotal": money(lease.contract_total),
        "signatureDate": iso(lease.signature_date),
        "contractEndDate": iso(lease.contract_end_date),
        "versions": [ser_version(v) for v in sorted(versions, key=lambda x: x.n)],
        "docs": [ser_doc(d) for d in docs],
        "wizardStep": lease.wizard_step,
        "createdAt": iso(lease.created_at),
        "updatedAt": iso(lease.updated_at),
        "rev": lease.rev,
        "importBatchId": lease.import_batch_id,
        "verificationFailed": lease.verification_failed,
    }


def ser_setup(v: LeaseSetupVersion) -> dict[str, Any]:
    return {
        **v.data,
        "version": v.version,
        "effectiveFrom": v.effective_from.isoformat(),
        "status": v.status,
        "reason": v.reason,
        "createdBy": v.created_by,
        "createdAt": iso(v.created_at),
        "approvedBy": v.approved_by,
        "approvedAt": iso(v.approved_at),
    }
