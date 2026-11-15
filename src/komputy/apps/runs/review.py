"""Run review lifecycle (maker/checker), new in the rebuild.

computed (status=complete) → needs_review → returned (preparer fixes and reruns) | signed_off (locked, immutable)
A signed-off run is the statutory figure: it can't be deleted, edited or recomputed; a rerun creates a new run that
supersedes it once that one is signed off (the old one is then archived). Reviewers can never sign off a run they
prepared (segregation of duties).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from komputy.apps.runs.models import Run, RunReviewEvent
from komputy.auth.deps import CurrentUser, DbSession, Membership, require_tenant_member
from komputy.events.audit_service import log_event
from komputy.events.notify import notify
from komputy.kernel.errors import ECLException
from komputy.kernel.ids import new_ulid
from komputy.web import User

router = APIRouter(prefix="/tenants", tags=["review"])
Member = Annotated[Membership, Depends(require_tenant_member)]


def review_state(run: Run) -> str | None:
    if run.status != "complete":
        return None
    return run.review_status or "needs_review"


def assert_mutable(run: Run) -> None:
    """Guard for every write to a run (delete, rename, uploads, recompute)."""
    if run.locked_at is not None:
        raise ECLException("RUN_LOCKED", "This run is signed off and locked. Start a rerun to change anything.", 409)


async def _run(db: AsyncSession, tenant_id: str, run_id: str) -> Run:
    run = (
        await db.execute(
            select(Run).where(Run.id == run_id, Run.tenant_id == tenant_id, Run.deleted_at.is_(None)).with_for_update()
        )
    ).scalar_one_or_none()
    if not run:
        raise ECLException("RUN_NOT_FOUND", "Run not found.", 404)
    return run


def _require_reviewer(m: Membership, run: Run, user_id: str) -> None:
    if m.role not in ("reviewer", "administrator"):
        raise ECLException("INSUFFICIENT_ROLE", "Only reviewers can do this.", 403)
    if run.created_by_user_id == user_id:
        raise ECLException("SEGREGATION_OF_DUTIES", "You can't review or sign off a run you prepared.", 403)


async def _event(db: AsyncSession, run: Run, user_id: str, action: str, comment: str | None) -> None:
    db.add(
        RunReviewEvent(
            id=new_ulid(), run_id=run.id, tenant_id=run.tenant_id, user_id=user_id, action=action, comment=comment
        )
    )
    await db.flush()
    await log_event(
        db,
        f"run.{action}",
        user_id=user_id,
        tenant_id=run.tenant_id,
        details={"run_id": run.id, "description": run.name, "comment": comment},
    )


class Decision(BaseModel):
    comment: str | None = Field(default=None, max_length=2000)


class ReturnDecision(BaseModel):
    comment: str = Field(min_length=3, max_length=2000)


async def _history(db: AsyncSession, run: Run) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(RunReviewEvent, User.name)
            .outerjoin(User, User.id == RunReviewEvent.user_id)
            .where(RunReviewEvent.run_id == run.id)
            .order_by(RunReviewEvent.created_at)
        )
    ).all()
    return {
        "runId": run.id,
        "state": review_state(run),
        "lockedAt": run.locked_at.isoformat() if run.locked_at else None,
        "signedOffAt": run.signed_off_at.isoformat() if run.signed_off_at else None,
        "reviewedBy": run.reviewed_by_user_id,
        "preparedBy": run.created_by_user_id,
        "events": [
            {
                "id": e.id,
                "action": e.action,
                "comment": e.comment,
                "by": name or e.user_id,
                "byId": e.user_id,
                "at": e.created_at.isoformat(),
            }
            for e, name in rows
        ],
    }


@router.get("/{tenant_id}/runs/{run_id}/review")
async def get_review(tenant_id: str, run_id: str, db: DbSession, _m: Member) -> dict[str, Any]:
    run = (await db.execute(select(Run).where(Run.id == run_id, Run.tenant_id == tenant_id))).scalar_one_or_none()
    if not run:
        raise ECLException("RUN_NOT_FOUND", "Run not found.", 404)
    return {"data": await _history(db, run)}


@router.post("/{tenant_id}/runs/{run_id}/review/comment")
async def comment(
    tenant_id: str, run_id: str, body: ReturnDecision, db: DbSession, user: CurrentUser, m: Member
) -> dict[str, Any]:
    run = await _run(db, tenant_id, run_id)
    if m.role == "viewer":
        raise ECLException("INSUFFICIENT_ROLE", "Viewers can't comment.", 403)
    await _event(db, run, user.id, "comment", body.comment)
    return {"data": await _history(db, run)}


@router.post("/{tenant_id}/runs/{run_id}/review/return")
async def return_run(
    tenant_id: str, run_id: str, body: ReturnDecision, db: DbSession, user: CurrentUser, m: Member
) -> dict[str, Any]:
    run = await _run(db, tenant_id, run_id)
    _require_reviewer(m, run, user.id)
    if review_state(run) != "needs_review":
        raise ECLException("INVALID_STATE", "Only runs awaiting review can be returned.", 409)
    run.review_status, run.reviewed_by_user_id = "returned", user.id
    await _event(db, run, user.id, "returned", body.comment)
    await notify(
        db,
        tenant_id=tenant_id,
        user_ids=[run.created_by_user_id],
        event="signoff",
        kind="runs",
        title=f"{run.name} was returned",
        body=f"{user.name}: “{body.comment}”",
        href=f"/ecl/runs/{run.id}/summary",
        action="Open",
        runId=run.id,
    )
    return {"data": await _history(db, run)}


@router.post("/{tenant_id}/runs/{run_id}/review/resubmit")
async def resubmit(
    tenant_id: str, run_id: str, body: Decision, db: DbSession, user: CurrentUser, m: Member
) -> dict[str, Any]:
    run = await _run(db, tenant_id, run_id)
    if m.role not in ("analyst", "reviewer", "administrator"):
        raise ECLException("INSUFFICIENT_ROLE", "Only preparers can resubmit.", 403)
    if review_state(run) != "returned":
        raise ECLException("INVALID_STATE", "Only returned runs can be resubmitted.", 409)
    run.review_status = "needs_review"
    await _event(db, run, user.id, "submitted", body.comment)
    await notify(
        db,
        tenant_id=tenant_id,
        user_ids="*reviewers",
        event="signoff",
        kind="runs",
        title=f"{run.name} is ready for sign-off",
        body=f"Resubmitted by {user.name}",
        href=f"/ecl/runs/{run.id}/summary",
        action="Review",
        runId=run.id,
    )
    return {"data": await _history(db, run)}


@router.post("/{tenant_id}/runs/{run_id}/review/sign-off")
async def sign_off(
    tenant_id: str, run_id: str, body: Decision, db: DbSession, user: CurrentUser, m: Member
) -> dict[str, Any]:
    run = await _run(db, tenant_id, run_id)
    _require_reviewer(m, run, user.id)
    if review_state(run) != "needs_review":
        raise ECLException("INVALID_STATE", "Only runs awaiting review can be signed off.", 409)
    now = datetime.now(UTC)
    run.review_status, run.reviewed_by_user_id, run.signed_off_at, run.locked_at = "signed_off", user.id, now, now
    await _event(db, run, user.id, "signed_off", body.comment)
    if run.supersedes_run_id:  # the rerun replaces the earlier statutory figure
        prev = (
            await db.execute(select(Run).where(Run.id == run.supersedes_run_id, Run.tenant_id == tenant_id))
        ).scalar_one_or_none()
        if prev and prev.review_status == "signed_off":
            prev.review_status = "archived"
            await _event(db, prev, user.id, "archived", f"Superseded by {run.name}")
    await notify(
        db,
        tenant_id=tenant_id,
        user_ids=[run.created_by_user_id],
        event="signoff",
        kind="runs",
        title=f"{run.name} was signed off",
        body=f"Signed off by {user.name}. Figures are locked.",
        href=f"/ecl/runs/{run.id}/summary",
        action="Open",
        runId=run.id,
    )
    return {"data": await _history(db, run)}
