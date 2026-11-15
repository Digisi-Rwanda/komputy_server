from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import text

from ledgerline.auth.tokens import set_jwks_for_tests
from ledgerline.config import get_settings
from ledgerline.kernel.ids import new_ulid

ROOT = Path(__file__).parents[2]
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = "test-key"
_prepared = False


def _prepare_db() -> None:
    """Web tables (minimal) → engine migrations → queue schema. Idempotent; once per session."""
    global _prepared
    if _prepared:
        return
    import psycopg

    from ledgerline.db import sync_dsn

    with psycopg.connect(sync_dsn(), autocommit=True) as c:
        c.execute((ROOT / "tests" / "web_tables.sql").read_text())
        has_queue = c.execute("SELECT to_regclass('public.procrastinate_jobs')").fetchone()[0]
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, check=True, capture_output=True)
    if not has_queue:
        subprocess.run(
            [sys.executable, "-m", "procrastinate", "--app=ledgerline.queue.app", "schema", "--apply"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
    _prepared = True


def token(
    sub: str, tid: str, wr: str, av: int = 1, ecl: str = "none", leases: str = "none", **extra: Any
) -> dict[str, str]:
    s, now = get_settings(), int(time.time())
    claims = {
        "sub": sub,
        "tid": tid,
        "wr": wr,
        "av": av,
        "iss": s.web_jwt_issuer,
        "aud": s.web_jwt_audience,
        "iat": now,
        "exp": now + 300,
        "svc": {"ecl": {"role": ecl, "state": "active"}, "leases": {"role": leases, "state": "active"}},
        **extra,
    }
    return {"Authorization": f"Bearer {jwt.encode(claims, KEY, algorithm='RS256', headers={'kid': KID})}"}


class Workspace:
    """A fresh tenant with an admin, a preparer and a reviewer for each service."""

    def __init__(self, tid: str, users: dict[str, str]) -> None:
        self.tid, self.users = tid, users

    def h(self, who: str) -> dict[str, str]:
        roles = {
            "admin": ("owner", "reviewer", "reviewer"),
            "prep": ("member", "analyst", "preparer"),
            "rev": ("member", "reviewer", "reviewer"),
            "viewer": ("member", "viewer", "viewer"),
        }[who]
        return token(self.users[who], self.tid, roles[0], ecl=roles[1], leases=roles[2])


async def make_workspace(name: str, viewer: bool = True) -> Workspace:
    from ledgerline.db import AsyncSessionLocal

    tid = new_ulid()
    users = {k: new_ulid() for k in ("admin", "prep", "rev", *(("viewer",) if viewer else ()))}
    roles = {
        "admin": ("owner", None),
        "prep": ("member", "preparer"),
        "rev": ("member", "reviewer"),
        "viewer": ("member", "viewer"),
    }
    async with AsyncSessionLocal() as db:
        await db.execute(text("INSERT INTO public.tenants (id, name) VALUES (:i, :n)"), {"i": tid, "n": name})
        for k, uid in users.items():
            mid = new_ulid()
            await db.execute(
                text("INSERT INTO public.users (id, email, name) VALUES (:i, :e, :n)"),
                {"i": uid, "e": f"{k}.{tid[-6:]}@x.test", "n": f"{k.title()} {name}"},
            )
            await db.execute(
                text(
                    "INSERT INTO public.tenant_memberships (id, user_id, tenant_id, workspace_role) VALUES (:m, :u, :t, :w)"
                ),
                {"m": mid, "u": uid, "t": tid, "w": roles[k][0]},
            )
            if roles[k][1]:
                await db.execute(
                    text(
                        "INSERT INTO public.member_service_roles (id, membership_id, service, role) VALUES (:i, :m, 'leases', :r)"
                    ),
                    {"i": new_ulid(), "m": mid, "r": roles[k][1]},
                )
        await db.commit()
    return Workspace(tid, users)


@pytest.fixture(scope="session")
async def client() -> Any:
    _prepare_db()
    jwk: dict[str, Any] = RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True)
    set_jwks_for_tests({"keys": [{**jwk, "kid": KID, "alg": "RS256", "use": "sig"}]})
    from ledgerline.app import create_app
    from ledgerline.storage import ensure_bucket

    app = create_app()
    async with app.router.lifespan_context(app):
        await ensure_bucket()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as c:
            yield c


async def drain(queues: list[str]) -> None:
    from ledgerline.queue import app as queue_app

    await queue_app.run_worker_async(queues=queues, wait=False, install_signal_handlers=False)
