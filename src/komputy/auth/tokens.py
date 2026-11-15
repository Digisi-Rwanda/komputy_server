"""Verification of the short-lived RS256 tokens minted by the web app (Next.js / Better Auth JWT plugin).

The web app owns sign-in. We check signature (JWKS, cached and refreshed on unknown `kid`), issuer,
audience and expiry. Claims: sub, tid?, wr (workspace role), svc{ecl|leases:{role,state,lim,wm}}, av, sup?, staff?
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import jwt
from jwt import PyJWKSet

from komputy.config import get_settings
from komputy.kernel.errors import ECLException

_cache: dict[str, Any] = {"set": None, "at": 0.0}
_TTL = 600.0
_LEGACY_ISSUER, _LEGACY_AUDIENCE = "ledgerline-web", "ecl-engine"


def _jwks(force: bool = False) -> PyJWKSet:
    if force or _cache["set"] is None or time.monotonic() - _cache["at"] > _TTL:
        s = get_settings()
        r = httpx.get(f"http://{s.web_hostport}/api/auth/jwks" if s.web_hostport else s.web_jwks_url, timeout=3.0)
        r.raise_for_status()
        _cache.update(set=PyJWKSet.from_dict(r.json()), at=time.monotonic())
    return _cache["set"]


def set_jwks_for_tests(jwks: dict[str, Any]) -> None:
    _cache.update(set=PyJWKSet.from_dict(jwks), at=time.monotonic() + 10**9)


def decode_web_token(token: str) -> dict[str, Any]:
    s = get_settings()
    try:
        kid = jwt.get_unverified_header(token).get("kid")
        keys = _jwks()
        match = [k for k in keys.keys if k.key_id == kid] if kid else keys.keys
        if not match:
            keys = _jwks(force=True)  # key rotated since the last fetch
            match = [k for k in keys.keys if k.key_id == kid]
        if not match:
            raise ECLException("TOKEN_INVALID", "Unknown signing key.", 401)
        claims: dict[str, Any] = jwt.decode(
            token,
            match[0],
            algorithms=["RS256"],
            # shortcut: the pre-rebrand names stay accepted for one release so a web/engine deploy race can't
            # reject sign-ins; remove `_LEGACY_*` once both services run the Komputy names.
            audience=[s.web_jwt_audience, _LEGACY_AUDIENCE],
            issuer=(s.web_jwt_issuer, _LEGACY_ISSUER),
            leeway=10,
        )
    except ECLException:
        raise
    except (jwt.PyJWTError, httpx.HTTPError) as exc:
        raise ECLException("TOKEN_INVALID", "Invalid or expired token.", 401) from exc
    if not claims.get("sub"):
        raise ECLException("TOKEN_INVALID", "Invalid token payload.", 401)
    return claims
