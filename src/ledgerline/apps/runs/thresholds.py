"""ECL validation thresholds per workspace (PD criteria bounds). Read by every PD validation; editable by admins and
reviewers, audited with before/after values. Defaults are the platform baseline (validators.pd_criteria.PdThresholds)."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator

from ledgerline.apps.runs.pd_validation import resolve_pd_thresholds
from ledgerline.auth.deps import CurrentUser, DbSession, Membership, require_tenant_member
from ledgerline.ecl.domain.validators.pd_criteria import PdThresholds
from ledgerline.events.audit_service import log_event
from ledgerline.kernel.errors import ECLException

router = APIRouter(prefix="/tenants", tags=["setup"])
Member = Annotated[Membership, Depends(require_tenant_member)]
FIELDS = (
    "max_month_gap",
    "min_transitions",
    "stage3_share_min",
    "stage3_share_max",
    "cure_rate_min",
    "cure_rate_max",
    "offbooks_abs_band",
)


def _out(row: Any) -> dict[str, Any]:
    return {"values": {f: float(getattr(row, f)) for f in FIELDS}, "defaults": asdict(PdThresholds())}


class ThresholdsIn(BaseModel):
    max_month_gap: int = Field(ge=0, le=24)
    min_transitions: int = Field(ge=0, le=1_000_000)
    stage3_share_min: float = Field(ge=0, le=1)
    stage3_share_max: float = Field(ge=0, le=1)
    cure_rate_min: float = Field(ge=0, le=1)
    cure_rate_max: float = Field(ge=0, le=1)
    offbooks_abs_band: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _bands(self) -> ThresholdsIn:
        if self.stage3_share_min > self.stage3_share_max or self.cure_rate_min > self.cure_rate_max:
            raise ValueError("Minimum must not exceed maximum.")
        return self


@router.get("/{tenant_id}/ecl/thresholds")
async def get_thresholds(tenant_id: str, db: DbSession, _m: Member) -> dict[str, Any]:
    _, row = await resolve_pd_thresholds(db, tenant_id)
    return {"data": _out(row)}


@router.put("/{tenant_id}/ecl/thresholds")
async def put_thresholds(
    tenant_id: str, body: ThresholdsIn, db: DbSession, user: CurrentUser, m: Member
) -> dict[str, Any]:
    if m.role not in ("administrator", "reviewer"):
        raise ECLException("INSUFFICIENT_ROLE", "Only reviewers and administrators can change thresholds.", 403)
    _, row = await resolve_pd_thresholds(db, tenant_id)
    before = {f: float(getattr(row, f)) for f in FIELDS}
    for f in FIELDS:
        v = getattr(body, f)
        setattr(row, f, Decimal(str(v)) if isinstance(v, float) else v)
    db.add(row)
    await db.flush()
    await log_event(
        db,
        "ecl.thresholds_changed",
        user_id=user.id,
        tenant_id=tenant_id,
        details={"before": before, "after": body.model_dump()},
    )
    return {"data": _out(row)}
