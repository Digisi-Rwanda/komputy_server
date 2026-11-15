"""Leases: setup, lease lifecycle (draft → calculate → review → approve), events, documents.

Port of the web demo store (src/lib/leases/store.ts) with the same rules and messages; the server is
authoritative. One request = one transaction (DbSession commits before the response).
"""

from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ledgerline.apps.leases.common import (
    PENDING,
    audit,
    checksum,
    conflict,
    curve_rate,
    day_count,
    entity_of,
    forbidden,
    in_force,
    invalid,
    is_exempt,
    locked_keys,
    not_found,
    notify,
    now,
    require,
    require_setup,
    segment_inputs,
    ser_lease,
    setup_versions,
    term_years,
    write_books,
)
from ledgerline.apps.leases.models import (
    Lease,
    LeaseCounterparty,
    LeaseDocument,
    LeaseEntity,
    LeasePeriod,
    LeaseSetupVersion,
    LeaseVersion,
)
from ledgerline.auth.deps import Membership as TenantMembership
from ledgerline.kernel.ids import new_ulid
from ledgerline.leases.domain.dates import is_iso_date, month_key
from ledgerline.leases.domain.engine import ENGINE_VERSION, compute

ASSET_CLASSES = ("property", "vehicles", "equipment", "it", "land")
ADMIN, PREP, REV = "administrator", "analyst", "reviewer"


def D(v: Any) -> Decimal:
    try:
        return Decimal(str(v)) if v not in (None, "") else Decimal(0)
    except InvalidOperation as e:
        raise invalid(f"“{v}” isn't a number.") from e


# ─── Setup ─────────────────────────────────────────────────────────────────


async def save_entity(db: AsyncSession, m: TenantMembership, body: dict[str, Any]) -> LeaseEntity:
    require(m, ADMIN)
    name, book = (body.get("name") or "").strip(), (body.get("bookName") or "").strip()
    if not name:
        raise invalid("Entity name is required.", "name")
    if not book:
        raise invalid("Name the reporting book.", "book")
    currency = (body.get("currency") or "").upper()
    if len(currency) != 3:
        raise invalid("Pick a 3-letter currency code.", "currency")
    fy = int(body.get("fyStartMonth") or 1)
    if not 1 <= fy <= 12:
        raise invalid("Financial year must start in month 1–12.")
    e = await entity_of(db, m.tenant_id)
    has_leases = (await db.execute(select(Lease.id).where(Lease.tenant_id == m.tenant_id).limit(1))).first() is not None
    if e is None:
        e = LeaseEntity(id=new_ulid(), tenant_id=m.tenant_id, currency=currency, fy_start_month=fy)
        db.add(e)
    elif has_leases and (e.currency != currency or e.fy_start_month != fy):
        raise invalid("Currency and financial year are locked once leases exist.")
    e.name, e.country, e.book_name, e.currency, e.fy_start_month = (
        name,
        body.get("country") or "",
        book,
        currency,
        fy,
    )
    await audit(db, m.tenant_id, m.user_id, "setup.entity", name)
    return e


async def open_first_period(db: AsyncSession, m: TenantMembership, key: str) -> LeasePeriod:
    require(m, ADMIN)
    if (await db.execute(select(LeasePeriod.id).where(LeasePeriod.tenant_id == m.tenant_id).limit(1))).first():
        raise invalid("Periods already exist.")
    if not (isinstance(key, str) and len(key) == 7 and is_iso_date(key + "-01")):
        raise invalid("Pick a valid month.")
    p = LeasePeriod(id=new_ulid(), tenant_id=m.tenant_id, key=key, status="open", reopen_log=[])
    db.add(p)
    await audit(db, m.tenant_id, m.user_id, "setup.period", key)
    return p


def _validate_setup(kind: str, data: dict[str, Any]) -> dict[str, Any]:
    if kind == "policies":
        dc = data.get("dayCount") or "periodic"
        if dc not in ("periodic", "act365"):
            raise invalid("Interest convention must be periodic or act365.")
        for k in ("shortTermClasses", "separateNonLeaseClasses"):
            if any(c not in ASSET_CLASSES for c in data.get(k) or []):
                raise invalid("Unknown asset class in policy.")
        return {
            "dayCount": dc,
            "shortTermClasses": data.get("shortTermClasses") or [],
            "lowValueElection": bool(data.get("lowValueElection")),
            "lowValueThreshold": str(D(data.get("lowValueThreshold"))),
            "separateNonLeaseClasses": data.get("separateNonLeaseClasses") or [],
            "capitalisationThreshold": str(D(data.get("capitalisationThreshold"))),
        }
    if kind == "curves":
        if not (data.get("source") or "").strip():
            raise invalid("Say where the rates come from (evidence).", "source")
        pts = data.get("points") or []
        if not pts:
            raise invalid("Add at least one term.")
        seen = set()
        for p in pts:
            r = D(p.get("rate"))
            if r <= 0 or r > Decimal("0.4"):
                raise invalid(f"{p.get('termYears')}-year rate must be between 0% and 40%.")
            key = (p.get("currency"), p.get("termYears"))
            if key in seen:
                raise invalid("Each term can appear once.")
            seen.add(key)
        return {
            "source": data["source"].strip(),
            "points": sorted(
                (
                    {
                        "currency": p["currency"],
                        "termYears": p["termYears"],
                        "rate": str(D(p["rate"])),
                    }
                    for p in pts
                ),
                key=lambda p: (p["currency"], p["termYears"]),
            ),
        }
    if kind == "gl":
        acc = data.get("accounts") or {}
        return {"accounts": {k: str(v).strip() for k, v in acc.items()}}
    raise not_found()


async def submit_setup(
    db: AsyncSession,
    m: TenantMembership,
    kind: str,
    data: dict[str, Any],
    effective_from: str,
    reason: str,
) -> int:
    require(m, ADMIN)
    if not is_iso_date(effective_from):
        raise invalid("Effective date is required.", "effectiveFrom")
    clean = _validate_setup(kind, data)
    vs = await setup_versions(db, m.tenant_id, kind)
    if vs and not (reason or "").strip():
        raise invalid("Give a reason for the change.", "reason")
    if any(v.status == "in_review" for v in vs):
        raise conflict("Another change is waiting for approval. Approve or withdraw it first.")
    first = not vs
    v = LeaseSetupVersion(
        id=new_ulid(),
        tenant_id=m.tenant_id,
        kind=kind,
        version=(vs[-1].version if vs else 0) + 1,
        effective_from=date.fromisoformat(effective_from),
        status="approved" if first else "in_review",
        reason=reason or "Initial setup",
        data=clean,
        created_by=m.user_id,
        approved_by=m.user_id if first else None,
        approved_at=now() if first else None,
    )
    db.add(v)
    await audit(db, m.tenant_id, m.user_id, f"setup.{kind}.submit", f"v{v.version}", reason)
    if not first:
        label = {"gl": "GL mapping", "curves": "Discount-rate curve", "policies": "Policy"}[kind]
        await notify(
            db,
            m.tenant_id,
            "*reviewer",
            f"{label} v{v.version} needs approval",
            reason,
            "/leases/inbox",
        )
    return v.version


async def decide_setup(
    db: AsyncSession, m: TenantMembership, kind: str, version: int, approve: bool, comment: str = ""
) -> None:
    require(m, REV, ADMIN)
    v = (
        await db.execute(
            select(LeaseSetupVersion)
            .where(
                LeaseSetupVersion.tenant_id == m.tenant_id,
                LeaseSetupVersion.kind == kind,
                LeaseSetupVersion.version == version,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if v is None or v.status != "in_review":
        raise conflict("This change is no longer waiting for approval.")
    if v.created_by == m.user_id:
        raise forbidden("You can't approve your own change.")
    if approve:
        v.status, v.approved_by, v.approved_at = "approved", m.user_id, now()
    else:
        await db.delete(v)
    await audit(
        db,
        m.tenant_id,
        m.user_id,
        f"setup.{kind}.{'approve' if approve else 'reject'}",
        f"v{version}",
        comment,
    )
    await notify(
        db,
        m.tenant_id,
        v.created_by,
        f"{'Approved' if approve else 'Rejected'}: {kind} v{version}",
        comment,
        "/leases/setup",
    )


# ─── Leases: load / create / autosave ──────────────────────────────────────


async def load(db: AsyncSession, tenant_id: str, lease_id: str, lock: bool = False) -> tuple[Lease, list[LeaseVersion]]:
    q = select(Lease).where(Lease.tenant_id == tenant_id, Lease.id == lease_id)
    lease = (await db.execute(q.with_for_update() if lock else q)).scalar_one_or_none()
    if lease is None:
        raise not_found("Lease not found.")
    vs = list(
        (
            await db.execute(select(LeaseVersion).where(LeaseVersion.lease_id == lease.id).order_by(LeaseVersion.n))
        ).scalars()
    )
    return lease, vs


def chain_of(vs: list[LeaseVersion]) -> list[LeaseVersion]:
    return [v for v in vs if v.status == "approved"]


def pending_of(vs: list[LeaseVersion]) -> LeaseVersion | None:
    return next((v for v in vs if v.status in PENDING), None)


def touch(lease: Lease, rev: int | None) -> None:
    if rev is not None and rev != lease.rev:
        raise conflict("Someone else changed this lease while you were editing. Reload to see their changes.")
    lease.rev += 1
    lease.updated_at = now()


async def serialize(db: AsyncSession, lease: Lease, vs: list[LeaseVersion] | None = None) -> dict[str, Any]:
    if vs is None:
        vs = list((await db.execute(select(LeaseVersion).where(LeaseVersion.lease_id == lease.id))).scalars())
    docs = list(
        (
            await db.execute(
                select(LeaseDocument).where(LeaseDocument.lease_id == lease.id).order_by(LeaseDocument.uploaded_at)
            )
        ).scalars()
    )
    return ser_lease(lease, vs, docs)


def blank_terms() -> dict[str, Any]:
    return {
        "commencementDate": "",
        "endDate": "",
        "manualLines": [],
        "nonLeasePerPayment": "0",
        "separateNonLease": True,
        "prepaidBeforeCommencement": "0",
        "initialDirectCosts": "0",
        "incentivesReceived": "0",
        "restorationEstimate": "0",
        "paymentRule": {
            "amount": "",
            "frequency": "monthly",
            "timing": "arrears",
            "escalation": {"type": "none"},
            "rentFreeMonths": 0,
        },
    }


def blank_assessment() -> dict[str, Any]:
    return {
        "identifiedAsset": None,
        "rightToControl": None,
        "exemption": None,
        "exemptionRationale": "",
        "options": [],
        "purchaseOption": {"exists": False, "reasonablyCertain": None, "rationale": ""},
        "termRationale": "",
    }


async def create_draft(db: AsyncSession, m: TenantMembership, body: dict[str, Any]) -> Lease:
    require(m, ADMIN, PREP)
    await require_setup(db, m.tenant_id)
    e = (
        await db.execute(select(LeaseEntity).where(LeaseEntity.tenant_id == m.tenant_id).with_for_update())
    ).scalar_one()
    if body.get("assetClass", "property") not in ASSET_CLASSES:
        raise invalid("Unknown asset class.", "assetClass")
    e.lease_seq += 1
    lease = Lease(
        id=new_ulid(),
        tenant_id=m.tenant_id,
        code=f"L-{e.lease_seq:04d}",
        name=(body.get("name") or "").strip(),
        asset_class=body.get("assetClass") or "property",
        counterparty_id=body.get("counterpartyId") or "",
        entity_id=e.id,
        location=body.get("location") or "",
        currency=e.currency,
        owner_id=m.user_id,
        cost_centre="",
        wizard_step=0,
        rev=1,
    )
    db.add(lease)
    await db.flush()
    db.add(
        LeaseVersion(
            id=new_ulid(),
            tenant_id=m.tenant_id,
            lease_id=lease.id,
            n=1,
            status="draft",
            event="initial",
            reason="New lease",
            terms=blank_terms(),
            assessment=blank_assessment(),
            rate={"value": "", "source": "curve"},
            comments=[],
            created_by=m.user_id,
        )
    )
    await audit(db, m.tenant_id, m.user_id, "lease.create", lease.code)
    return lease


LEASE_FIELDS = {
    "name": "name",
    "assetClass": "asset_class",
    "counterpartyId": "counterparty_id",
    "location": "location",
    "ownerId": "owner_id",
    "costCentre": "cost_centre",
    "contractTotal": "contract_total",
    "signatureDate": "signature_date",
    "contractEndDate": "contract_end_date",
    "wizardStep": "wizard_step",
}
VERSION_INPUTS = (
    "terms",
    "assessment",
    "rate",
    "effectiveDate",
    "scopeDecrease",
    "terminationPenalty",
)


def _coerce(attr: str, v: Any) -> Any:
    if attr in ("signature_date", "contract_end_date"):
        if not v:
            return None
        if not is_iso_date(v):
            raise invalid(f"“{v}” isn't a valid date.")
        return date.fromisoformat(v)
    if attr == "contract_total":
        return D(v) if v not in (None, "") else None
    if attr == "wizard_step":
        return max(0, min(4, int(v)))
    if attr == "asset_class" and v not in ASSET_CLASSES:
        raise invalid("Unknown asset class.")
    return v if v is not None else ""


def _version_value(v: LeaseVersion, key: str) -> Any:
    return {
        "terms": v.terms,
        "assessment": v.assessment,
        "rate": v.rate,
        "effectiveDate": v.effective_date.isoformat() if v.effective_date else "",
        "scopeDecrease": str(v.scope_decrease) if v.scope_decrease is not None else None,
        "terminationPenalty": str(v.termination_penalty) if v.termination_penalty is not None else None,
    }[key]


async def update_draft(db: AsyncSession, m: TenantMembership, lease_id: str, rev: int, patch: dict[str, Any]) -> int:
    lease, vs = await load(db, m.tenant_id, lease_id, lock=True)
    v = pending_of(vs)
    if v is None or v.status == "in_review":
        raise conflict("This version is in review and can't be edited. Withdraw it first.")
    if v.created_by != m.user_id and m.role != ADMIN:
        raise forbidden("Only the preparer can edit this draft.")
    touch(lease, rev)
    for k, val in (patch.get("lease") or {}).items():
        if k in LEASE_FIELDS:
            setattr(lease, LEASE_FIELDS[k], _coerce(LEASE_FIELDS[k], val))
    pv = patch.get("version") or {}
    changed = any(k in pv and pv[k] != _version_value(v, k) for k in VERSION_INPUTS)
    if "terms" in pv:
        v.terms = pv["terms"]
    if "assessment" in pv:
        v.assessment = pv["assessment"]
    if "rate" in pv:
        v.rate = pv["rate"]
    if "reason" in pv:
        v.reason = pv["reason"] or ""
    if "effectiveDate" in pv and v.event != "initial":
        v.effective_date = date.fromisoformat(pv["effectiveDate"]) if is_iso_date(pv["effectiveDate"]) else None
    if "scopeDecrease" in pv:
        v.scope_decrease = D(pv["scopeDecrease"]) if pv["scopeDecrease"] not in (None, "") else None
    if "terminationPenalty" in pv:
        v.termination_penalty = D(pv["terminationPenalty"]) if pv["terminationPenalty"] not in (None, "") else None
    if changed:  # unchanged autosaves keep the calculation (fixes the "submit after autosave" bug)
        v.calc_at = v.calc_liability = v.calc_rou = v.calc_checks_ok = v.input_checksum = None
    if v.event == "initial" and is_iso_date(v.terms.get("commencementDate")):
        v.effective_date = date.fromisoformat(v.terms["commencementDate"])
    if v.status == "returned":
        v.status = "draft"
    return lease.rev


# ─── Validation (same keys/messages as the web wizard) ─────────────────────


async def validate_version(db: AsyncSession, lease: Lease, v: LeaseVersion, vs: list[LeaseVersion]) -> dict[str, str]:
    e: dict[str, str] = {}
    t = v.terms or {}
    ent = await entity_of(db, lease.tenant_id)
    if not (lease.name or "").strip():
        e["name"] = "Give the lease a name."
    if not lease.counterparty_id:
        e["counterpartyId"] = "Choose the lessor."
    cd, ed = t.get("commencementDate"), t.get("endDate")
    if not is_iso_date(cd):
        e["commencementDate"] = "Commencement date is required."
    if not is_iso_date(ed):
        e["endDate"] = "End date is required."
    if is_iso_date(cd) and is_iso_date(ed) and ed <= cd:
        e["endDate"] = "End date must be after commencement."
    if ent and lease.currency != ent.currency:
        e["currency"] = f"Foreign-currency leases aren't supported yet. Use {ent.currency}."
    r = t.get("paymentRule")
    if (not r or not r.get("amount")) and not t.get("manualLines"):
        e["amount"] = "Add the payment amount or at least one payment line."
    if r and r.get("amount") and D(r["amount"]) <= 0:
        e["amount"] = "Payment must be more than zero."
    if r:
        if r.get("frequency") not in ("monthly", "quarterly", "semiannual", "annual"):
            e["amount"] = "Unknown payment frequency."
        if r.get("timing") not in ("advance", "arrears"):
            e["amount"] = "Unknown payment timing."
        if int(r.get("rentFreeMonths") or 0) < 0:
            e["rentFreeMonths"] = "Can't be negative."
    for i, ml in enumerate(t.get("manualLines") or []):
        if not is_iso_date(ml.get("date")):
            e[f"line{i}"] = f"Line {i + 1}: date is required."
        if not ml.get("amount") or D(ml["amount"]) == 0:
            e[f"line{i}"] = f"Line {i + 1}: amount is required."
    exempt = is_exempt(v)
    rate = v.rate or {}
    if not exempt:
        if not rate.get("value"):
            e["rate"] = "Discount rate is required."
        elif D(rate["value"]) <= 0 or D(rate["value"]) > Decimal("0.4"):
            e["rate"] = "Rate must be between 0% and 40%."
        if rate.get("source") in ("override", "implicit") and not (rate.get("reason") or "").strip():
            e["rateReason"] = "Explain why the curve rate isn't used."
    a = v.assessment or {}
    if v.event == "initial":
        if a.get("identifiedAsset") is None:
            e["identifiedAsset"] = "Answer this question."
        if a.get("rightToControl") is None:
            e["rightToControl"] = "Answer this question."
        if a.get("identifiedAsset") is False or a.get("rightToControl") is False:
            e["notLease"] = "This contract doesn't contain a lease. Discard the draft or change the answers."
        if a.get("exemption") is None:
            e["exemption"] = "Answer this question."
        if exempt and not (a.get("exemptionRationale") or "").strip():
            e["exemptionRationale"] = "Explain why the exemption applies."
        for i, o in enumerate(a.get("options") or []):
            if o.get("reasonablyCertain") is None:
                e[f"option{i}"] = "Say whether this option is reasonably certain."
            elif not (o.get("rationale") or "").strip():
                e[f"option{i}"] = "Give a rationale."
        po = a.get("purchaseOption") or {}
        if po.get("exists") and po.get("reasonablyCertain") is None:
            e["purchaseOption"] = "Say whether the purchase option is reasonably certain."
    else:
        ch = chain_of(vs)
        prev = ch[-1] if ch else None
        eff = v.effective_date.isoformat() if v.effective_date else ""
        if not eff:
            e["effectiveDate"] = "Effective date is required."
        elif prev:
            prev_start = prev.effective_date.isoformat() if len(ch) > 1 else prev.terms["commencementDate"]
            if eff <= prev_start:
                e["effectiveDate"] = "Must be after the previous version's effective date."
            if eff > prev.terms["endDate"]:
                e["effectiveDate"] = "Must be within the lease term."
        if not (v.reason or "").strip():
            e["reason"] = "Describe the change."
        if v.scope_decrease is not None and not (0 < v.scope_decrease < 1):
            e["scopeDecrease"] = "Enter a share between 0% and 100%."
    return e


async def _raise_first(errs: dict[str, str]) -> None:
    if errs:
        k, msg = next(iter(errs.items()))
        raise invalid(msg, k)


def _computation(versions: list[LeaseVersion], dc: str):
    return compute(segment_inputs(versions), dc)


async def calculate(db: AsyncSession, m: TenantMembership, lease_id: str, rev: int) -> dict[str, Any]:
    lease, vs = await load(db, m.tenant_id, lease_id, lock=True)
    v = pending_of(vs)
    if v is None:
        raise invalid("Nothing to calculate.")
    await _raise_first(await validate_version(db, lease, v, vs))
    touch(lease, rev)
    dc = await day_count(db, m.tenant_id)
    if is_exempt(v):
        v.calc_liability = v.calc_rou = Decimal(0)
        v.calc_checks_ok, v.calc_at = True, now()
        v.input_checksum, v.engine_version = (
            checksum({"v": v.terms, "a": v.assessment}),
            ENGINE_VERSION,
        )
        return {"rev": lease.rev, "computation": None}
    versions = [v] if v.event == "initial" else [*chain_of(vs), v]
    c = _computation(versions, dc)
    seg = c.segments[-1]
    v.calc_liability, v.calc_rou = (
        seg.L0.quantize(Decimal("0.01")),
        seg.R0.quantize(Decimal("0.01")),
    )
    v.calc_checks_ok, v.calc_at = c.ok, now()
    v.input_checksum = checksum({"inputs": segment_inputs(versions), "dayCount": dc})
    v.engine_version = ENGINE_VERSION
    return {"rev": lease.rev, "computation": c.to_json()}


async def _member_ids(db: AsyncSession, tenant_id: str, roles: tuple[str, ...]) -> set[str]:
    """Active members whose Lease Accounting role (legacy vocabulary) is one of `roles`."""
    from ledgerline.apps.leases.common import members

    return {m["id"] for m in await members(db, tenant_id) if m["role"] in roles}


async def submit(
    db: AsyncSession, m: TenantMembership, lease_id: str, rev: int, approver_id: str, note: str = ""
) -> int:
    require(m, ADMIN, PREP)
    lease, vs = await load(db, m.tenant_id, lease_id, lock=True)
    v = pending_of(vs)
    if v is None or v.status not in ("draft", "returned"):
        raise invalid("There's no draft to submit.")
    if v.calc_at is None:
        raise invalid("Calculate before submitting.")
    if not v.calc_checks_ok:
        raise invalid("Integrity checks must pass before submitting.")
    if approver_id == m.user_id:
        raise forbidden("You can't approve your own lease. Choose someone else.")
    if approver_id not in await _member_ids(db, m.tenant_id, (REV, ADMIN)):
        raise invalid("Choose a reviewer or administrator.", "approver")
    touch(lease, rev)
    v.status, v.submitted_by, v.submitted_at, v.approver_id = (
        "in_review",
        m.user_id,
        now(),
        approver_id,
    )
    v.comments = [
        *v.comments,
        {
            "id": new_ulid(),
            "by": m.user_id,
            "at": now().isoformat(),
            "text": note or "Submitted for review",
            "kind": "submit",
        },
    ]
    await audit(db, m.tenant_id, m.user_id, "lease.submit", f"{lease.code} v{v.n}")
    await notify(
        db,
        m.tenant_id,
        approver_id,
        f"{lease.code} needs your review",
        f"{lease.name} · v{v.n} {'new lease' if v.event == 'initial' else v.event}",
        f"/leases/{lease.id}",
    )
    return lease.rev


async def withdraw(db: AsyncSession, m: TenantMembership, lease_id: str, rev: int) -> int:
    lease, vs = await load(db, m.tenant_id, lease_id, lock=True)
    v = pending_of(vs)
    if v is None or v.status != "in_review":
        raise invalid("Nothing in review to withdraw.")
    if v.submitted_by != m.user_id and m.role != ADMIN:
        raise forbidden("Only the submitter can withdraw.")
    touch(lease, rev)
    v.status = "draft"
    v.comments = [
        *v.comments,
        {
            "id": new_ulid(),
            "by": m.user_id,
            "at": now().isoformat(),
            "text": "Withdrawn from review",
            "kind": "withdraw",
        },
    ]
    await audit(db, m.tenant_id, m.user_id, "lease.withdraw", f"{lease.code} v{v.n}")
    return lease.rev


async def decide(
    db: AsyncSession,
    m: TenantMembership,
    lease_id: str,
    rev: int,
    approve: bool,
    text: str,
    field: str | None = None,
) -> int:
    require(m, ADMIN, REV)
    lease, vs = await load(db, m.tenant_id, lease_id, lock=True)
    v = pending_of(vs)
    if v is None or v.status != "in_review":
        raise conflict("This version is no longer in review.")
    if m.user_id in (v.submitted_by, v.created_by):
        raise forbidden("You prepared this version, so you can't approve or return it.")
    if not approve and not (text or "").strip():
        raise invalid("Say what needs to change.", "comment")
    touch(lease, rev)
    v.decided_by, v.decided_at = m.user_id, now()
    v.comments = [
        *v.comments,
        {
            "id": new_ulid(),
            "by": m.user_id,
            "at": now().isoformat(),
            "text": text or "Approved",
            "field": field,
            "kind": "approve" if approve else "return",
        },
    ]
    if approve:
        if v.event == "initial":
            v.effective_date = date.fromisoformat(v.terms["commencementDate"])
        v.status = "approved"
        # recompute server-side from stored inputs; never trust the stored calc for the books
        await write_books(db, lease, [*chain_of(vs)], await day_count(db, m.tenant_id))
    else:
        v.status = "returned"
    await audit(
        db,
        m.tenant_id,
        m.user_id,
        "lease.approve" if approve else "lease.return",
        f"{lease.code} v{v.n}",
        text,
    )
    await notify(
        db,
        m.tenant_id,
        v.submitted_by or v.created_by,
        f"{lease.code} {'approved' if approve else 'returned'}",
        f"v{v.n} is now on the books." if approve else text,
        f"/leases/{lease.id}",
    )
    return lease.rev


async def discard(db: AsyncSession, m: TenantMembership, lease_id: str) -> None:
    lease, vs = await load(db, m.tenant_id, lease_id, lock=True)
    v = pending_of(vs)
    if v is None or v.status == "in_review":
        raise invalid("Only drafts can be discarded.")
    if v.created_by != m.user_id and m.role != ADMIN:
        raise forbidden("Only the preparer can discard this draft.")
    if chain_of(vs):
        await db.delete(v)
        touch(lease, None)
        await audit(db, m.tenant_id, m.user_id, "lease.discard_change", f"{lease.code} v{v.n}")
    else:
        await db.delete(lease)
        await audit(db, m.tenant_id, m.user_id, "lease.discard", lease.code)


# ─── Events ────────────────────────────────────────────────────────────────


def _event_version(lease: Lease, vs: list[LeaseVersion], ev: dict[str, Any], user_id: str) -> LeaseVersion:
    ch = chain_of(vs)
    last = ch[-1]
    terms = copy.deepcopy(last.terms)
    eff = ev.get("effectiveDate") or ""
    if not is_iso_date(eff):
        raise invalid("Choose the effective date.", "effectiveDate")
    if ev.get("newAmount") and terms.get("paymentRule"):
        steps = [s for s in terms["paymentRule"].get("amountSteps") or [] if s["from"] < eff]
        terms["paymentRule"]["amountSteps"] = [
            *steps,
            {"from": eff, "amount": str(D(ev["newAmount"]))},
        ]
    if ev.get("newEndDate"):
        if not is_iso_date(ev["newEndDate"]):
            raise invalid("Enter a valid end date.", "endDate")
        terms["endDate"] = ev["newEndDate"]
    if ev.get("event") not in ("index", "term", "modification", "termination"):
        raise invalid("Unknown event type.")
    rate = ev.get("rate") or {}
    if ev["event"] in ("index", "termination"):
        rate = last.rate  # index changes keep the original rate (IFRS 16.42(b)); termination needs none
    return LeaseVersion(
        id=new_ulid(),
        tenant_id=lease.tenant_id,
        lease_id=lease.id,
        n=len(vs) + 1,
        status="draft",
        event=ev["event"],
        effective_date=date.fromisoformat(eff),
        reason=(ev.get("reason") or "").strip(),
        terms=terms,
        assessment=copy.deepcopy(last.assessment),
        rate=rate,
        scope_decrease=D(ev["scopeDecrease"]) if ev.get("scopeDecrease") else None,
        termination_penalty=D(ev.get("terminationPenalty")) if ev["event"] == "termination" else None,
        comments=[],
        created_by=user_id,
    )


async def preview_event(db: AsyncSession, m: TenantMembership, lease_id: str, ev: dict[str, Any]) -> dict[str, Any]:
    lease, vs = await load(db, m.tenant_id, lease_id)
    ch = chain_of(vs)
    if not ch or is_exempt(ch[0]):
        raise invalid("Events apply to capitalised leases only.")
    v = _event_version(lease, vs, ev, m.user_id)
    await _raise_first(await validate_version(db, lease, v, vs))
    c = _computation([*ch, v], await day_count(db, m.tenant_id))
    month = month_key(v.effective_date.isoformat())
    return {
        "segment": c.segments[-1].to_json(),
        "ok": c.ok,
        "journals": [j for j in c.journals if j["month"] == month and j["kind"] in ("remeasurement", "derecognition")],
    }


async def start_event(db: AsyncSession, m: TenantMembership, lease_id: str, rev: int, ev: dict[str, Any]) -> int:
    require(m, ADMIN, PREP)
    lease, vs = await load(db, m.tenant_id, lease_id, lock=True)
    if pending_of(vs):
        raise conflict("A change is already pending on this lease. Approve or withdraw it first.")
    ch = chain_of(vs)
    if not ch or is_exempt(ch[0]):
        raise invalid("Events apply to capitalised leases only.")
    touch(lease, rev)
    v = _event_version(lease, vs, ev, m.user_id)
    await _raise_first(await validate_version(db, lease, v, vs))
    dc = await day_count(db, m.tenant_id)
    c = _computation([*ch, v], dc)
    seg = c.segments[-1]
    v.calc_liability, v.calc_rou, v.calc_checks_ok, v.calc_at = (
        seg.L0.quantize(Decimal("0.01")),
        seg.R0.quantize(Decimal("0.01")),
        c.ok,
        now(),
    )
    v.input_checksum, v.engine_version = (
        checksum({"inputs": segment_inputs([*ch, v]), "dayCount": dc}),
        ENGINE_VERSION,
    )
    db.add(v)
    await db.flush()
    await audit(db, m.tenant_id, m.user_id, "lease.event", f"{lease.code} v{v.n}", v.event)
    return lease.rev


# ─── Counterparties, duplicates, documents ─────────────────────────────────


async def add_counterparty(db: AsyncSession, m: TenantMembership, name: str) -> LeaseCounterparty:
    n = (name or "").strip()
    if not n:
        raise invalid("Name is required.", "name")
    ex = (
        await db.execute(
            select(LeaseCounterparty).where(
                LeaseCounterparty.tenant_id == m.tenant_id,
                func.lower(LeaseCounterparty.name) == n.lower(),
            )
        )
    ).scalar_one_or_none()
    if ex:
        return ex
    cp = LeaseCounterparty(id=new_ulid(), tenant_id=m.tenant_id, name=n)
    db.add(cp)
    return cp


async def duplicates(db: AsyncSession, tenant_id: str, lease_id: str) -> list[dict[str, str]]:
    lease, vs = await load(db, tenant_id, lease_id)
    v = pending_of(vs) or vs[0]
    a, b = v.terms.get("commencementDate"), v.terms.get("endDate")
    if not (a and b):
        return []
    others = list(
        (
            await db.execute(
                select(Lease).where(
                    Lease.tenant_id == tenant_id,
                    Lease.id != lease.id,
                    Lease.counterparty_id == lease.counterparty_id,
                    Lease.location == lease.location,
                )
            )
        ).scalars()
    )
    out = []
    for o in others:
        _, ovs = await load(db, tenant_id, o.id)
        ov = (chain_of(ovs) or ovs)[0]
        if ov.terms.get("commencementDate", "") <= b and ov.terms.get("endDate", "") >= a:
            out.append({"id": o.id, "code": o.code, "name": o.name})
    return out


def locked_commencement(keys: set[str], terms: dict[str, Any]) -> bool:
    cd = terms.get("commencementDate") or ""
    return bool(cd) and cd[:7] in keys


async def lessor_names(db: AsyncSession, tenant_id: str) -> dict[str, str]:
    return dict(
        (
            await db.execute(
                select(LeaseCounterparty.id, LeaseCounterparty.name).where(LeaseCounterparty.tenant_id == tenant_id)
            )
        ).all()
    )


async def is_locked_date(db: AsyncSession, tenant_id: str, d: str) -> bool:
    return d[:7] in await locked_keys(db, tenant_id)


async def curve_rate_for(db: AsyncSession, tenant_id: str, currency: str, start: str, end: str) -> dict[str, Any]:
    curve = in_force(await setup_versions(db, tenant_id, "curves"), start)
    yrs = term_years(start, end)
    return {
        "value": curve_rate(curve, currency, yrs) or "",
        "source": "curve",
        "curveVersion": curve.version if curve else None,
        "termYears": yrs,
    }
