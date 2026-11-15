"""Validation as a background job, tracked in Postgres.

PD, LGD and EAD validation parse whole workbooks and (for EAD) run the full ECL preview, so the request only
records a job; a worker runs it and stores the result; the web app streams progress (NOTIFY) or polls.

One active job per (run, scope): a double click or a retried request joins the running job (partial unique index).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import JSON, DateTime, Index, String, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from ledgerline.db import AsyncSessionLocal, Base
from ledgerline.events.notify import progress
from ledgerline.kernel.errors import ECLException
from ledgerline.kernel.ids import new_ulid
from ledgerline.log import get_logger

log = get_logger("runs.validation_jobs")

SCOPES = ("pd", "lgd", "ead")
# A job still "active" after this long is treated as abandoned (worker killed); a new request replaces it.
ACTIVE_TTL = timedelta(minutes=30)


class ValidationJob(Base):
    __tablename__ = "validation_jobs"
    __table_args__ = (
        Index(
            "uq_validation_jobs_active",
            "run_id",
            "scope",
            unique=True,
            postgresql_where=text("status IN ('queued','running')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(26), index=True)
    run_id: Mapped[str] = mapped_column(String(26), index=True)
    scope: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16))  # queued | running | complete | error
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def _out(j: ValidationJob) -> dict[str, Any]:
    d: dict[str, Any] = {"jobId": j.id, "runId": j.run_id, "scope": j.scope, "status": j.status}
    if j.result is not None:
        d["result"] = j.result
    if j.error is not None:
        d["error"] = j.error
    return d


async def create_job(db: AsyncSession, *, tenant_id: str, run_id: str, scope: str) -> tuple[str, bool]:
    """Return (job_id, created). Joins the active job for this run and scope if there is one."""
    if scope not in SCOPES:
        raise ECLException("INVALID_SCOPE", "scope must be pd, lgd or ead.", 400)
    active = (
        await db.execute(
            select(ValidationJob).where(
                ValidationJob.run_id == run_id,
                ValidationJob.scope == scope,
                ValidationJob.status.in_(("queued", "running")),
            )
        )
    ).scalar_one_or_none()
    if active and active.updated_at > datetime.now(UTC) - ACTIVE_TTL:
        return active.id, False
    if active:  # abandoned
        active.status, active.error = (
            "error",
            {"code": "ABANDONED", "detail": "Validation was interrupted.", "status": 500},
        )
        await db.flush()
    job = ValidationJob(id=new_ulid(), tenant_id=tenant_id, run_id=run_id, scope=scope, status="queued")
    db.add(job)
    try:
        await db.flush()
    except IntegrityError:  # a concurrent request won the race: join it
        await db.rollback()
        return await create_job(db, tenant_id=tenant_id, run_id=run_id, scope=scope)
    return job.id, True


async def get_job(db: AsyncSession, *, tenant_id: str, run_id: str, job_id: str) -> dict[str, Any]:
    j = (
        await db.execute(
            select(ValidationJob).where(
                ValidationJob.id == job_id, ValidationJob.tenant_id == tenant_id, ValidationJob.run_id == run_id
            )
        )
    ).scalar_one_or_none()
    if not j:
        raise ECLException("JOB_NOT_FOUND", "Validation job not found; validate again.", 404)
    return _out(j)


async def _set(job_id: str, tenant_id: str, run_id: str, scope: str, **values: Any) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(ValidationJob).where(ValidationJob.id == job_id).values(updated_at=func.now(), **values)
        )
        await progress(
            db,
            tenant_id,
            {"type": "validation", "runId": run_id, "scope": scope, "jobId": job_id, "status": values.get("status")},
        )
        await db.commit()


async def run_job(
    *,
    job_id: str,
    tenant_id: str,
    run_id: str,
    user_id: str,
    scope: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Execute one validation and store its outcome. Never raises."""
    from ledgerline.apps.runs import ecl_validation, pd_validation

    run = {
        "pd": pd_validation.validate_pd_files,
        "lgd": ecl_validation.validate_lgd_files,
        "ead": ecl_validation.validate_ead_files,
    }[scope]
    await _set(job_id, tenant_id, run_id, scope, status="running")
    try:
        async with AsyncSessionLocal() as db:
            result = await run(db, tenant_id, run_id, user_id, ip=ip, user_agent=user_agent)
            await db.commit()
        await _set(job_id, tenant_id, run_id, scope, status="complete", result=result.model_dump(mode="json"))
        log.info("validation_job_complete", job_id=job_id, run_id=run_id, scope=scope, status=result.status)
    except ECLException as exc:
        await _set(
            job_id,
            tenant_id,
            run_id,
            scope,
            status="error",
            error={"code": exc.code, "detail": exc.detail, "status": exc.status_code},
        )
        log.info("validation_job_rejected", job_id=job_id, run_id=run_id, scope=scope, code=exc.code)
    except Exception as exc:
        await _set(
            job_id,
            tenant_id,
            run_id,
            scope,
            status="error",
            error={
                "code": "VALIDATION_FAILED",
                "detail": "Validation failed unexpectedly. Try again; if it repeats, contact support.",
                "status": 500,
            },
        )
        log.error("validation_job_failed", job_id=job_id, run_id=run_id, scope=scope, exc_info=exc)
