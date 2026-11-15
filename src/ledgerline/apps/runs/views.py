"""Web-shaped read models (the Next.js app's `Run` type), computed from stored results in a few grouped queries.

Kept separate from the ported API so the web UI gets exactly the shape it renders without N+1 calls:
status folds the compute state and the review state into the one lifecycle the UI shows.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from ledgerline.apps.results.models import EclLoanSummary, RunException
from ledgerline.apps.runs.models import Run, Upload
from ledgerline.auth.deps import DbSession, Membership, require_tenant_member
from ledgerline.kernel.errors import ECLException
from ledgerline.web import User

router = APIRouter(prefix="/tenants", tags=["views"])
Member = Annotated[Membership, Depends(require_tenant_member)]
COMPUTING = {"queued", "pd_running", "lgd_running", "ead_running"}
STAGE_NO = {"Stage 1": 1, "Stage 2": 2, "Stage 3": 3, "1": 1, "2": 2, "3": 3}


def ui_status(run: Run, validating: bool) -> str:
    if run.status in COMPUTING:
        return "computing"
    if run.status == "failed":
        return "failed"
    if run.status == "complete":
        return {"returned": "returned", "signed_off": "signed-off", "archived": "archived"}.get(
            run.review_status or "", "needs-review"
        )
    return "validating" if validating else "draft"


def _step(uploads: list[Upload]) -> int:
    """Wizard step to resume at (1 setup … 7 review), from what's been uploaded and accepted."""
    kinds = {u.kind: u for u in uploads}
    for i, k in enumerate(("PD", "LGD", "EAD")):
        u = kinds.get(k)
        if not u:
            return 2 + i
        if u.validation_status not in ("ok", "warn") or (u.validation_status == "warn" and not u.warnings_accepted):
            return 2 + i
    return 6


async def _views(db: Any, tenant_id: str, run_ids: list[str] | None = None) -> list[dict[str, Any]]:
    q = (
        select(Run, User.name)
        .outerjoin(User, User.id == Run.created_by_user_id)
        .where(Run.tenant_id == tenant_id, Run.deleted_at.is_(None))
    )
    if run_ids:
        q = q.where(Run.id.in_(run_ids))
    rows = (await db.execute(q.order_by(Run.created_at.desc()).limit(500))).all()
    ids = [r.id for r, _ in rows]
    if not ids:
        return []
    stages: dict[str, dict[int, tuple[float, float]]] = {}
    for rid, stage, ecl, exp in (
        await db.execute(
            select(
                EclLoanSummary.run_id,
                EclLoanSummary.stage,
                func.sum(EclLoanSummary.total_ecl),
                func.sum(EclLoanSummary.outstanding),
            )
            .where(EclLoanSummary.run_id.in_(ids))
            .group_by(EclLoanSummary.run_id, EclLoanSummary.stage)
        )
    ).all():
        stages.setdefault(rid, {})[STAGE_NO.get(str(stage), 0)] = (float(ecl or 0), float(exp or 0))
    loans = dict(
        (
            await db.execute(
                select(EclLoanSummary.run_id, func.count())
                .where(EclLoanSummary.run_id.in_(ids))
                .group_by(EclLoanSummary.run_id)
            )
        ).all()
    )
    excs = dict(
        (
            await db.execute(
                select(RunException.run_id, func.count())
                .where(RunException.run_id.in_(ids))
                .group_by(RunException.run_id)
            )
        ).all()
    )
    uploads: dict[str, list[Upload]] = {}
    for u in (await db.execute(select(Upload).where(Upload.run_id.in_(ids)))).scalars():
        uploads.setdefault(u.run_id, []).append(u)
    reviewers = dict(
        (
            await db.execute(
                select(User.id, User.name).where(
                    User.id.in_({r.reviewed_by_user_id for r, _ in rows if r.reviewed_by_user_id} or {""})
                )
            )
        ).all()
    )

    out: list[dict[str, Any]] = []
    prior_ecl: float | None = None
    for run, by in reversed(rows):  # oldest first so each run can compare with the previous completed one
        st = stages.get(run.id, {})
        total = float(run.total_ecl) if run.total_ecl is not None else sum(v[0] for v in st.values())
        exposure = float(run.total_outstanding) if run.total_outstanding is not None else sum(v[1] for v in st.values())
        recon = run.reconciliation or []
        ups = uploads.get(run.id, [])
        item = {
            "id": run.id,
            "name": run.name,
            "reportingDate": (run.reporting_date.isoformat() if run.reporting_date else (run.reporting_period or "")),
            "status": ui_status(run, any(u.validation_status is None for u in ups) and bool(ups)),
            "engine": f"v{run.engine_version.lstrip('v')}",
            "currency": run.currency,
            "totalEcl": total,
            "exposure": exposure,
            "coverage": (total / exposure) if exposure else 0.0,
            "deltaVsPrior": ((total - prior_ecl) / prior_ecl) if (prior_ecl and run.status == "complete") else 0.0,
            "stages": [
                {
                    "stage": s,
                    "ecl": st.get(s, (0.0, 0.0))[0],
                    "exposure": st.get(s, (0.0, 0.0))[1],
                    "share": (st.get(s, (0.0, 0.0))[1] / exposure) if exposure else 0.0,
                }
                for s in (1, 2, 3)
            ],
            "loans": int(loans.get(run.id, 0)),
            "exceptions": int(excs.get(run.id, 0)),
            "checksPassed": sum(1 for c in recon if c.get("passed")),
            "checksTotal": len(recon),
            "createdBy": by or "",
            "createdById": run.created_by_user_id,
            "reviewer": reviewers.get(run.reviewed_by_user_id) if run.reviewed_by_user_id else None,
            "signedOffAt": run.signed_off_at.isoformat() if run.signed_off_at else None,
            "updatedAt": run.updated_at.isoformat(),
            "createdAt": run.created_at.isoformat(),
            "failureMessage": run.failure_message,
            "failureStage": run.failure_stage,
            "progress": run.engine_progress,
            "locked": run.locked_at is not None,
            "supersedes": run.supersedes_run_id,
            **({"step": _step(ups)} if run.status in ("draft", "failed") else {}),
        }
        if run.status == "complete" and run.review_status != "archived":
            prior_ecl = total
        out.append(item)
    return list(reversed(out))


@router.get("/{tenant_id}/ecl/views/runs")
async def runs_view(tenant_id: str, db: DbSession, _m: Member) -> dict[str, Any]:
    return {"data": await _views(db, tenant_id)}


@router.get("/{tenant_id}/ecl/views/runs/{run_id}")
async def run_view(tenant_id: str, run_id: str, db: DbSession, _m: Member) -> dict[str, Any]:
    rows = await _views(db, tenant_id)  # needs neighbours for deltaVsPrior
    item = next((r for r in rows if r["id"] == run_id), None)
    if not item:
        raise ECLException("RUN_NOT_FOUND", "Run not found.", 404)
    segs = (
        await db.execute(
            select(
                EclLoanSummary.segment,
                EclLoanSummary.stage,
                func.count(),
                func.sum(EclLoanSummary.outstanding),
                func.sum(EclLoanSummary.total_ecl),
            )
            .where(EclLoanSummary.run_id == run_id)
            .group_by(EclLoanSummary.segment, EclLoanSummary.stage)
        )
    ).all()
    by_seg: dict[str, dict[str, Any]] = {}
    for seg, stage, n, exp, ecl in segs:
        d = by_seg.setdefault(
            seg, {"segment": seg, "loans": 0, "exposure": 0.0, "ecl": 0.0, "stages": {1: 0.0, 2: 0.0, 3: 0.0}}
        )
        d["loans"] += int(n)
        d["exposure"] += float(exp or 0)
        d["ecl"] += float(ecl or 0)
        d["stages"][STAGE_NO.get(str(stage), 0)] = d["stages"].get(STAGE_NO.get(str(stage), 0), 0.0) + float(ecl or 0)
    from ledgerline.apps.results.models import OutputArtifact

    run = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()
    item["reconciliation"] = run.reconciliation or []
    item["inputs"] = [
        {
            "id": u.id,
            "kind": u.kind,
            "filename": u.original_filename,
            "sha256": u.sha256,
            "size": u.file_size_bytes,
            "rows": u.row_count,
            "sheets": u.sheet_count,
            "status": u.validation_status,
            "warningsAccepted": u.warnings_accepted,
            "at": u.created_at.isoformat(),
        }
        for u in (
            await db.execute(select(Upload).where(Upload.run_id == run_id).order_by(Upload.kind, Upload.created_at))
        ).scalars()
    ]
    item["outputs"] = [
        {"kind": a.kind, "sha256": a.sha256, "size": a.file_size_bytes, "at": a.created_at.isoformat()}
        for a in (
            await db.execute(
                select(OutputArtifact).where(OutputArtifact.run_id == run_id).order_by(OutputArtifact.created_at)
            )
        ).scalars()
    ]
    item["segments"] = [
        {
            **d,
            "coverage": d["ecl"] / d["exposure"] if d["exposure"] else 0.0,
            "stages": [d["stages"][1], d["stages"][2], d["stages"][3]],
        }
        for d in sorted(by_seg.values(), key=lambda x: -x["ecl"])
    ]
    return {"data": item}


@router.get("/{tenant_id}/ecl/views/runs/{run_id}/loans")
async def run_loans(
    tenant_id: str,
    run_id: str,
    db: DbSession,
    _m: Member,
    segment: str | None = None,
    stage: int | None = None,
    q: str | None = None,
    after: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Loan-level results, keyset-paginated by loan id (stable across millions of rows; no OFFSET scans)."""
    from ledgerline.apps.results.models import EadResult

    limit = max(1, min(limit, 500))
    s = EclLoanSummary
    w = [s.run_id == run_id, s.tenant_id == tenant_id]
    if segment:
        w.append(s.segment == segment)
    if stage:
        w.append(s.stage.in_((f"Stage {stage}", str(stage))))
    if q:
        w.append(s.loan_id.ilike(f"%{q}%") | s.customer_id.ilike(f"%{q}%"))
    if after:
        w.append(s.loan_id > after)
    rows = (await db.execute(select(s).where(*w).order_by(s.loan_id).limit(limit + 1))).scalars().all()
    page, more = rows[:limit], len(rows) > limit
    ids = [r.loan_id for r in page]
    # Per-loan PD/LGD/EAD for display: lifetime PD (sum of marginal PDs, ≤ 1), mean LGD, peak EAD over the projection.
    risk = (
        {
            r[0]: r[1:]
            for r in (
                await db.execute(
                    select(
                        EadResult.loan_id,
                        func.sum(EadResult.marginal_pd),
                        func.avg(EadResult.lgd),
                        func.max(EadResult.bal_after_missed),
                    )
                    .where(EadResult.run_id == run_id, EadResult.loan_id.in_(ids))
                    .group_by(EadResult.loan_id)
                )
            ).all()
        }
        if ids
        else {}
    )
    total = await db.scalar(select(func.count()).select_from(s).where(*w[:-1] if after else w)) or 0
    return {
        "data": {
            "rows": [
                {
                    "id": r.loan_id,
                    "customer": r.customer_id,
                    "segment": r.segment,
                    "stage": STAGE_NO.get(str(r.stage), 0),
                    "outstanding": float(r.outstanding),
                    "ead": float((risk.get(r.loan_id) or (0, 0, r.outstanding))[2] or r.outstanding),
                    "pd": min(1.0, float((risk.get(r.loan_id) or (0, 0, 0))[0] or 0)),
                    "lgd": float((risk.get(r.loan_id) or (0, 0, 0))[1] or 0),
                    "ecl": float(r.total_ecl),
                    "capped": bool(r.ecl_capped),
                }
                for r in page
            ],
            "next": page[-1].loan_id if more and page else None,
            "total": int(total),
        }
    }


@router.get("/{tenant_id}/ecl/views/runs/{run_id}/exceptions")
async def run_exceptions(tenant_id: str, run_id: str, db: DbSession, _m: Member, limit: int = 1000) -> dict[str, Any]:
    rows = (
        (
            await db.execute(
                select(RunException)
                .where(RunException.run_id == run_id, RunException.tenant_id == tenant_id)
                .order_by(RunException.seq)
                .limit(max(1, min(limit, 5000)))
            )
        )
        .scalars()
        .all()
    )
    return {
        "data": [
            {
                "id": str(r.seq),
                "category": r.category,
                "dataset": r.dataset,
                "loan": r.loan_id,
                "customer": r.customer_id,
                "rule": getattr(r, "field", "") or r.category,
                "detail": getattr(r, "issue", ""),
                "impact": getattr(r, "action", ""),
                "value": getattr(r, "value", ""),
                "amount": 0,
            }
            for r in rows
        ]
    }


@router.get("/{tenant_id}/ecl/views/runs/{run_id}/audit")
async def run_audit(tenant_id: str, run_id: str, db: DbSession, _m: Member) -> dict[str, Any]:
    from sqlalchemy import cast
    from sqlalchemy.dialects.postgresql import JSONB

    from ledgerline.events.audit_models import AuditLog

    rows = (
        await db.execute(
            select(AuditLog, User.name)
            .outerjoin(User, User.id == AuditLog.user_id)
            .where(AuditLog.tenant_id == tenant_id, cast(AuditLog.details, JSONB)["run_id"].astext == run_id)
            .order_by(AuditLog.created_at)
        )
    ).all()
    out = []
    for a, name in rows:
        d = a.details or {}
        out.append(
            {
                "at": a.created_at.isoformat(),
                "actor": name or "Ledgerline",
                "initials": "".join(w[0] for w in (name or "LL").split()[:2]).upper(),
                "action": a.event_type.replace("_", " ").replace(".", " · "),
                "object": d.get("description") or d.get("comment") or run_id,
                "service": "ECL",
            }
        )
    return {"data": out}


SIZE_BUCKETS = [
    (0, 10_000, "< 10k"),
    (10_000, 50_000, "10k–50k"),
    (50_000, 250_000, "50k–250k"),
    (250_000, 1_000_000, "250k–1M"),
    (1_000_000, None, "> 1M"),
]


@router.get("/{tenant_id}/ecl/views/runs/{run_id}/explore")
async def explore(
    tenant_id: str, run_id: str, db: DbSession, _m: Member, segment: str | None = None, top: int = 10
) -> dict[str, Any]:
    """Portfolio / segment drill-down: top loans by ECL, ECL by loan size, stage split and cure/LGD stats."""
    from sqlalchemy import case

    from ledgerline.apps.results.models import EadResult

    s = EclLoanSummary
    w = [s.run_id == run_id, s.tenant_id == tenant_id] + ([s.segment == segment] if segment else [])
    top_rows = (
        (await db.execute(select(s).where(*w).order_by(s.total_ecl.desc()).limit(max(1, min(top, 100)))))
        .scalars()
        .all()
    )
    bucket = case(
        *[
            ((s.outstanding >= lo) & (s.outstanding < hi) if hi else (s.outstanding >= lo), label)
            for lo, hi, label in SIZE_BUCKETS
        ]
    )
    sizes = dict((await db.execute(select(bucket, func.sum(s.total_ecl)).where(*w).group_by(bucket))).all())
    stages = {
        STAGE_NO.get(str(st), 0): (float(e or 0), float(x or 0), int(n))
        for st, e, x, n in (
            await db.execute(
                select(s.stage, func.sum(s.total_ecl), func.sum(s.outstanding), func.count())
                .where(*w)
                .group_by(s.stage)
            )
        ).all()
    }
    tot_e = sum(v[0] for v in stages.values())
    tot_x = sum(v[1] for v in stages.values())
    stats = (
        await db.execute(
            select(func.avg(EadResult.cure_rate), func.avg(EadResult.lgd)).where(
                EadResult.run_id == run_id, *([EadResult.segment == segment] if segment else [])
            )
        )
    ).one()
    return {
        "data": {
            "loans": sum(v[2] for v in stages.values()),
            "ecl": tot_e,
            "exposure": tot_x,
            "stages": [
                {
                    "stage": k,
                    "ecl": stages.get(k, (0, 0, 0))[0],
                    "exposure": stages.get(k, (0, 0, 0))[1],
                    "share": (stages.get(k, (0, 0, 0))[1] / tot_x) if tot_x else 0,
                }
                for k in (1, 2, 3)
            ],
            "sizes": [{"bucket": label, "ecl": float(sizes.get(label) or 0)} for _, _, label in SIZE_BUCKETS],
            "top": [
                {
                    "id": r.loan_id,
                    "customer": r.customer_id,
                    "segment": r.segment,
                    "stage": STAGE_NO.get(str(r.stage), 0),
                    "outstanding": float(r.outstanding),
                    "ecl": float(r.total_ecl),
                    "capped": r.ecl_capped,
                }
                for r in top_rows
            ],
            "cureRate": float(stats[0]) if stats[0] is not None else None,
            "avgLgd": float(stats[1]) if stats[1] is not None else None,
        }
    }


@router.get("/{tenant_id}/ecl/views/runs/{run_id}/loans/{loan_id}")
async def loan_detail(tenant_id: str, run_id: str, loan_id: str, db: DbSession, _m: Member) -> dict[str, Any]:
    """One loan: summary, collateral, and the month-by-month schedule whose discounted losses sum to its ECL."""
    from ledgerline.apps.results.models import EadResult, LgdResult

    s = (
        await db.execute(
            select(EclLoanSummary).where(
                EclLoanSummary.run_id == run_id,
                EclLoanSummary.tenant_id == tenant_id,
                EclLoanSummary.loan_id == loan_id,
            )
        )
    ).scalar_one_or_none()
    if not s:
        raise ECLException("LOAN_NOT_FOUND", "That loan isn't in this run.", 404)
    rows = (
        (
            await db.execute(
                select(EadResult)
                .where(EadResult.run_id == run_id, EadResult.loan_id == loan_id)
                .order_by(EadResult.snapshot_date)
            )
        )
        .scalars()
        .all()
    )
    lgd = (
        await db.execute(select(LgdResult).where(LgdResult.run_id == run_id, LgdResult.loan_id == loan_id))
    ).scalar_one_or_none()
    return {
        "data": {
            "id": s.loan_id,
            "customer": s.customer_id,
            "segment": s.segment,
            "stage": STAGE_NO.get(str(s.stage), 0),
            "reportingDate": s.reporting_date.isoformat(),
            "outstanding": float(s.outstanding),
            "eclBeforeCap": float(s.ecl_before_cap),
            "ecl": float(s.total_ecl),
            "capped": s.ecl_capped,
            "discountedCollateral": float(s.discounted_collateral),
            "eir": float(lgd.eir) if lgd else None,
            "treatment": rows[0].treatment if rows else None,
            "pdRule": rows[0].pd_rule if rows else None,
            "schedule": [
                {
                    "month": r.snapshot_date.isoformat()[:7],
                    "sinceOrigination": r.period_since_orig,
                    "balance": float(r.bal_after_repayment),
                    "ead": float(r.bal_after_missed),
                    "pd": float(r.marginal_pd or 0),
                    "lgd": float(r.lgd),
                    "loss": float(r.credit_loss),
                    "discounted": float(r.discounted_ecl),
                }
                for r in rows
            ],
        }
    }


@router.get("/{tenant_id}/ecl/views/compare")
async def compare(tenant_id: str, a: str, b: str, db: DbSession, _m: Member) -> dict[str, Any]:
    """Run A → run B, loan by loan. The movement steps tie exactly: opening + new − derecognised + transfers +
    remeasurement = closing (every loan is in exactly one bucket)."""
    from sqlalchemy import text

    for rid in (a, b):
        if not await db.scalar(select(func.count()).select_from(Run).where(Run.id == rid, Run.tenant_id == tenant_id)):
            raise ECLException("RUN_NOT_FOUND", "Run not found.", 404)
    rows = (
        await db.execute(
            text("""
        WITH x AS (SELECT loan_id, segment, stage, total_ecl FROM engine.ecl_loan_summaries WHERE run_id = :a AND tenant_id = :t),
             y AS (SELECT loan_id, segment, stage, total_ecl FROM engine.ecl_loan_summaries WHERE run_id = :b AND tenant_id = :t)
        SELECT x.stage AS sa, y.stage AS sb, coalesce(y.segment, x.segment) AS seg,
               count(*) AS n, coalesce(sum(x.total_ecl), 0) AS ea, coalesce(sum(y.total_ecl), 0) AS eb
        FROM x FULL OUTER JOIN y ON x.loan_id = y.loan_id
        GROUP BY 1, 2, 3"""),
            {"a": a, "b": b, "t": tenant_id},
        )
    ).all()
    st = lambda v: STAGE_NO.get(str(v), 0) if v is not None else None  # noqa: E731
    mig = [[0, 0, 0] for _ in range(3)]
    opening = new = gone = transfer = remeasure = 0.0
    segs: dict[str, list[float]] = {}
    for sa, sb, seg, n, ea, eb in rows:
        ea, eb, i, j = float(ea), float(eb), st(sa), st(sb)
        opening += ea
        segs.setdefault(seg, [0.0, 0.0])
        segs[seg][0] += ea
        segs[seg][1] += eb
        if i is None:
            new += eb
        elif j is None:
            gone -= ea
        else:
            if 1 <= i <= 3 and 1 <= j <= 3:
                mig[i - 1][j - 1] += int(n)
            if i != j:
                transfer += eb - ea
            else:
                remeasure += eb - ea
    closing = opening + new + gone + transfer + remeasure
    return {
        "data": {
            "migration": mig,
            "movement": [
                {"label": "Opening", "value": opening, "kind": "total"},
                {"label": "New loans", "value": new},
                {"label": "Derecognised", "value": gone},
                {"label": "Stage transfers", "value": transfer},
                {"label": "Remeasurement", "value": remeasure},
                {"label": "Closing", "value": closing, "kind": "total"},
            ],
            "segments": [
                {"segment": k, "a": v[0], "b": v[1], "delta": (v[1] - v[0]) / v[0] if v[0] else 0.0}
                for k, v in sorted(segs.items(), key=lambda kv: -kv[1][1])
            ],
        }
    }
