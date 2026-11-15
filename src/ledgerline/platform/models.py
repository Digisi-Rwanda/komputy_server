from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from ledgerline.db import Base


class EngineVersion(Base):
    """Registry of calculation engine releases. Workspaces run the promoted version unless pinned (web: tenants.engine_version_pin)."""

    __tablename__ = "engine_versions"
    version: Mapped[str] = mapped_column(String(32), primary_key=True)
    service: Mapped[str] = mapped_column(String(16), primary_key=True)  # ecl | leases
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), server_default="available")  # available | promoted | retired
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    promoted_by: Mapped[str | None] = mapped_column(String(26))
