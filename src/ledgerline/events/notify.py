"""Engine → web events. The engine never notifies people directly: it appends to `engine.outbox_events`
in the caller's transaction and fires NOTIFY; the web app consumes the rows and applies each person's
notification preferences (in-app + email) through its own notify()."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, String, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from ledgerline.db import Base
from ledgerline.kernel.ids import new_ulid

CHANNEL = "engine_events"


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (Index("ix_outbox_events_pending", "consumed_at", "created_at"),)

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(26), index=True)
    type: Mapped[str] = mapped_column(String(64))
    # Notification fields the web app maps onto lib/notify-events.ts (event, kind, recipients, copy, link).
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


async def publish(db: AsyncSession, tenant_id: str, type_: str, payload: dict[str, Any]) -> None:
    """Durable event + live ping. NOTIFY is transactional in Postgres: delivered only if the caller commits."""
    db.add(OutboxEvent(id=new_ulid(), tenant_id=tenant_id, type=type_, payload=payload))
    await progress(
        db, tenant_id, {"type": type_, **{k: v for k, v in payload.items() if k in ("runId", "leaseId", "href")}}
    )


async def progress(db: AsyncSession, tenant_id: str, data: dict[str, Any]) -> None:
    """Live-only signal (run progress etc.) for SSE in the web app. Small payloads (NOTIFY limit 8 KB)."""
    import orjson

    await db.execute(
        text("SELECT pg_notify(:c, :p)"), {"c": CHANNEL, "p": orjson.dumps({"tenantId": tenant_id, **data}).decode()}
    )


async def notify(
    db: AsyncSession,
    *,
    tenant_id: str,
    user_ids: list[str] | str,
    event: str,
    kind: str,
    title: str,
    body: str,
    href: str | None = None,
    action: str | None = None,
    **extra: Any,
) -> None:
    """Ask the web app to notify people. `user_ids` may be '*' (all members) or '*reviewers'."""
    await publish(
        db,
        tenant_id,
        "notify",
        {
            "to": user_ids,
            "event": event,
            "kind": kind,
            "title": title[:255],
            "body": body,
            "href": href,
            "action": action,
            **extra,
        },
    )


async def notify_run_completed(db: AsyncSession, *, user_id: str, tenant_id: str, run_name: str, run_id: str) -> None:
    await notify(
        db,
        tenant_id=tenant_id,
        user_ids=[user_id],
        event="completed",
        kind="runs",
        title=f"{run_name} finished",
        body="Results are ready to review.",
        href=f"/ecl/runs/{run_id}/summary",
        action="Open",
        runId=run_id,
    )
    # Reviewers (and admins) learn there's a run awaiting sign-off; the web app drops the preparer from this list.
    await notify(
        db,
        tenant_id=tenant_id,
        user_ids="*reviewers",
        event="signoff",
        kind="runs",
        title=f"{run_name} is ready for sign-off",
        body="A completed run is awaiting your review.",
        href=f"/ecl/runs/{run_id}/summary",
        action="Review",
        runId=run_id,
        exclude=[user_id],
    )


async def notify_run_failed(db: AsyncSession, *, user_id: str, tenant_id: str, run_name: str, run_id: str) -> None:
    await notify(
        db,
        tenant_id=tenant_id,
        user_ids=[user_id],
        event="failed",
        kind="runs",
        title=f"{run_name} failed",
        body="The calculation stopped. Open the run to see why.",
        href=f"/ecl/runs/{run_id}/summary",
        action="Open",
        runId=run_id,
    )
