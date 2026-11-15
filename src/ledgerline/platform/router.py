"""Platform-facing endpoints: engine versions (staff), usage (workspace), ops (service token from the web app)."""

from __future__ import annotations

import hmac
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from ledgerline.auth.deps import DbSession, Membership, PlatformAdmin, require_tenant_member
from ledgerline.config import get_settings
from ledgerline.ecl.domain import ENGINE_VERSION as ECL_VERSION
from ledgerline.kernel.errors import ECLException
from ledgerline.leases.domain.engine import ENGINE_VERSION as LEASES_VERSION
from ledgerline.platform.models import EngineVersion

router = APIRouter(prefix="/api/v1", tags=["platform"])


def require_service(authorization: Annotated[str | None, Header()] = None) -> None:
    want = f"Bearer {get_settings().service_token}"
    if not authorization or not hmac.compare_digest(authorization, want):
        raise ECLException("UNAUTHORIZED", "Service token required.", 401)


@router.get("/platform/engine-versions")
async def list_versions(db: DbSession, _u: PlatformAdmin) -> dict[str, Any]:
    rows = (await db.execute(select(EngineVersion).order_by(EngineVersion.created_at.desc()))).scalars().all()
    return {
        "data": {
            "running": {"ecl": ECL_VERSION, "leases": LEASES_VERSION},
            "versions": [
                {
                    "version": r.version,
                    "service": r.service,
                    "status": r.status,
                    "notes": r.notes,
                    "createdAt": r.created_at.isoformat(),
                    "promotedAt": r.promoted_at.isoformat() if r.promoted_at else None,
                }
                for r in rows
            ],
        }
    }


class Promote(BaseModel):
    service: str = Field(pattern="^(ecl|leases)$")
    version: str = Field(min_length=1, max_length=32)


@router.post("/platform/engine-versions/promote")
async def promote(body: Promote, db: DbSession, user: PlatformAdmin) -> dict[str, Any]:
    row = await db.get(EngineVersion, (body.version, body.service))
    if not row:
        raise ECLException("NOT_FOUND", "Unknown engine version.", 404)
    await db.execute(
        update(EngineVersion)
        .where(EngineVersion.service == body.service, EngineVersion.status == "promoted")
        .values(status="available")
    )
    row.status, row.promoted_at, row.promoted_by = "promoted", datetime.now(UTC), user.id
    return {"data": {"service": body.service, "version": body.version}}


@router.get("/tenants/{tenant_id}/usage")
async def usage(
    tenant_id: str, db: DbSession, _m: Annotated[Membership, Depends(require_tenant_member)]
) -> dict[str, Any]:
    """Quota meters for the web app: ECL runs this month, loans in the latest run, active leases."""
    from ledgerline.apps.leases.models import LeaseVersion
    from ledgerline.apps.results.models import EclLoanSummary
    from ledgerline.apps.runs.models import Run

    month = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    runs = (
        await db.scalar(
            select(func.count())
            .select_from(Run)
            .where(Run.tenant_id == tenant_id, Run.created_at >= month, Run.deleted_at.is_(None))
        )
        or 0
    )
    last_run = (
        await db.execute(
            select(Run.id)
            .where(Run.tenant_id == tenant_id, Run.status == "complete", Run.deleted_at.is_(None))
            .order_by(Run.created_at.desc())
            .limit(1)
        )
    ).scalar()
    last = (
        await db.scalar(select(func.count()).select_from(EclLoanSummary).where(EclLoanSummary.run_id == last_run))
        if last_run
        else 0
    )
    leases = (
        await db.scalar(
            select(func.count(func.distinct(LeaseVersion.lease_id))).where(
                LeaseVersion.tenant_id == tenant_id, LeaseVersion.status == "approved"
            )
        )
        or 0
    )
    return {
        "data": {"ecl": {"runsThisMonth": int(runs), "loansLastRun": int(last or 0)}, "leases": {"active": int(leases)}}
    }


@router.post("/ops/tenants/{tenant_id}/purge", dependencies=[Depends(require_service)], status_code=202)
async def purge(tenant_id: str) -> dict[str, Any]:
    """Called by the web app after a closed workspace's 30-day window: delete every engine row + stored file."""
    from ledgerline.platform.jobs import purge_tenant

    await purge_tenant.configure(queueing_lock=f"purge:{tenant_id}").defer_async(tenant_id=tenant_id)
    return {"data": {"queued": True}}
