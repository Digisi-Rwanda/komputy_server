"""Row-level security on every tenant-scoped engine table (defence in depth).

API requests set `app.tenant_id` (SET LOCAL) after checking the token; queries then can't see another tenant's rows
even if a filter is forgotten. Background jobs (unset) work across tenants by design.

Revision ID: b1c2d3e4f5a6
Revises: a234b0c5b9a0
"""
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "a234b0c5b9a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY = "tenant_isolation"
# Bulk-loaded result tables (Postgres refuses COPY under forced RLS). They're only read through run-scoped,
# tenant-checked queries; everything else is forced.
COPY_TABLES = {"pd_results", "lgd_results", "ead_results", "ecl_loan_summaries", "run_exceptions"}


def _tables() -> list[str]:
    rows = op.get_bind().execute(text(
        "SELECT table_name FROM information_schema.columns WHERE table_schema = 'engine' AND column_name = 'tenant_id'"
    ))
    return [r[0] for r in rows]


def upgrade() -> None:
    for t in _tables():
        if t in COPY_TABLES:
            continue
        op.execute(f"ALTER TABLE engine.{t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE engine.{t} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {POLICY} ON engine.{t} USING ("
            "coalesce(current_setting('app.tenant_id', true), '') = '' "
            "OR tenant_id = current_setting('app.tenant_id', true))"
        )


def downgrade() -> None:
    for t in _tables():
        op.execute(f"DROP POLICY IF EXISTS {POLICY} ON engine.{t}")
        op.execute(f"ALTER TABLE engine.{t} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE engine.{t} DISABLE ROW LEVEL SECURITY")
