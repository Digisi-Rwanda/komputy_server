"""/api/v1/tenants/{tenant_id}/leases/* — IFRS 16 module."""

from __future__ import annotations

import csv
import hashlib
import io
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, File, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select, update

from ledgerline.apps.leases import close as close_svc
from ledgerline.apps.leases import imports as imp_svc
from ledgerline.apps.leases import service as svc
from ledgerline.apps.leases.common import (
    conflict,
    entity_of,
    invalid,
    iso,
    members,
    not_found,
    require,
    ser_lease,
    ser_setup,
    setup_status,
    setup_versions,
)
from ledgerline.apps.leases.models import (
    Lease,
    LeaseActualPayment,
    LeaseCloseRun,
    LeaseCounterparty,
    LeaseDocument,
    LeaseImportBatch,
    LeaseJournalBatch,
    LeasePeriod,
    LeasePostedBalance,
    LeaseSavedView,
    LeaseVersion,
)
from ledgerline.auth.deps import DbSession, require_tenant_member
from ledgerline.auth.deps import Membership as TenantMembership
from ledgerline.events.audit_models import AuditLog
from ledgerline.kernel.ids import new_ulid
from ledgerline.storage import delete_object, presign_download, upload_stream
from ledgerline.web import UserNotification

router = APIRouter(prefix="/tenants/{tenant_id}/leases", tags=["leases"])
Member = Depends(require_tenant_member)
Json = Annotated[dict, Body()]
OptJson = Annotated[dict, Body(default_factory=dict)]
Upload = Annotated[UploadFile, File()]
DOC_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/webp"}
MAX_DOC = 25 * 1024 * 1024


def ok(data: Any = None) -> dict[str, Any]:
    return {"data": data}


# ─── Snapshot (web LeasesState shape) ──────────────────────────────────────


@router.get("/state")
async def state(tenant_id: str, db: DbSession, m: TenantMembership = Member) -> dict[str, Any]:
    e = await entity_of(db, tenant_id)
    sv = await setup_versions(db, tenant_id)
    leases = list(
        (
            await db.execute(
                select(Lease).where(Lease.tenant_id == tenant_id).order_by(Lease.updated_at.desc())
            )
        ).scalars()
    )
    versions: dict[str, list[LeaseVersion]] = {}
    for v in (
        await db.execute(select(LeaseVersion).where(LeaseVersion.tenant_id == tenant_id))
    ).scalars():
        versions.setdefault(v.lease_id, []).append(v)
    docs: dict[str, list[LeaseDocument]] = {}
    for d in (
        await db.execute(
            select(LeaseDocument)
            .where(LeaseDocument.tenant_id == tenant_id)
            .order_by(LeaseDocument.uploaded_at)
        )
    ).scalars():
        docs.setdefault(d.lease_id, []).append(d)
    batches = list(
        (
            await db.execute(
                select(LeaseJournalBatch)
                .where(LeaseJournalBatch.tenant_id == tenant_id)
                .order_by(LeaseJournalBatch.created_at.desc())
            )
        ).scalars()
    )
    by_id = {b.id: b.code for b in batches}
    closes = []
    for c in (
        await db.execute(
            select(LeaseCloseRun)
            .where(LeaseCloseRun.tenant_id == tenant_id)
            .order_by(LeaseCloseRun.period)
        )
    ).scalars():
        sc = await close_svc.ser_close(db, c)
        sc["batchId"] = by_id.get(c.batch_id) if c.batch_id else None
        closes.append(sc)
    ser_batches = []
    for b in batches:
        sb = await close_svc.ser_batch(db, b)
        sb["id"], sb["reversalOf"] = b.code, by_id.get(b.reversal_of) if b.reversal_of else None
        ser_batches.append(sb)
    posted: dict[str, dict[str, str]] = {}
    for p in (
        await db.execute(
            select(LeasePostedBalance).where(LeasePostedBalance.tenant_id == tenant_id)
        )
    ).scalars():
        posted.setdefault(p.lease_id, {})[p.account] = f"{p.amount:.2f}"
    notes = (
        await db.execute(
            select(UserNotification)
            .where(
                UserNotification.tenant_id == tenant_id,
                UserNotification.user_id == m.user_id,
                UserNotification.kind == "leases",
            )
            .order_by(UserNotification.created_at.desc())
            .limit(100)
        )
    ).scalars()
    audit_rows = (
        await db.execute(
            select(AuditLog)
            .where(AuditLog.tenant_id == tenant_id, AuditLog.event_type.like("lease.%"))
            .order_by(AuditLog.created_at.desc())
            .limit(1000)
        )
    ).scalars()
    return ok(
        {
            "schema": 1,
            "tenantId": tenant_id,
            "meId": m.user_id,
            "members": await members(db, tenant_id),
            "role": m.role,
            "setup": await setup_status(db, tenant_id),
            "entity": e
            and {
                "id": e.id,
                "name": e.name,
                "currency": e.currency,
                "country": e.country,
                "fyStartMonth": e.fy_start_month,
            },
            "book": e
            and {
                "id": e.id,
                "entityId": e.id,
                "name": e.book_name,
                "framework": e.framework,
                "standardVersion": e.standard_version,
            },
            "periods": [
                {
                    "key": p.key,
                    "status": p.status,
                    "lockedBy": p.locked_by,
                    "lockedAt": iso(p.locked_at),
                    "reopenLog": p.reopen_log,
                }
                for p in (
                    await db.execute(
                        select(LeasePeriod)
                        .where(LeasePeriod.tenant_id == tenant_id)
                        .order_by(LeasePeriod.key)
                    )
                ).scalars()
            ],
            "policies": [ser_setup(v) for v in sv if v.kind == "policies"],
            "curves": [ser_setup(v) for v in sv if v.kind == "curves"],
            "gl": [ser_setup(v) for v in sv if v.kind == "gl"],
            "counterparties": [
                {"id": c.id, "name": c.name, "vendorRef": c.vendor_ref}
                for c in (
                    await db.execute(
                        select(LeaseCounterparty)
                        .where(LeaseCounterparty.tenant_id == tenant_id)
                        .order_by(LeaseCounterparty.name)
                    )
                ).scalars()
            ],
            "leases": [
                ser_lease(lz, versions.get(lz.id, []), docs.get(lz.id, [])) for lz in leases
            ],
            "batches": ser_batches,
            "closes": closes,
            "imports": [
                await imp_svc.ser(db, b, with_rows=False)
                for b in (
                    await db.execute(
                        select(LeaseImportBatch)
                        .where(LeaseImportBatch.tenant_id == tenant_id)
                        .order_by(LeaseImportBatch.created_at.desc())
                    )
                ).scalars()
            ],
            "notifications": [
                {
                    "id": n.id,
                    "at": iso(n.created_at),
                    "to": m.user_id,
                    "title": n.title,
                    "body": n.body,
                    "href": n.href or "/leases/inbox",
                    "read": n.is_read,
                }
                for n in notes
            ],
            "audit": [
                {
                    "id": a.id,
                    "at": iso(a.created_at),
                    "by": a.user_id,
                    "action": a.event_type.removeprefix("lease."),
                    "target": (a.details or {}).get("target", ""),
                    "detail": (a.details or {}).get("detail"),
                }
                for a in audit_rows
            ],
            "posted": posted,
            "savedViews": [
                {"id": v.id, "name": v.name, "query": v.query}
                for v in (
                    await db.execute(
                        select(LeaseSavedView).where(
                            LeaseSavedView.tenant_id == tenant_id,
                            LeaseSavedView.user_id == m.user_id,
                        )
                    )
                ).scalars()
            ],
            "importMapping": e.import_mapping if e else None,
        }
    )


# ─── Setup ─────────────────────────────────────────────────────────────────


@router.put("/entity")
async def put_entity(
    tenant_id: str, db: DbSession, body: Json, m: TenantMembership = Member
) -> dict[str, Any]:
    await svc.save_entity(db, m, body)
    return ok()


@router.post("/periods", status_code=201)
async def post_period(
    tenant_id: str, db: DbSession, body: Json, m: TenantMembership = Member
) -> dict[str, Any]:
    await svc.open_first_period(db, m, body.get("key", ""))
    return ok()


@router.post("/setup/{kind}", status_code=201)
async def post_setup(
    tenant_id: str, kind: str, db: DbSession, body: Json, m: TenantMembership = Member
) -> dict[str, Any]:
    if kind not in ("policies", "curves", "gl"):
        raise not_found()
    return ok(
        {
            "version": await svc.submit_setup(
                db,
                m,
                kind,
                body.get("data") or {},
                body.get("effectiveFrom", ""),
                body.get("reason", ""),
            )
        }
    )


@router.post("/setup/{kind}/{version}/decision")
async def decide_setup(
    tenant_id: str, kind: str, version: int, db: DbSession, body: Json, m: TenantMembership = Member
) -> dict[str, Any]:
    await svc.decide_setup(db, m, kind, version, bool(body.get("approve")), body.get("comment", ""))
    return ok()


@router.post("/counterparties", status_code=201)
async def post_counterparty(
    tenant_id: str, db: DbSession, body: Json, m: TenantMembership = Member
) -> dict[str, Any]:
    cp = await svc.add_counterparty(db, m, body.get("name", ""))
    await db.flush()
    return ok({"id": cp.id, "name": cp.name})


# ─── Leases ────────────────────────────────────────────────────────────────


@router.post("", status_code=201)
async def create_lease(
    tenant_id: str, db: DbSession, body: Json, m: TenantMembership = Member
) -> dict[str, Any]:
    lease = await svc.create_draft(db, m, body)
    return ok({"id": lease.id, "rev": lease.rev, "code": lease.code})


@router.get("/{lease_id}")
async def get_lease(
    tenant_id: str, lease_id: str, db: DbSession, m: TenantMembership = Member
) -> dict[str, Any]:
    lease, vs = await svc.load(db, tenant_id, lease_id)
    return ok(await svc.serialize(db, lease, vs))


@router.patch("/{lease_id}")
async def patch_lease(
    tenant_id: str, lease_id: str, db: DbSession, body: Json, m: TenantMembership = Member
) -> dict[str, Any]:
    return ok({"rev": await svc.update_draft(db, m, lease_id, int(body.get("rev", -1)), body)})


@router.post("/{lease_id}/calculate")
async def calculate(
    tenant_id: str, lease_id: str, db: DbSession, body: Json, m: TenantMembership = Member
) -> dict[str, Any]:
    return ok(await svc.calculate(db, m, lease_id, int(body.get("rev", -1))))


@router.post("/{lease_id}/submit")
async def submit(
    tenant_id: str, lease_id: str, db: DbSession, body: Json, m: TenantMembership = Member
) -> dict[str, Any]:
    return ok(
        {
            "rev": await svc.submit(
                db,
                m,
                lease_id,
                int(body.get("rev", -1)),
                body.get("approverId", ""),
                body.get("note", ""),
            )
        }
    )


@router.post("/{lease_id}/withdraw")
async def withdraw(
    tenant_id: str, lease_id: str, db: DbSession, body: Json, m: TenantMembership = Member
) -> dict[str, Any]:
    return ok({"rev": await svc.withdraw(db, m, lease_id, int(body.get("rev", -1)))})


@router.post("/{lease_id}/decision")
async def decision(
    tenant_id: str, lease_id: str, db: DbSession, body: Json, m: TenantMembership = Member
) -> dict[str, Any]:
    return ok(
        {
            "rev": await svc.decide(
                db,
                m,
                lease_id,
                int(body.get("rev", -1)),
                bool(body.get("approve")),
                body.get("text", ""),
                body.get("field"),
            )
        }
    )


@router.delete("/{lease_id}/draft")
async def discard(
    tenant_id: str, lease_id: str, db: DbSession, m: TenantMembership = Member
) -> dict[str, Any]:
    await svc.discard(db, m, lease_id)
    return ok()


@router.get("/{lease_id}/duplicates")
async def duplicates(
    tenant_id: str, lease_id: str, db: DbSession, m: TenantMembership = Member
) -> dict[str, Any]:
    return ok(await svc.duplicates(db, tenant_id, lease_id))


@router.post("/{lease_id}/events/preview")
async def event_preview(
    tenant_id: str, lease_id: str, db: DbSession, body: Json, m: TenantMembership = Member
) -> dict[str, Any]:
    return ok(await svc.preview_event(db, m, lease_id, body))


@router.post("/{lease_id}/events", status_code=201)
async def event(
    tenant_id: str, lease_id: str, db: DbSession, body: Json, m: TenantMembership = Member
) -> dict[str, Any]:
    """Create the event version, then submit it to the chosen reviewer (one transaction)."""
    rev = await svc.start_event(db, m, lease_id, int(body.get("rev", -1)), body)
    await db.flush()
    rev = await svc.submit(db, m, lease_id, rev, body.get("approverId", ""), body.get("reason", ""))
    return ok({"rev": rev})


@router.get("/{lease_id}/rate")
async def suggested_rate(
    tenant_id: str, lease_id: str, start: str, end: str, db: DbSession, m: TenantMembership = Member
) -> dict[str, Any]:
    lease, _ = await svc.load(db, tenant_id, lease_id)
    return ok(await svc.curve_rate_for(db, tenant_id, lease.currency, start, end))


# ─── Documents ─────────────────────────────────────────────────────────────


@router.post("/{lease_id}/documents", status_code=201)
async def upload_doc(
    tenant_id: str, lease_id: str, db: DbSession, file: Upload, m: TenantMembership = Member
) -> dict[str, Any]:
    lease, vs = await svc.load(db, tenant_id, lease_id, lock=True)
    if (file.content_type or "") not in DOC_TYPES:
        raise invalid(f"{file.filename}: upload a PDF or an image.")
    raw = await file.read(MAX_DOC + 1)
    if len(raw) > MAX_DOC:
        raise invalid(f"{file.filename} is larger than 25 MB.")
    sha = hashlib.sha256(raw).hexdigest()  # computed here; the client's hash is never trusted
    dup = (
        await db.execute(
            select(LeaseDocument, Lease)
            .join(Lease, Lease.id == LeaseDocument.lease_id)
            .where(LeaseDocument.tenant_id == tenant_id, LeaseDocument.sha256 == sha)
        )
    ).first()
    if dup:
        raise conflict(
            f"This exact file is already attached to {dup.Lease.code}{'' if dup.Lease.id == lease.id else f' ({dup.Lease.name})'}."
        )
    doc_id = new_ulid()
    safe = "".join(
        ch if ch.isalnum() or ch in "._-" else "_" for ch in (file.filename or "document")
    )[:120]
    key = f"/{tenant_id}/leases/{lease.id}/{doc_id}-{safe}"
    await upload_stream(key, io.BytesIO(raw), file.content_type or "application/octet-stream")
    has_docs = (
        await db.execute(
            select(LeaseDocument.id).where(LeaseDocument.lease_id == lease.id).limit(1)
        )
    ).first()
    db.add(
        LeaseDocument(
            id=doc_id,
            tenant_id=tenant_id,
            lease_id=lease.id,
            version_n=len(vs),
            name=file.filename or "document",
            size=len(raw),
            content_type=file.content_type or "",
            sha256=sha,
            kind="amendment" if has_docs else "contract",
            storage_key=key,
            uploaded_by=m.user_id,
        )
    )
    svc.touch(lease, None)
    await svc.audit(db, tenant_id, m.user_id, "lease.doc", lease.code, file.filename)
    return ok({"id": doc_id, "sha256": sha, "rev": lease.rev})


@router.get("/{lease_id}/documents/{doc_id}/url")
async def doc_url(
    tenant_id: str, lease_id: str, doc_id: str, db: DbSession, m: TenantMembership = Member
) -> dict[str, Any]:
    d = (
        await db.execute(
            select(LeaseDocument).where(
                LeaseDocument.tenant_id == tenant_id,
                LeaseDocument.lease_id == lease_id,
                LeaseDocument.id == doc_id,
            )
        )
    ).scalar_one_or_none()
    if d is None:
        raise not_found("Document not found.")
    return ok({"url": await presign_download(d.storage_key)})


@router.delete("/{lease_id}/documents/{doc_id}")
async def delete_doc(
    tenant_id: str, lease_id: str, doc_id: str, db: DbSession, m: TenantMembership = Member
) -> dict[str, Any]:
    lease, vs = await svc.load(db, tenant_id, lease_id, lock=True)
    d = (
        await db.execute(
            select(LeaseDocument).where(
                LeaseDocument.tenant_id == tenant_id,
                LeaseDocument.lease_id == lease_id,
                LeaseDocument.id == doc_id,
            )
        )
    ).scalar_one_or_none()
    if d is None:
        raise not_found("Document not found.")
    if any(v.status == "approved" and v.n == d.version_n for v in vs):
        raise invalid("Documents on approved versions can't be removed.")
    await db.delete(d)
    svc.touch(lease, None)
    await delete_object(d.storage_key)
    return ok({"rev": lease.rev})


# ─── Close / journals ──────────────────────────────────────────────────────


async def _batch_by_code(db: DbSession, tenant_id: str, code: str) -> LeaseJournalBatch:
    b = (
        await db.execute(
            select(LeaseJournalBatch).where(
                LeaseJournalBatch.tenant_id == tenant_id, LeaseJournalBatch.code == code
            )
        )
    ).scalar_one_or_none()
    if b is None:
        raise not_found("Batch not found.")
    return b


@router.post("/close/{period}/start")
async def close_start(
    tenant_id: str, period: str, db: DbSession, m: TenantMembership = Member
) -> dict[str, Any]:
    await close_svc.start(db, m, period)
    return ok()


@router.get("/close/{period}/issues")
async def close_issues(
    tenant_id: str, period: str, db: DbSession, m: TenantMembership = Member
) -> dict[str, Any]:
    c = await close_svc._close(db, tenant_id, period)
    if c is None:
        raise not_found("Close not started.")
    return ok(
        {
            "steps": {
                s["id"]: (
                    await close_svc.step_issues(db, tenant_id, c, s["id"])
                    if s["status"] != "locked"
                    else {"errors": [], "warnings": []}
                )
                for s in c.steps
            },
            "reconciliations": await close_svc.reconciliations(db, tenant_id, c),
            "sourceChecksum": await close_svc.source_checksum(db, tenant_id),
        }
    )


@router.post("/close/{period}/steps/{step}/complete")
async def close_step(
    tenant_id: str,
    period: str,
    step: str,
    db: DbSession,
    body: OptJson,
    m: TenantMembership = Member,
) -> dict[str, Any]:
    await close_svc.complete_step(
        db, m, period, step, body.get("note", ""), bool(body.get("acceptWarning"))
    )
    return ok()


@router.post("/close/{period}/generate")
async def close_generate(
    tenant_id: str, period: str, db: DbSession, m: TenantMembership = Member
) -> dict[str, Any]:
    b = await close_svc.generate(db, m, period)
    return ok({"id": b.code})


@router.post("/close/{period}/payments")
async def close_payments(
    tenant_id: str, period: str, db: DbSession, file: Upload, m: TenantMembership = Member
) -> dict[str, Any]:
    rows = imp_svc.parse_file(file.filename or "payments.csv", await file.read(5 * 1024 * 1024))
    head = [h.strip().lower() for h in rows[0]] if rows else []
    for col in ("date", "vendor", "amount", "reference"):
        if col not in head:
            raise invalid(f"The payments file needs a '{col}' column.")
    return ok(
        await close_svc.upload_payments(
            db, m, period, [dict(zip(head, r, strict=False)) for r in rows[1:]]
        )
    )


@router.post("/close/{period}/payments/confirm-scheduled")
async def close_confirm_scheduled(
    tenant_id: str, period: str, db: DbSession, body: OptJson, m: TenantMembership = Member
) -> dict[str, Any]:
    """For workspaces without an AP feed: confirm every still-open scheduled payment was paid as scheduled."""
    require(m, "administrator", "analyst")
    c = await close_svc._close(db, tenant_id, period)
    if c is None:
        raise invalid("Start the close first.")
    rows = list(
        (
            await db.execute(
                select(LeaseActualPayment).where(
                    LeaseActualPayment.close_id == c.id,
                    LeaseActualPayment.match == "late",
                    LeaseActualPayment.resolution.is_(None),
                )
            )
        ).scalars()
    )
    for p in rows:
        p.amount, p.match, p.resolution_note, p.resolved_by = (
            p.expected or p.amount,
            "matched",
            body.get("note") or "Confirmed paid as scheduled",
            m.user_id,
        )
    await svc.audit(
        db, tenant_id, m.user_id, "close.confirm_scheduled", period, f"{len(rows)} payments"
    )
    return ok({"confirmed": len(rows)})


@router.patch("/close/{period}/payments/{pid}")
async def close_resolve(
    tenant_id: str, period: str, pid: str, db: DbSession, body: Json, m: TenantMembership = Member
) -> dict[str, Any]:
    await close_svc.resolve_payment(
        db, m, period, pid, body.get("resolution", ""), body.get("note", "")
    )
    return ok()


@router.post("/periods/{period}/lock")
async def lock(
    tenant_id: str, period: str, db: DbSession, m: TenantMembership = Member
) -> dict[str, Any]:
    await close_svc.lock(db, m, period)
    return ok()


@router.post("/periods/{period}/reopen")
async def reopen(
    tenant_id: str, period: str, db: DbSession, body: Json, m: TenantMembership = Member
) -> dict[str, Any]:
    await close_svc.reopen(db, m, period, body.get("reason", ""))
    return ok()


@router.get("/batches/{code}/export")
async def batch_export(
    tenant_id: str, code: str, db: DbSession, m: TenantMembership = Member
) -> Response:
    b = await _batch_by_code(db, tenant_id, code)
    text = await close_svc.export_batch(db, m, b.id)
    return Response(
        "﻿" + text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{b.code}.csv"'},
    )


@router.post("/batches/{code}/post")
async def batch_post(
    tenant_id: str,
    code: str,
    db: DbSession,
    request: Request,
    body: Json,
    m: TenantMembership = Member,
) -> dict[str, Any]:
    b = await _batch_by_code(db, tenant_id, code)
    await close_svc.post_batch(db, m, b.id, body.get("erpRef", ""))
    return ok()


# ─── Imports ───────────────────────────────────────────────────────────────


@router.get("/imports/template.csv")
async def import_template(tenant_id: str, m: TenantMembership = Member) -> Response:
    buf = io.StringIO()
    csv.writer(buf).writerows(imp_svc.template_rows())
    return Response(
        "﻿" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="ledgerline_leases_template_{imp_svc.TEMPLATE_VERSION}.csv"'
        },
    )


@router.post("/imports", status_code=201)
async def import_upload(
    tenant_id: str, db: DbSession, file: Upload, m: TenantMembership = Member
) -> dict[str, Any]:
    raw = await file.read(imp_svc.MAX_BYTES + 1)
    b = await imp_svc.create(db, m, file.filename or "upload.csv", raw)
    return ok({"id": b.code})


@router.get("/imports/{code}")
async def import_get(
    tenant_id: str, code: str, db: DbSession, m: TenantMembership = Member
) -> dict[str, Any]:
    return ok(await imp_svc.ser(db, await imp_svc.get(db, tenant_id, code)))


@router.get("/imports/{code}/errors.csv")
async def import_errors(
    tenant_id: str, code: str, db: DbSession, m: TenantMembership = Member
) -> Response:
    b = await imp_svc.ser(db, await imp_svc.get(db, tenant_id, code))
    return Response(
        "﻿" + imp_svc.error_csv(b),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{code}_errors.csv"'},
    )


@router.put("/imports/{code}/mapping")
async def import_mapping(
    tenant_id: str, code: str, db: DbSession, body: Json, m: TenantMembership = Member
) -> dict[str, Any]:
    await imp_svc.set_mapping(db, m, code, body.get("mapping") or {})
    return ok()


@router.post("/imports/{code}/{action}")
async def import_action(
    tenant_id: str,
    code: str,
    action: str,
    db: DbSession,
    body: OptJson,
    m: TenantMembership = Member,
) -> dict[str, Any]:
    if action == "accept-warnings":
        await imp_svc.accept_warnings(db, m, code)
    elif action == "commit":
        await imp_svc.start_commit(db, m, code)
    elif action == "retry":
        await imp_svc.retry_failed(db, m, code)
    elif action == "cancel":
        await imp_svc.cancel(db, m, code)
    elif action == "submit":
        return ok({"submitted": await imp_svc.submit_all(db, m, code, body.get("approverId", ""))})
    else:
        raise not_found()
    return ok()


# ─── Views & notifications ─────────────────────────────────────────────────


@router.post("/views", status_code=201)
async def save_view(
    tenant_id: str, db: DbSession, body: Json, m: TenantMembership = Member
) -> dict[str, Any]:
    name = (body.get("name") or "").strip()
    if not name:
        raise invalid("Name the view.", "name")
    db.add(
        LeaseSavedView(
            id=new_ulid(),
            tenant_id=tenant_id,
            user_id=m.user_id,
            name=name,
            query=body.get("query") or "",
        )
    )
    return ok()


@router.delete("/views/{view_id}")
async def delete_view(
    tenant_id: str, view_id: str, db: DbSession, m: TenantMembership = Member
) -> dict[str, Any]:
    v = await db.get(LeaseSavedView, view_id)
    if v and v.tenant_id == tenant_id and v.user_id == m.user_id:
        await db.delete(v)
    return ok()


@router.post("/notifications/read")
async def notifications_read(
    tenant_id: str, db: DbSession, body: OptJson, m: TenantMembership = Member
) -> dict[str, Any]:
    q = update(UserNotification).where(
        UserNotification.tenant_id == tenant_id,
        UserNotification.user_id == m.user_id,
        UserNotification.kind == "leases",
    )
    if body.get("ids"):
        q = q.where(UserNotification.id.in_(body["ids"]))
    await db.execute(q.values(is_read=True))
    return ok()
