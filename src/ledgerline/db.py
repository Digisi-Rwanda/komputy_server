"""Async SQLAlchemy engine + session. Engine-owned tables live in the `engine` schema."""

from __future__ import annotations

import ssl
from collections.abc import AsyncGenerator
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from ledgerline.config import get_settings

settings = get_settings()
ENGINE_SCHEMA = "engine"


def _prepare_async_url(url: str) -> tuple[str, dict]:
    """asyncpg takes TLS as an SSLContext, not `?ssl=require` / `?sslmode=require` in the URL."""
    connect_args: dict = {}
    parsed = urlparse(url)
    if parsed.scheme in ("postgres", "postgresql"):
        parsed = parsed._replace(scheme="postgresql+asyncpg")
    params = parse_qs(parsed.query, keep_blank_values=True)
    ssl_val = (params.pop("ssl", [None])[0]) or (params.pop("sslmode", [None])[0])
    if ssl_val in ("require", "true", "verify-full", "verify-ca"):
        connect_args["ssl"] = ssl.create_default_context()
    url = urlunparse(parsed._replace(query=urlencode({k: v[0] for k, v in params.items()})))
    return url, connect_args


_db_url, _connect_args = _prepare_async_url(settings.database_url)
# Raw SQL in the ported modules names tables unqualified; engine tables first, then the web app's.
_connect_args["server_settings"] = {"search_path": f"{ENGINE_SCHEMA},{settings.web_schema}", "application_name": "ledgerline-engine"}

engine = create_async_engine(
    _db_url,
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
    pool_timeout=settings.database_pool_timeout,
    pool_pre_ping=True,
    pool_recycle=1800,
    connect_args=_connect_args,
    echo=settings.debug,
)

AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)


class Base(DeclarativeBase):
    metadata = MetaData(
        schema=ENGINE_SCHEMA,
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_N_name)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        },
    )


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def sync_dsn() -> str:
    """libpq DSN (psycopg / Procrastinate / Alembic) from the same DATABASE_URL."""
    url = settings.database_url.replace("postgresql+asyncpg://", "postgresql://").replace("postgres://", "postgresql://")
    return url.replace("ssl=require", "sslmode=require")
