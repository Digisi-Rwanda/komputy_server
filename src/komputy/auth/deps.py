"""Request dependencies: who is calling (from the web token) and what they may do in this workspace.

Roles come from the token, per service. The ported modules speak the legacy vocabulary
(administrator / reviewer / analyst / viewer); `Membership.role` is that vocabulary, derived from the
service of the route being called (ECL or Leases), so the proven role checks keep working unchanged.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from komputy.auth.tokens import decode_web_token
from komputy.db import get_db
from komputy.kernel.errors import ECLException
from komputy.web import Tenant, User

DbSession = Annotated[AsyncSession, Depends(get_db, scope="function")]  # commit before the response is sent
bearer_scheme = HTTPBearer(auto_error=False)

ECL_MAP = {"reviewer": "reviewer", "analyst": "analyst", "viewer": "viewer"}
LEASES_MAP = {"reviewer": "reviewer", "poster": "reviewer", "preparer": "analyst", "viewer": "viewer"}


@dataclass
class Membership:
    """What the ported code calls a membership: built from token claims, never from a table."""

    user_id: str
    tenant_id: str
    role: str  # administrator | reviewer | analyst | viewer (legacy vocabulary, per service)
    workspace_role: str | None
    services: dict[str, Any] = field(default_factory=dict)
    support_session: str | None = None
    id: str = "web-token"
    status: str = "active"


async def get_current_user(
    request: Request,
    db: DbSession,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    if not creds or not creds.credentials:
        raise ECLException("TOKEN_INVALID", "Authentication required.", 401)
    claims = decode_web_token(creds.credentials)
    request.state.web_claims = claims
    if claims.get("sup") and request.method not in ("GET", "HEAD"):
        raise ECLException("SUPPORT_READ_ONLY", "Support sessions are view only.", 403)
    user = (await db.execute(select(User).where(User.id == claims["sub"]))).scalar_one_or_none()
    if not user or not user.is_active:
        raise ECLException("ACCOUNT_DISABLED", "Account is disabled.", 403)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def legacy_role(claims: dict[str, Any], service: str) -> str:
    if claims.get("wr") in ("owner", "admin"):
        return "administrator"
    role = (claims.get("svc") or {}).get(service, {}).get("role", "none")
    return (LEASES_MAP if service == "leases" else ECL_MAP).get(role, "viewer")


def _service_of(request: Request) -> str:
    return "leases" if "/leases" in request.url.path else "ecl"


async def require_tenant_member(tenant_id: str, user: CurrentUser, db: DbSession, request: Request) -> Membership:
    claims: dict[str, Any] = request.state.web_claims
    if claims.get("tid") != tenant_id:
        raise ECLException("NOT_TENANT_MEMBER", "Not a member of this workspace.", 403)
    tenant = (
        await db.execute(
            select(Tenant.status, Tenant.authz_version).where(Tenant.id == tenant_id, Tenant.deleted_at.is_(None))
        )
    ).one_or_none()
    if tenant is None:
        raise ECLException("NOT_TENANT_MEMBER", "Not a member of this workspace.", 403)
    if int(claims.get("av", 0)) < tenant.authz_version:
        raise ECLException("TOKEN_STALE", "Permissions changed. Retry the request.", 401)
    if tenant.status in ("suspended", "closed") and request.method not in ("GET", "HEAD"):
        raise ECLException("TENANT_SUSPENDED", "This workspace is view only.", 403)
    service = _service_of(request)
    svc = (claims.get("svc") or {}).get(service, {})
    if (
        svc.get("state") in (None, "locked", "requested")
        and not claims.get("sup")
        and claims.get("wr") not in ("owner", "admin")
    ):
        raise ECLException("SERVICE_NOT_ENABLED", "This service isn't enabled for your workspace.", 403)
    if svc.get("state") in ("expired", "trial-expired", "suspended") and request.method not in ("GET", "HEAD"):
        raise ECLException("SERVICE_READ_ONLY", "This service is view only for your workspace.", 403)
    # Defence in depth: row-level security policies read this for the rest of the transaction.
    await db.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
    sup = claims.get("sup") or None
    return Membership(
        user_id=user.id,
        tenant_id=tenant_id,
        role="viewer" if sup else legacy_role(claims, service),
        workspace_role=claims.get("wr"),
        services=claims.get("svc") or {},
        support_session=(sup or {}).get("id"),
    )


async def require_tenant_admin(m: Annotated[Membership, Depends(require_tenant_member)]) -> Membership:
    if m.role != "administrator":
        raise ECLException("INSUFFICIENT_ROLE", "Administrator role required.", 403)
    return m


async def require_tenant_analyst_or_admin(m: Annotated[Membership, Depends(require_tenant_member)]) -> Membership:
    if m.role not in ("administrator", "analyst", "reviewer"):
        raise ECLException("INSUFFICIENT_ROLE", "Analyst or administrator role required.", 403)
    return m


async def require_platform_admin(user: CurrentUser, request: Request) -> User:
    if request.state.web_claims.get("staff") != "admin" and not user.is_platform_admin:
        raise ECLException("INSUFFICIENT_ROLE", "Platform administrator role required.", 403)
    return user


PlatformAdmin = Annotated[User, Depends(require_platform_admin)]


def get_client_ip(x_forwarded_for: str | None = None) -> str:
    return (x_forwarded_for or "").split(",")[0].strip() or "unknown"


def get_user_agent(user_agent: Annotated[str | None, Header(alias="User-Agent")] = None) -> str:
    return (user_agent or "")[:500]


def hash_ip(ip: str, salt: str) -> str:
    return hashlib.sha256(f"{ip}{salt}".encode()).hexdigest()
