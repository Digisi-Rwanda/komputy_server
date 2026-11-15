"""Onboarding: a run pre-loaded with sample workbooks (the engine's own test portfolio), so a new workspace can
validate and compute end to end in minutes. Flagged as a sample: it never counts towards quota."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from ledgerline.apps.runs import service
from ledgerline.apps.runs.schemas import CreateRunRequest
from ledgerline.auth.deps import (
    CurrentUser,
    DbSession,
    Membership,
    get_client_ip,
    get_user_agent,
    require_tenant_analyst_or_admin,
)

router = APIRouter(prefix="/tenants", tags=["runs"])
SAMPLES = Path(__file__).resolve().parents[2] / "ecl" / "samples"
FILES = [
    ("PD", "PD_2025_Jan_Feb.xlsx"),
    ("PD", "PD_2025_Mar_Apr.xlsx"),
    ("LGD", "LGD_2025_03.xlsx"),
    ("EAD", "EAD_2025_03.xlsx"),
]
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.post("/{tenant_id}/ecl/sample-run", status_code=201)
async def sample_run(
    tenant_id: str,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    m: Annotated[Membership, Depends(require_tenant_analyst_or_admin)],
) -> dict[str, Any]:
    run = await service.create_run(
        db,
        tenant_id,
        user.id,
        CreateRunRequest(name="Sample run · Mar 2025", reporting_period="Sample data"),
        ip=get_client_ip(request.headers.get("X-Forwarded-For")),
        user_agent=get_user_agent(request.headers.get("User-Agent")),
    )
    rid = run.fullId
    for kind, name in FILES:
        await service.upload_file(
            db,
            tenant_id,
            rid,
            user.id,
            kind=kind,
            filename=name,
            file_obj=io.BytesIO((SAMPLES / name).read_bytes()),
            content_type=XLSX,
        )
    return {"data": {"fullId": rid}}
