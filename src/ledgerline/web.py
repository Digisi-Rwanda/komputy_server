"""Read-only views of tables owned by the web app (Next.js). The engine never migrates or writes these,
except `audit_logs` (append-only, shared so one audit trail covers both sides)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, MetaData, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ledgerline.config import get_settings


class WebBase(DeclarativeBase):
    metadata = MetaData(schema=get_settings().web_schema)


class User(WebBase):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    email: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean)
    platform_role: Mapped[str | None] = mapped_column(String(16))

    @property
    def is_platform_admin(self) -> bool:
        return self.platform_role == "admin"


class Tenant(WebBase):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))
    currency: Mapped[str] = mapped_column(String(8))
    timezone: Mapped[str] = mapped_column(String(64))
    reporting_cadence: Mapped[str] = mapped_column(String(16))
    fiscal_year_end: Mapped[str] = mapped_column(String(5))
    authz_version: Mapped[int] = mapped_column(Integer)
    engine_version_pin: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TenantMembership(WebBase):
    __tablename__ = "tenant_memberships"
    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(26))
    tenant_id: Mapped[str] = mapped_column(String(26))
    role: Mapped[str] = mapped_column(String(32))
    workspace_role: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32))


class MemberServiceRole(WebBase):
    __tablename__ = "member_service_roles"
    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    membership_id: Mapped[str] = mapped_column(String(26))
    service: Mapped[str] = mapped_column(String(16))
    role: Mapped[str] = mapped_column(String(16))


def user_initials(name: str | None) -> str:
    parts = (name or "").split()
    return ("".join(p[0] for p in parts[:2]) or "U").upper()


class UserNotification(WebBase):
    """The web app's notification inbox (rows are written by the web app's outbox consumer). The lease inbox
    reads its own `kind = 'leases'` rows and can mark them read."""

    __tablename__ = "user_notifications"
    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(26))
    tenant_id: Mapped[str] = mapped_column(String(26))
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    href: Mapped[str | None] = mapped_column(Text)
    is_read: Mapped[bool] = mapped_column(Boolean)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
