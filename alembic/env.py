"""Alembic owns the `engine` schema only. The web app's tables (public / app) are never touched."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, text

from ledgerline.db import ENGINE_SCHEMA, Base, sync_dsn
from ledgerline.models import load_all

load_all()
target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):  # noqa: ANN001, ARG001
    schema = getattr(obj, "schema", None) or getattr(getattr(obj, "table", None), "schema", None)
    return schema == ENGINE_SCHEMA


def run() -> None:
    url = sync_dsn().replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {ENGINE_SCHEMA}"))
        conn.execute(text(f"SET search_path TO {ENGINE_SCHEMA}"))
        context.configure(
            connection=conn, target_metadata=target_metadata, include_schemas=True, include_object=include_object,
            version_table_schema=ENGINE_SCHEMA, compare_type=True,
        )
        context.run_migrations()


run()
