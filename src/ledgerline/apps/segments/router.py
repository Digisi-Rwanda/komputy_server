from fastapi import APIRouter, Depends, status

from ledgerline.apps.segments import service
from ledgerline.apps.segments.schemas import (
    BatchCreateSegmentsRequest,
    CreateSegmentRequest,
    SegmentListResponse,
    SegmentResponse,
    UpdateSegmentRequest,
)
from ledgerline.auth.deps import CurrentUser, DbSession, require_tenant_admin, require_tenant_member
from ledgerline.auth.deps import Membership as TenantMembership

router = APIRouter(prefix="/tenants", tags=["segments"])


@router.get("/{tenant_id}/segments")
async def list_segments_endpoint(
    tenant_id: str,
    db: DbSession,
    user: CurrentUser,
    _m: TenantMembership = Depends(require_tenant_member),
) -> SegmentListResponse:
    data = await service.list_segments(db, tenant_id)
    return SegmentListResponse(data=data)


@router.post("/{tenant_id}/segments", status_code=status.HTTP_201_CREATED)
async def create_segment_endpoint(
    tenant_id: str,
    body: CreateSegmentRequest,
    db: DbSession,
    user: CurrentUser,
    _a: TenantMembership = Depends(require_tenant_admin),
) -> SegmentResponse:
    data = await service.create_segment(db, tenant_id, user.id, body)
    return SegmentResponse(data=data)


@router.post("/{tenant_id}/segments/batch", status_code=status.HTTP_201_CREATED)
async def batch_create_segments_endpoint(
    tenant_id: str,
    body: BatchCreateSegmentsRequest,
    db: DbSession,
    user: CurrentUser,
    _a: TenantMembership = Depends(require_tenant_admin),
) -> SegmentListResponse:
    data = await service.batch_create_segments(db, tenant_id, user.id, body)
    return SegmentListResponse(data=data)


@router.patch("/{tenant_id}/segments/{segment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def update_segment_endpoint(
    tenant_id: str,
    segment_id: str,
    body: UpdateSegmentRequest,
    db: DbSession,
    user: CurrentUser,
    _a: TenantMembership = Depends(require_tenant_admin),
) -> None:
    await service.update_segment(db, tenant_id, segment_id, body, user.id)


@router.delete("/{tenant_id}/segments/{segment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_segment_endpoint(
    tenant_id: str,
    segment_id: str,
    db: DbSession,
    user: CurrentUser,
    _a: TenantMembership = Depends(require_tenant_admin),
) -> None:
    await service.delete_segment(db, tenant_id, segment_id)
