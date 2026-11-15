"""Calendar dates as ISO 'YYYY-MM-DD' strings (port of web src/lib/leases/dates.ts).

Strings compare correctly with < and >, which the engine relies on.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal


def is_iso_date(s: object) -> bool:
    if not isinstance(s, str) or len(s) != 10:
        return False
    try:
        return date.fromisoformat(s).isoformat() == s
    except ValueError:
        return False


def add_days(d: str, n: int) -> str:
    return (date.fromisoformat(d) + timedelta(days=n)).isoformat()


def days_between(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def add_months(d: str, n: int, anchor_day: int | None = None) -> str:
    """31 Jan + 1m = 28/29 Feb; anchor_day keeps 31 → 31 Mar later."""
    y, m, day = (int(x) for x in d.split("-"))
    total = (y * 12 + (m - 1)) + n
    ny, nm = divmod(total, 12)
    dim = calendar.monthrange(ny, nm + 1)[1]
    return date(ny, nm + 1, min(anchor_day or day, dim)).isoformat()


def month_key(d: str) -> str:
    return d[:7]


def month_end(key: str) -> str:
    y, m = (int(x) for x in key.split("-"))
    return date(y, m, calendar.monthrange(y, m)[1]).isoformat()


def next_month_key(key: str) -> str:
    return month_key(add_months(key + "-01", 1))


def months_between(a: str, b: str) -> Decimal:
    """Fractional months: whole months while a+k <= b, then remaining days over the next month's length."""
    if b < a:
        return -months_between(b, a)
    day = int(a[8:])
    ay, am = int(a[:4]), int(a[5:7])
    by, bm = int(b[:4]), int(b[5:7])
    k = (by - ay) * 12 + (bm - am)
    if add_months(a, k, day) > b:
        k -= 1
    frm = add_months(a, k, day)
    to = add_months(a, k + 1, day)
    return Decimal(k) + Decimal(days_between(frm, b)) / Decimal(days_between(frm, to))
