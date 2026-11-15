"""FastAPI application factory (engine API). Private service: reachable only by the web app and workers."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import ORJSONResponse
from sqlalchemy import text

from komputy.config import get_settings
from komputy.db import engine
from komputy.kernel.errors import register_exception_handlers
from komputy.log import configure_logging, get_logger
from komputy.queue import app as queue_app

log = get_logger("app")


def _init_observability() -> None:
    s = get_settings()
    if s.sentry_dsn:
        import sentry_sdk

        sentry_sdk.init(dsn=s.sentry_dsn, environment=s.environment, release=s.app_version, traces_sample_rate=0.1)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    from komputy.storage import close_storage, init_storage

    await init_storage()
    await _register_engine_versions()
    async with queue_app.open_async():  # the API defers jobs; workers run them
        yield
    await close_storage()
    await engine.dispose()


async def _register_engine_versions() -> None:
    """The code that's running is a version: record it (first boot of a release) and promote it if none is."""
    from sqlalchemy import func, select
    from sqlalchemy.dialects.postgresql import insert

    from komputy.db import AsyncSessionLocal
    from komputy.ecl.domain import ENGINE_VERSION as ECL_V
    from komputy.leases.domain.engine import ENGINE_VERSION as LEASES_V
    from komputy.platform.models import EngineVersion

    try:
        async with AsyncSessionLocal() as db:
            for service, ver in (("ecl", ECL_V), ("leases", LEASES_V)):
                await db.execute(
                    insert(EngineVersion)
                    .values(version=ver, service=service, notes="Registered at startup")
                    .on_conflict_do_nothing()
                )
                promoted = await db.scalar(
                    select(func.count())
                    .select_from(EngineVersion)
                    .where(EngineVersion.service == service, EngineVersion.status == "promoted")
                )
                if not promoted:
                    row = await db.get(EngineVersion, (ver, service))
                    if row:
                        row.status, row.promoted_at = "promoted", func.now()
            await db.commit()
    except Exception as exc:
        log.warning("engine_version_registration_failed", error=str(exc))


def create_app() -> FastAPI:
    s = get_settings()
    configure_logging(s)
    _init_observability()
    app = FastAPI(
        title="Komputy Engine",
        version=s.app_version,
        default_response_class=ORJSONResponse,
        lifespan=lifespan,
        docs_url="/docs" if s.environment != "production" else None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
    )
    register_exception_handlers(app)

    @app.middleware("http")
    async def request_context(request: Request, call_next: Any) -> Any:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=rid, path=request.url.path, method=request.method)
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        ms = round((time.perf_counter() - start) * 1000, 1)
        if request.url.path not in ("/healthz", "/readyz"):
            log.info("request", status=response.status_code, ms=ms)
        return response

    from komputy.apps.collateral.router import router as collateral
    from komputy.apps.leases.router import router as leases
    from komputy.apps.leases.views import router as lease_views
    from komputy.apps.results.router import router as results
    from komputy.apps.runs.reports import router as reports
    from komputy.apps.runs.review import router as review
    from komputy.apps.runs.router import router as runs
    from komputy.apps.runs.sample import router as sample
    from komputy.apps.runs.thresholds import router as thresholds
    from komputy.apps.runs.views import router as views
    from komputy.apps.segments.router import router as segments
    from komputy.platform.router import router as platform

    for r in (runs, review, views, thresholds, sample, reports, results, segments, collateral):
        app.include_router(r, prefix="/api/v1")
    app.include_router(lease_views, prefix="/api/v1")  # before the lease router so /views/* isn't taken as a lease id
    app.include_router(leases, prefix="/api/v1")
    app.include_router(platform)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> ORJSONResponse:
        from komputy.storage import head

        checks: dict[str, str] = {}
        workers = 0
        try:
            async with engine.connect() as c:
                await c.execute(text("SELECT 1"))
                # Informational: queue workers with a heartbeat in the last minute (not part of API readiness).
                workers = (
                    await c.scalar(
                        text(
                            "SELECT count(*) FROM procrastinate_workers WHERE last_heartbeat > now() - interval '1 minute'"
                        )
                    )
                    or 0
                )
            checks["db"] = "ok"
        except Exception as exc:
            checks["db"] = f"error: {type(exc).__name__}"
        try:
            await head("readyz-probe")
            checks["storage"] = "ok"
        except Exception as exc:
            checks["storage"] = f"error: {type(exc).__name__}"
        ok = all(v == "ok" for v in checks.values())
        return ORJSONResponse(
            {"status": "ok" if ok else "degraded", "version": s.app_version, "checks": checks, "workers": workers},
            status_code=200 if ok else 503,
        )

    return app


app = create_app()
