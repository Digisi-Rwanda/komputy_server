"""Disclosure exports per run (R13/R14), built from stored results. XLSX via xlsxwriter (constant memory) or CSV.

Every report is a list of sheets (title, header, rows), so the same data feeds both formats.
"""

from __future__ import annotations

import csv
import io
from typing import Annotated, Any

import xlsxwriter
from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import func, select

from ledgerline.apps.results.models import EclLoanSummary, RunException
from ledgerline.apps.runs.models import Run
from ledgerline.apps.runs.views import STAGE_NO
from ledgerline.apps.runs.views import compare as compare_view
from ledgerline.auth.deps import CurrentUser, DbSession, Membership, require_tenant_member
from ledgerline.events.audit_service import log_event
from ledgerline.kernel.errors import ECLException

router = APIRouter(prefix="/tenants", tags=["reports"])
Member = Annotated[Membership, Depends(require_tenant_member)]
Sheet = tuple[str, list[str], list[list[Any]]]
KINDS = {"stage", "rollforward", "coverage", "exceptions", "recon", "board"}


def _safe(v: Any) -> Any:
    """Formula-injection guard for text cells opened in Excel."""
    return f"'{v}" if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v


async def _sheets(db: Any, tenant_id: str, run: Run, kind: str, compare: str | None, m: Membership) -> list[Sheet]:
    s = EclLoanSummary
    by_stage = (await db.execute(select(s.stage, func.count(), func.sum(s.outstanding), func.sum(s.total_ecl)).where(s.run_id == run.id).group_by(s.stage).order_by(s.stage))).all()
    by_seg_stage = (await db.execute(select(s.segment, s.stage, func.count(), func.sum(s.outstanding), func.sum(s.total_ecl)).where(s.run_id == run.id).group_by(s.segment, s.stage).order_by(s.segment, s.stage))).all()
    if kind == "stage":
        rows = [[f"Stage {STAGE_NO.get(str(st), st)}", int(n), float(x), float(e), float(e) / float(x) if x else 0] for st, n, x, e in by_stage]
        out: list[Sheet] = [("Stage summary", ["Stage", "Loans", "Exposure", "ECL", "Coverage"], rows)]
        if compare:
            c = (await compare_view(tenant_id, compare, run.id, db, m))["data"]
            out.append(("Stage migration", ["From \\ To", "Stage 1", "Stage 2", "Stage 3"], [[f"Stage {i + 1}", *r] for i, r in enumerate(c["migration"])]))
        return out
    if kind == "rollforward":
        if not compare:
            raise ECLException("COMPARE_REQUIRED", "Pick the run to roll forward from.", 400)
        c = (await compare_view(tenant_id, compare, run.id, db, m))["data"]
        return [("Roll-forward", ["Movement", "Amount"], [[x["label"], x["value"]] for x in c["movement"]]),
                ("By segment", ["Segment", "Opening ECL", "Closing ECL", "Change %"], [[x["segment"], x["a"], x["b"], x["delta"]] for x in c["segments"]])]
    if kind == "coverage":
        return [("Coverage", ["Segment", "Stage", "Loans", "Exposure", "ECL", "Coverage"],
                 [[seg, f"Stage {STAGE_NO.get(str(st), st)}", int(n), float(x), float(e), float(e) / float(x) if x else 0] for seg, st, n, x, e in by_seg_stage])]
    if kind == "exceptions":
        ex = (await db.execute(select(RunException).where(RunException.run_id == run.id).order_by(RunException.seq))).scalars().all()
        return [("Exceptions", ["#", "Category", "Dataset", "Loan ID", "Customer ID", "Field", "Value", "Issue", "Action"],
                 [[e.seq, e.category, e.dataset, e.loan_id, e.customer_id, e.field, e.value, e.issue, e.action] for e in ex])]
    if kind == "recon":
        recon = run.reconciliation or []
        return [("Reconciliation", ["Check", "Expected", "Actual", "Difference", "Passed", "Detail"],
                 [[r.get("name"), r.get("expected"), r.get("actual"), r.get("difference"), "Yes" if r.get("passed") else "No", r.get("detail")] for r in recon]),
                ("Certificate", ["Field", "Value"], [["Run", run.name], ["Run ID", run.id], ["Engine", run.engine_version], ["Reporting date", str(run.reporting_date or "")],
                                                     ["Checks passed", f"{sum(1 for r in recon if r.get('passed'))} of {len(recon)}"], ["Signed off", str(run.signed_off_at or "Not signed off")]])]
    # board
    total, exp = float(run.total_ecl or 0), float(run.total_outstanding or 0)
    return [("Summary", ["Measure", "Value"], [["Run", run.name], ["Reporting date", str(run.reporting_date or "")], ["Currency", run.currency], ["Total exposure", exp],
                                               ["Total ECL", total], ["Coverage", total / exp if exp else 0], ["Signed off", str(run.signed_off_at or "Not signed off")]]),
            ("By stage", ["Stage", "Exposure", "ECL"], [[f"Stage {STAGE_NO.get(str(st), st)}", float(x), float(e)] for st, _, x, e in by_stage])]


def _xlsx(sheets: list[Sheet], title: str) -> bytes:
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"constant_memory": True, "in_memory": False})
    head = wb.add_format({"bold": True, "bg_color": "#F2F2F2", "border": 1})
    num = wb.add_format({"num_format": "#,##0.00;(#,##0.00)"})
    for name, header, rows in sheets:
        ws = wb.add_worksheet(name[:31])
        ws.write_row(0, 0, header, head)
        for i, r in enumerate(rows, start=1):
            for j, v in enumerate(r):
                ws.write(i, j, _safe(v), num if isinstance(v, float) else None)
        ws.set_column(0, len(header) - 1, 18)
    wb.set_properties({"title": title, "company": "Ledgerline"})
    wb.close()
    return buf.getvalue()


def _csv(sheets: list[Sheet]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    for i, (name, header, rows) in enumerate(sheets):
        if i:
            w.writerow([])
        w.writerow([f"# {name}"])
        w.writerow(header)
        w.writerows([[_safe(v) for v in r] for r in rows])
    return buf.getvalue().encode("utf-8-sig")


@router.get("/{tenant_id}/ecl/runs/{run_id}/reports/{kind}")
async def report(tenant_id: str, run_id: str, kind: str, db: DbSession, user: CurrentUser, m: Member,
                 fmt: str = Query(default="xlsx", pattern="^(xlsx|csv)$"), compare: str | None = None) -> Response:
    if kind not in KINDS:
        raise ECLException("UNKNOWN_REPORT", "Unknown report.", 404)
    run = (await db.execute(select(Run).where(Run.id == run_id, Run.tenant_id == tenant_id, Run.deleted_at.is_(None)))).scalar_one_or_none()
    if not run or run.status != "complete":
        raise ECLException("RUN_NOT_COMPLETE", "Reports need a computed run.", 409)
    sheets = await _sheets(db, tenant_id, run, kind, compare, m)
    name = f"{run.name} - {kind}".replace("/", "-")
    body = _xlsx(sheets, name) if fmt == "xlsx" else _csv(sheets)
    await log_event(db, "ecl.report_exported", user_id=user.id, tenant_id=tenant_id, details={"run_id": run_id, "kind": kind, "fmt": fmt, "description": name})
    media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if fmt == "xlsx" else "text/csv; charset=utf-8"
    return Response(body, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}.{fmt}"', "Cache-Control": "no-store"})
