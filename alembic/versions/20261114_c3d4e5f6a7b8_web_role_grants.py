"""Let the web app's database role consume engine events (and nothing else in `engine`).

Set WEB_DB_ROLE to the web app's Postgres role when running migrations in an environment where the two apps use
different roles (production). No-op when unset (local dev uses one superuser).

Revision ID: c3d4e5f6a7b8
Revises: b1c2d3e4f5a6
"""
import os
from collections.abc import Sequence

from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    role = os.environ.get("WEB_DB_ROLE")
    if not role:
        return
    q = '"' + role.replace('"', "") + '"'
    op.execute(f"GRANT USAGE ON SCHEMA engine TO {q}")
    op.execute(f"GRANT SELECT, UPDATE (consumed_at) ON engine.outbox_events TO {q}")


def downgrade() -> None:
    role = os.environ.get("WEB_DB_ROLE")
    if role:
        q = '"' + role.replace('"', "") + '"'
        op.execute(f"REVOKE ALL ON engine.outbox_events FROM {q}")
        op.execute(f"REVOKE USAGE ON SCHEMA engine FROM {q}")
