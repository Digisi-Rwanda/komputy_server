from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from ledgerline.apps.results import service as results_service
from ledgerline.apps.runs import ecl_validation, pd_validation, service
from ledgerline.apps.runs.jobs import compute_run, validate
from ledgerline.apps.runs.schemas import (
    AcceptReviewResponse,
    CreateRunRequest,
    ExecuteRunResponse,
    PresignedDownloadResponse,
    RerunRequest,
    RunListResponse,
    RunResponse,
    UpdateRunRequest,
    UploadResponse,
    ValidateRunRequest,
)
from ledgerline.auth.deps import (
    CurrentUser,
    DbSession,
    get_client_ip,
    get_user_agent,
    require_tenant_admin,
    require_tenant_analyst_or_admin,
    require_tenant_member,
)
from ledgerline.auth.deps import Membership as TenantMembership
from ledgerline.log import get_logger

_log = get_logger("runs.router")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

router = APIRouter(prefix="/tenants", tags=["runs"])

_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "ecl" / "templates"


@router.post("/{tenant_id}/runs", status_code=status.HTTP_201_CREATED)
async def create_run_endpoint(
    tenant_id: str,
    body: CreateRunRequest,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    _a: TenantMembership = Depends(require_tenant_analyst_or_admin),
) -> RunResponse:
    _log.info("run_create_requested", tenant_id=tenant_id, user_id=user.id, name=body.name)
    data = await service.create_run(
        db,
        tenant_id,
        user.id,
        body,
        ip=get_client_ip(request.headers.get("X-Forwarded-For")),
        user_agent=get_user_agent(request.headers.get("User-Agent")),
    )
    _log.info("run_created", run_id=data.fullId, tenant_id=tenant_id, user_id=user.id)
    return RunResponse(data=data)


@router.patch("/{tenant_id}/runs/{run_id}")
async def update_run_endpoint(
    tenant_id: str,
    run_id: str,
    body: UpdateRunRequest,
    db: DbSession,
    user: CurrentUser,
    _a: TenantMembership = Depends(require_tenant_analyst_or_admin),
) -> RunResponse:
    data = await service.update_run(db, tenant_id, run_id, body)
    return RunResponse(data=data)


@router.post("/{tenant_id}/runs/{run_id}/uploads", status_code=status.HTTP_201_CREATED)
async def upload_file_endpoint(
    request: Request,
    tenant_id: str,
    run_id: str,
    db: DbSession,
    user: CurrentUser,
    kind: str = Form(...),
    file: UploadFile = File(...),
    _a: TenantMembership = Depends(require_tenant_analyst_or_admin),
) -> UploadResponse:
    _log.info(
        "file_upload_received",
        run_id=run_id,
        tenant_id=tenant_id,
        kind=kind.upper(),
        filename=file.filename,
        content_type=file.content_type,
    )
    data = await service.upload_file(
        db,
        tenant_id,
        run_id,
        user.id,
        kind=kind.upper(),
        filename=file.filename or "upload.xlsx",
        file_obj=file.file,
        content_type=file.content_type or "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ip=get_client_ip(request.headers.get("X-Forwarded-For")),
        user_agent=get_user_agent(request.headers.get("User-Agent")),
    )
    _log.info(
        "file_upload_complete",
        run_id=run_id,
        kind=kind.upper(),
        filename=data.filename,
        size_bytes=data.size_bytes,
        sha256_prefix=data.sha256[:8],
        sheets=data.sheet_count,
        rows=data.row_count,
    )
    return UploadResponse(data=data)


class DirectUploadStart(BaseModel):
    kind: str = Field(pattern="^(PD|LGD|EAD|pd|lgd|ead)$")
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)
    content_type: str = Field(default=XLSX, alias="contentType")


class DirectUploadPart(BaseModel):
    part_number: int = Field(alias="partNumber", ge=1, le=10000)
    etag: str = Field(min_length=1, max_length=200)


class DirectUploadComplete(BaseModel):
    kind: str = Field(pattern="^(PD|LGD|EAD|pd|lgd|ead)$")
    filename: str = Field(min_length=1, max_length=255)
    key: str = Field(min_length=1, max_length=1024)
    upload_id: str = Field(alias="uploadId", min_length=1, max_length=1024)
    parts: list[DirectUploadPart] = Field(min_length=1, max_length=10000)
    content_type: str = Field(default=XLSX, alias="contentType")


@router.post("/{tenant_id}/runs/{run_id}/uploads/direct", status_code=status.HTTP_201_CREATED)
async def start_direct_upload_endpoint(
    tenant_id: str,
    run_id: str,
    body: DirectUploadStart,
    db: DbSession,
    _a: TenantMembership = Depends(require_tenant_analyst_or_admin),
) -> dict[str, Any]:
    """Browser uploads straight to object storage in parallel parts; nothing streams through the API."""
    return {
        "data": await service.start_direct_upload(
            db,
            tenant_id,
            run_id,
            kind=body.kind.upper(),
            filename=body.filename,
            size=body.size,
            content_type=body.content_type,
        )
    }


@router.post("/{tenant_id}/runs/{run_id}/uploads/direct/complete", status_code=status.HTTP_201_CREATED)
async def complete_direct_upload_endpoint(
    request: Request,
    tenant_id: str,
    run_id: str,
    body: DirectUploadComplete,
    db: DbSession,
    user: CurrentUser,
    _a: TenantMembership = Depends(require_tenant_analyst_or_admin),
) -> UploadResponse:
    data = await service.complete_direct_upload(
        db,
        tenant_id,
        run_id,
        user.id,
        kind=body.kind.upper(),
        filename=body.filename,
        key=body.key,
        upload_id=body.upload_id,
        parts=[{"partNumber": p.part_number, "etag": p.etag} for p in body.parts],
        content_type=body.content_type,
        ip=get_client_ip(request.headers.get("X-Forwarded-For")),
        user_agent=get_user_agent(request.headers.get("User-Agent")),
    )
    return UploadResponse(data=data)


@router.delete("/{tenant_id}/runs/{run_id}/uploads/{upload_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_upload_endpoint(
    tenant_id: str,
    run_id: str,
    upload_id: str,
    db: DbSession,
    user: CurrentUser,
    _a: TenantMembership = Depends(require_tenant_analyst_or_admin),
) -> None:
    await service.delete_upload(db, tenant_id, run_id, upload_id, user.id)


@router.post("/{tenant_id}/runs/{run_id}/validate")
async def validate_files_endpoint(
    request: Request,
    tenant_id: str,
    run_id: str,
    body: ValidateRunRequest,
    db: DbSession,
    user: CurrentUser,
    scope: str | None = Query(default=None),
    _a: TenantMembership = Depends(require_tenant_analyst_or_admin),
) -> JSONResponse:
    # One lane: PD, then LGD, then EAD, each validated on its own. The work runs
    # as a background job (it parses whole workbooks and, for EAD, previews the
    # full ECL); this request only records it, so it can never time out.
    from ledgerline.apps.runs.service import _get_run
    from ledgerline.apps.runs.validation_jobs import create_job

    scope_key = (scope or "").strip().lower()
    await _get_run(db, tenant_id, run_id)
    job_id, created = await create_job(db, tenant_id=tenant_id, run_id=run_id, scope=scope_key)
    await db.commit()
    _log.info("validation_job_requested", run_id=run_id, scope=scope_key, job_id=job_id, created=created)
    if created:
        await validate.defer_async(
            job_id=job_id,
            tenant_id=tenant_id,
            run_id=run_id,
            user_id=user.id,
            scope=scope_key,
            ip=get_client_ip(request.headers.get("X-Forwarded-For")),
            user_agent=get_user_agent(request.headers.get("User-Agent")),
        )
    return JSONResponse(status_code=202, content={"data": {"jobId": job_id, "status": "queued"}})


@router.get("/{tenant_id}/runs/{run_id}/validation-jobs/{job_id}")
async def validation_job_endpoint(
    tenant_id: str,
    run_id: str,
    job_id: str,
    db: DbSession,
    user: CurrentUser,
    _m: TenantMembership = Depends(require_tenant_member),
) -> JSONResponse:
    from ledgerline.apps.runs.validation_jobs import get_job

    job = await get_job(db, tenant_id=tenant_id, run_id=run_id, job_id=job_id)
    return JSONResponse(content={"data": job}, headers={"Cache-Control": "no-store"})


@router.post("/{tenant_id}/runs/{run_id}/validation/{kind}/accept")
async def accept_review_endpoint(
    tenant_id: str,
    run_id: str,
    kind: str,
    db: DbSession,
    user: CurrentUser,
    _a: TenantMembership = Depends(require_tenant_analyst_or_admin),
) -> AcceptReviewResponse:
    return AcceptReviewResponse(data=await ecl_validation.accept_review(db, tenant_id, run_id, user.id, kind))


@router.get("/{tenant_id}/runs/{run_id}/ecl/preview-export")
async def ecl_preview_export_endpoint(
    request: Request,
    tenant_id: str,
    run_id: str,
    db: DbSession,
    user: CurrentUser,
    _m: TenantMembership = Depends(require_tenant_member),
) -> Response:
    content, filename = await ecl_validation.build_ecl_preview_export(db, tenant_id, run_id)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _enforce_run_quota(db: Any, tenant_id: str, run_id: str, request: Request) -> None:
    """Entitlement limit `runs_month` (from the web token): computed runs this calendar month, failed ones excluded."""
    from datetime import UTC, datetime

    from sqlalchemy import func, select

    from ledgerline.apps.runs.models import Run
    from ledgerline.kernel.errors import ECLException

    lim = (((request.state.web_claims.get("svc") or {}).get("ecl") or {}).get("lim") or {}).get("runs_month")
    if not lim:
        return
    run = await db.get(Run, run_id)
    if run is not None and run.reporting_period == "Sample data":
        return  # sample runs never use quota
    month = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    used = await db.scalar(
        select(func.count())
        .select_from(Run)
        .where(
            Run.tenant_id == tenant_id,
            Run.id != run_id,
            Run.deleted_at.is_(None),
            Run.created_at >= month,
            Run.reporting_period.is_distinct_from("Sample data"),
            Run.status.in_(("queued", "pd_running", "lgd_running", "ead_running", "complete")),
        )
    )
    if (used or 0) >= int(lim):
        raise ECLException(
            "QUOTA_EXCEEDED",
            f"Your workspace has used its {lim} runs this month. Ask your admin to request more capacity.",
            409,
        )


@router.post(
    "/{tenant_id}/runs/{run_id}/execute",
    status_code=status.HTTP_202_ACCEPTED,
)
async def execute_run_endpoint(
    tenant_id: str,
    run_id: str,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    _a: TenantMembership = Depends(require_tenant_analyst_or_admin),
) -> ExecuteRunResponse:
    _log.info("execute_run_requested", run_id=run_id, tenant_id=tenant_id, user_id=user.id)
    await _enforce_run_quota(db, tenant_id, run_id, request)
    data = await service.execute_run(
        db,
        tenant_id,
        run_id,
        user.id,
        ip=get_client_ip(request.headers.get("X-Forwarded-For")),
        user_agent=get_user_agent(request.headers.get("User-Agent")),
    )
    await db.commit()
    if data.dispatch_task:
        # One calc job per run (queue lock = run id): retries or double clicks can't compute twice.
        await compute_run.configure(queueing_lock=run_id).defer_async(run_id=run_id)
        _log.info("compute_job_queued", run_id=run_id)
    else:
        _log.info("execute_run_idempotent_skip_dispatch", run_id=run_id, status=data.status)
    return ExecuteRunResponse(data=data)


@router.get("/{tenant_id}/runs/{run_id}")
async def get_run_endpoint(
    tenant_id: str,
    run_id: str,
    db: DbSession,
    user: CurrentUser,
    membership: TenantMembership = Depends(require_tenant_member),
    view: str = Query(default="full"),
) -> RunResponse:
    data = await service.get_run(
        db,
        tenant_id,
        run_id,
        user=user,
        membership=membership,
        view=view,
    )
    return RunResponse(data=data)


@router.get("/{tenant_id}/runs")
async def list_runs_endpoint(
    tenant_id: str,
    db: DbSession,
    user: CurrentUser,
    membership: TenantMembership = Depends(require_tenant_member),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    status: str | None = Query(default=None),
    search: str | None = Query(default=None),
) -> RunListResponse:
    items, meta = await service.list_runs(
        db,
        tenant_id,
        user=user,
        membership=membership,
        page=page,
        per_page=per_page,
        status=status,
        search=search,
    )
    return RunListResponse(data=items, meta=meta.model_dump())


@router.post("/{tenant_id}/runs/{run_id}/rerun", status_code=status.HTTP_201_CREATED)
async def rerun_run_endpoint(
    tenant_id: str,
    run_id: str,
    body: RerunRequest,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    _a: TenantMembership = Depends(require_tenant_analyst_or_admin),
) -> RunResponse:
    data = await service.rerun_run(
        db,
        tenant_id,
        run_id,
        user.id,
        body,
        ip=get_client_ip(request.headers.get("X-Forwarded-For")),
        user_agent=get_user_agent(request.headers.get("User-Agent")),
    )
    return RunResponse(data=data)


@router.delete("/{tenant_id}/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run_endpoint(
    tenant_id: str,
    run_id: str,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    _a: TenantMembership = Depends(require_tenant_admin),
) -> None:
    await service.delete_run(
        db,
        tenant_id,
        run_id,
        user.id,
        ip=get_client_ip(request.headers.get("X-Forwarded-For")),
        user_agent=get_user_agent(request.headers.get("User-Agent")),
    )


@router.get("/{tenant_id}/runs/{run_id}/inputs/bundle")
async def download_inputs_bundle_endpoint(
    tenant_id: str,
    run_id: str,
    db: DbSession,
    user: CurrentUser,
    _m: TenantMembership = Depends(require_tenant_member),
) -> Response:
    content, filename = await service.build_inputs_bundle(db, tenant_id, run_id)
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{tenant_id}/runs/{run_id}/inputs/{kind}/file")
async def download_input_file_endpoint(
    tenant_id: str,
    run_id: str,
    kind: str,
    db: DbSession,
    user: CurrentUser,
    _m: TenantMembership = Depends(require_tenant_member),
) -> Response:
    content, filename = await service.get_input_file_bytes(db, tenant_id, run_id, kind)
    # User-supplied names may hold quotes or non-Latin-1 characters; RFC 5987 form is safe.
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.get("/{tenant_id}/runs/{run_id}/downloads/bundle")
async def download_workbooks_bundle_endpoint(
    tenant_id: str,
    run_id: str,
    db: DbSession,
    user: CurrentUser,
    _m: TenantMembership = Depends(require_tenant_member),
) -> Response:
    content, filename = await results_service.build_workbooks_bundle(db, tenant_id, run_id)
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{tenant_id}/runs/{run_id}/pd/export")
async def pd_export_endpoint(
    request: Request,
    tenant_id: str,
    run_id: str,
    db: DbSession,
    user: CurrentUser,
    _m: TenantMembership = Depends(require_tenant_member),
) -> Response:
    # PD Calcs.xlsx straight from the PD uploads, before LGD/EAD exist.
    content, filename = await pd_validation.build_pd_export(db, tenant_id, run_id)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{tenant_id}/runs/{run_id}/downloads/{kind}/file")
async def download_artifact_file_endpoint(
    tenant_id: str,
    run_id: str,
    kind: str,
    db: DbSession,
    user: CurrentUser,
    _m: TenantMembership = Depends(require_tenant_member),
) -> Response:
    content, filename = await results_service.get_artifact_bytes(db, tenant_id, run_id, kind.upper())
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{tenant_id}/runs/{run_id}/downloads/{kind}")
async def download_artifact_endpoint(
    tenant_id: str,
    run_id: str,
    kind: str,
    db: DbSession,
    user: CurrentUser,
    _m: TenantMembership = Depends(require_tenant_member),
) -> PresignedDownloadResponse:
    url, expires_at = await results_service.get_presigned_download(db, tenant_id, run_id, kind.upper())
    from ledgerline.apps.runs.schemas import PresignedDownloadOut

    return PresignedDownloadResponse(data=PresignedDownloadOut(url=url, expires_at=expires_at))


@router.get("/{tenant_id}/templates/{kind}")
async def get_template_endpoint(
    tenant_id: str,
    kind: str,
    user: CurrentUser,
    _m: TenantMembership = Depends(require_tenant_member),
) -> FileResponse:
    kind_upper = kind.upper()
    if kind_upper not in {"PD", "LGD", "EAD"}:
        from ledgerline.kernel.errors import ECLException

        raise ECLException("INVALID_TEMPLATE_KIND", "Template kind must be PD, LGD, or EAD.", 400)
    path = _TEMPLATES_DIR / f"{kind_upper}.xlsx"
    if not path.is_file():
        from ledgerline.kernel.errors import ECLException

        raise ECLException("TEMPLATE_NOT_FOUND", f"Template {kind_upper} not found.", 404)
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"ECL_{kind_upper}_template.xlsx",
    )
