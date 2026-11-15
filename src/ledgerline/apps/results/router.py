from fastapi import APIRouter, Depends, Query

from ledgerline.apps.results import service
from ledgerline.apps.results.schemas import (
    DashboardResponse,
    LoanResponse,
    PortfolioResponse,
    SegmentResponse,
)
from ledgerline.apps.runs.schemas import EclReportResponse, ExceptionsPageResponse
from ledgerline.auth.deps import CurrentUser, DbSession, require_tenant_member
from ledgerline.auth.deps import Membership as TenantMembership

router = APIRouter(prefix="/tenants", tags=["results"])


@router.get("/{tenant_id}/dashboard")
async def get_dashboard_endpoint(
    tenant_id: str,
    db: DbSession,
    user: CurrentUser,
    membership: TenantMembership = Depends(require_tenant_member),
) -> DashboardResponse:
    data = await service.get_dashboard(db, tenant_id, user=user, membership=membership)
    return DashboardResponse(data=data)


@router.get("/{tenant_id}/results")
async def get_portfolio_endpoint(
    tenant_id: str,
    db: DbSession,
    user: CurrentUser,
    membership: TenantMembership = Depends(require_tenant_member),
    run_id: str | None = Query(default=None),
    stage: list[str] | None = Query(default=None, description="Repeat for several: 'Stage 1', 'Stage 2', 'Stage 3'"),
    min_ecl: float | None = Query(default=None, ge=0),
    q: str | None = Query(default=None, max_length=100, description="Loan or customer ID contains"),
) -> PortfolioResponse:
    data = await service.get_portfolio(
        db, tenant_id, run_id, user=user, membership=membership, stages=stage, min_ecl=min_ecl, q=q
    )
    return PortfolioResponse(data=data)


@router.get("/{tenant_id}/results/segments/{segment_name}")
async def get_segment_endpoint(
    tenant_id: str,
    segment_name: str,
    db: DbSession,
    user: CurrentUser,
    membership: TenantMembership = Depends(require_tenant_member),
    run_id: str | None = Query(default=None),
    stage: list[str] | None = Query(default=None, description="Repeat for several: 'Stage 1', 'Stage 2', 'Stage 3'"),
    min_ecl: float | None = Query(default=None, ge=0),
    q: str | None = Query(default=None, max_length=100, description="Loan or customer ID contains"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
) -> SegmentResponse:
    data = await service.get_segment(
        db,
        tenant_id,
        segment_name,
        run_id=run_id,
        stages=stage,
        min_ecl=min_ecl,
        q=q,
        page=page,
        per_page=per_page,
        user=user,
        membership=membership,
    )
    return SegmentResponse(data=data)


@router.get("/{tenant_id}/results/loans/{loan_id}")
async def get_loan_endpoint(
    tenant_id: str,
    loan_id: str,
    db: DbSession,
    user: CurrentUser,
    membership: TenantMembership = Depends(require_tenant_member),
    run_id: str | None = Query(default=None),
) -> LoanResponse:
    data = await service.get_loan(
        db, tenant_id, loan_id, run_id=run_id, user=user, membership=membership
    )
    return LoanResponse(data=data)


@router.get("/{tenant_id}/runs/{run_id}/ecl-report")
async def get_ecl_report_endpoint(
    tenant_id: str,
    run_id: str,
    db: DbSession,
    user: CurrentUser,
    membership: TenantMembership = Depends(require_tenant_member),
) -> EclReportResponse:
    data = await service.get_ecl_report(db, tenant_id, run_id, user=user, membership=membership)
    return EclReportResponse(data=data)


@router.get("/{tenant_id}/runs/{run_id}/exceptions")
async def list_run_exceptions_endpoint(
    tenant_id: str,
    run_id: str,
    db: DbSession,
    user: CurrentUser,
    membership: TenantMembership = Depends(require_tenant_member),
    category: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
) -> ExceptionsPageResponse:
    data = await service.list_run_exceptions(
        db, tenant_id, run_id, category=category, page=page, per_page=per_page,
        user=user, membership=membership,
    )
    return ExceptionsPageResponse(data=data)
