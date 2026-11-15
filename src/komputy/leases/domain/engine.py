"""IFRS 16 lessee engine: authoritative server port of web src/lib/leases/engine.ts.

Same algorithm, same JSON shapes (camelCase terms in, camelCase string amounts out) so the
browser preview and the server agree to the cent; tests/fixtures/leases_golden.json is shared.

Model: a lease is a chain of segments. Segment 0 starts at commencement; every approved event
starts a new segment at its effective date. Within a segment L(s) is the PV of remaining flows,
so interest over (a, b] = L(b) − L(a) + payments in (a, b] and telescopes to Σpayments − L0.
Instants are ISO dates meaning "start of that day": an arrears payment shown on 31 Dec happens at
the 1 Jan instant, so the k-th periodic payment sits exactly k periods after commencement.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, ROUND_HALF_UP, Context, Decimal, localcontext
from typing import Any

from komputy.leases.domain.dates import (
    add_days,
    add_months,
    days_between,
    month_key,
    months_between,
    next_month_key,
)

ENGINE_VERSION = "leases-1.0.0"
CTX = Context(prec=34, rounding=ROUND_HALF_EVEN)
ZERO = Decimal(0)
ONE = Decimal(1)
FREQ_MONTHS = {"monthly": 1, "quarterly": 3, "semiannual": 6, "annual": 12}
MATURITY_BUCKETS = ["≤ 1 year", "1-2 years", "2-5 years", "> 5 years"]

ACCOUNT_LABELS = {
    "rou_cost": "Right-of-use asset: cost",
    "rou_accdep": "Right-of-use asset: accumulated depreciation",
    "lease_liability": "Lease liability",
    "interest_expense": "Interest expense: leases",
    "depreciation_expense": "Depreciation expense: ROU",
    "cash_payable": "Cash / accounts payable",
    "restoration_provision": "Restoration provision",
    "idc_clearing": "Initial direct costs clearing",
    "incentive_receivable": "Lease incentive received",
    "modification_gain_loss": "Gain / loss on lease modification",
    "lease_expense": "Lease expense (exempt leases)",
}


def D(v: Any) -> Decimal:
    if isinstance(v, Decimal):
        return v
    if v is None or v == "":
        return ZERO
    return Decimal(str(v))


def r2(d: Decimal) -> Decimal:
    q = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return abs(q) if q == 0 else q  # never "-0.00"


def s2(d: Decimal) -> str:
    return f"{r2(d):.2f}"


def _max(a: str, b: str) -> str:
    return a if a > b else b


def _min(a: str, b: str) -> str:
    return a if a < b else b


# ─── Cash flows ────────────────────────────────────────────────────────────


@dataclass
class Flow:
    date: str
    at: str
    amount: Decimal
    kind: str
    manual: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "at": self.at,
            "amount": s2(self.amount),
            "kind": self.kind,
            "manual": self.manual,
        }


def build_cash_flows(t: dict[str, Any]) -> list[Flow]:
    """Dated cash flows, sorted by instant (ties keep generation order)."""
    flows: list[Flow] = []
    comm, end_date = t["commencementDate"], t["endDate"]
    end_instant = add_days(end_date, 1)
    rule = t.get("paymentRule")
    with localcontext(CTX):
        if rule and D(rule.get("amount")) > 0:
            pm = FREQ_MONTHS[rule["frequency"]]
            day = int(comm[8:])
            free_until = add_months(comm, int(rule.get("rentFreeMonths") or 0), day)
            steps = sorted(rule.get("amountSteps") or [], key=lambda s: s["from"])
            esc = rule.get("escalation") or {"type": "none"}
            fixed = esc.get("type") == "fixed_pct" and esc.get("everyMonths") and esc.get("pct")

            def esc_steps(d: str) -> int:
                if not fixed:
                    return 0
                v = months_between(comm, d) / Decimal(esc["everyMonths"]) + Decimal("1e-9")
                return int(v.to_integral_value(rounding=ROUND_FLOOR))

            k = 1
            while True:
                start = add_months(comm, (k - 1) * pm, day)
                if start >= end_instant:
                    break
                full = add_months(comm, k * pm, day)
                nxt = _min(full, end_instant)
                shown = start if rule["timing"] == "advance" else add_days(nxt, -1)
                at = start if rule["timing"] == "advance" else nxt
                k += 1
                if start < free_until:
                    continue
                base, base_steps = D(rule["amount"]), 0
                for s in (
                    steps
                ):  # CPI / renegotiated rent resets the base; fixed escalation continues from it
                    if shown >= s["from"]:
                        base, base_steps = D(s["amount"]), esc_steps(s["from"])
                amount = base
                if fixed:
                    amount = amount * (ONE + D(esc["pct"])) ** (esc_steps(start) - base_steps)
                if nxt < full:  # stub final period pays pro-rata
                    amount = amount * days_between(start, nxt) / days_between(start, full)
                if not t.get("separateNonLease", True):
                    amount += D(t.get("nonLeasePerPayment"))
                flows.append(Flow(shown, at, r2(amount), "fixed", False))
        for m in t.get("manualLines") or []:
            at = m["date"] if m.get("endOfDay") is False else add_days(m["date"], 1)
            flows.append(Flow(m["date"], at, D(m["amount"]), m.get("kind") or "fixed", True))
    flows.sort(key=lambda f: f.at)  # stable
    return flows


# ─── Time & discounting ────────────────────────────────────────────────────


def tau(comm: str, at: str, dc: str) -> Decimal:
    with localcontext(CTX):
        if dc == "periodic":
            return months_between(comm, at) / 12
        return Decimal(days_between(comm, at)) / 365


def df(rate: Decimal, years: Decimal) -> Decimal:
    with localcontext(CTX):
        return (ONE + rate) ** (-years)


def pv_at(flows: list[Flow], s: str, rate: Decimal, comm: str, dc: str) -> Decimal:
    with localcontext(CTX):
        ts = tau(comm, s, dc)
        return sum((f.amount * df(rate, tau(comm, f.at, dc) - ts) for f in flows if f.at > s), ZERO)


# ─── Segments ──────────────────────────────────────────────────────────────


@dataclass
class Segment:
    start: str
    end: str
    dep_end: str
    rate: Decimal
    flows: list[Flow]
    terms: dict[str, Any]
    event: str
    L0: Decimal = ZERO
    R0: Decimal = ZERO
    liab_before: Decimal = ZERO
    rou_before: Decimal = ZERO
    derec_liab: Decimal = ZERO
    derec_rou: Decimal = ZERO
    liab_adj: Decimal = ZERO
    rou_adj: Decimal = ZERO
    gain_loss: Decimal = ZERO
    penalty: Decimal = ZERO
    prepaid: Decimal = ZERO
    idc: Decimal = ZERO
    incentives: Decimal = ZERO
    restoration_pv: Decimal = ZERO
    _idx: tuple[list[str], list[Decimal]] | None = field(default=None, repr=False)

    def to_json(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "event": self.event,
            "rate": str(self.rate),
            "L0": s2(self.L0),
            "R0": s2(self.R0),
            "liabBefore": s2(self.liab_before),
            "rouBefore": s2(self.rou_before),
            "derecLiab": s2(self.derec_liab),
            "derecRou": s2(self.derec_rou),
            "liabAdj": s2(self.liab_adj),
            "rouAdj": s2(self.rou_adj),
            "gainLoss": s2(self.gain_loss),
            "penalty": s2(self.penalty),
        }


def _liability_at(seg: Segment, s: str, comm: str, dc: str) -> Decimal:
    """L(s) = (1+r)^τ(s) · Σ_{at_i > s} CF_i·(1+r)^-τ(at_i): one power per call."""
    with localcontext(CTX):
        if seg._idx is None:
            w = [f.amount * df(seg.rate, tau(comm, f.at, dc)) for f in seg.flows]
            suffix = [ZERO] * (len(w) + 1)
            for i in range(len(w) - 1, -1, -1):
                suffix[i] = suffix[i + 1] + w[i]
            seg._idx = ([f.at for f in seg.flows], suffix)
        ats, suffix = seg._idx
        at = _max(s, seg.start)
        lo = bisect.bisect_right(ats, at)
        return suffix[lo] * (ONE + seg.rate) ** tau(comm, at, dc)


def _rou_at(seg: Segment, s: str, comm: str, dc: str) -> Decimal:
    with localcontext(CTX):
        t0, t1 = tau(comm, seg.start, dc), tau(comm, seg.dep_end, dc)
        if t1 <= t0:
            return ZERO
        ts = max(t0, min(t1, tau(comm, s, dc)))
        return seg.R0 * (ONE - (ts - t0) / (t1 - t0))


def build_segments(inputs: list[dict[str, Any]], day_count: str) -> list[Segment]:
    """inputs: [{start, terms, rate, event, scopeDecrease?, terminationPenalty?}]"""
    if not inputs:
        return []
    comm = inputs[0]["terms"]["commencementDate"]
    out: list[Segment] = []
    with localcontext(CTX):
        for i, inp in enumerate(inputs):
            t, ev = inp["terms"], inp["event"]
            rate = D(inp["rate"])
            flows = (
                []
                if ev == "termination"
                else [f for f in build_cash_flows(t) if i == 0 or f.at > inp["start"]]
            )
            end = add_days(t["endDate"], 1)
            dep_end = add_days(t["usefulLifeEnd"], 1) if t.get("usefulLifeEnd") else end
            seg = Segment(
                inp["start"],
                inp["start"] if ev == "termination" else end,
                dep_end,
                rate,
                flows,
                t,
                ev,
            )
            if i == 0:
                seg.L0 = pv_at(flows, comm, rate, comm, day_count)
                seg.prepaid = sum((f.amount for f in flows if f.at <= comm), ZERO) + D(
                    t.get("prepaidBeforeCommencement")
                )
                seg.restoration_pv = D(t.get("restorationEstimate")) * df(
                    rate, tau(comm, end, day_count)
                )
                seg.idc = D(t.get("initialDirectCosts"))
                seg.incentives = D(t.get("incentivesReceived"))
                seg.R0 = seg.L0 + seg.prepaid - seg.incentives + seg.idc + seg.restoration_pv
                out.append(seg)
                continue
            prev = out[i - 1]
            seg.liab_before = _liability_at(prev, inp["start"], comm, day_count)
            seg.rou_before = _rou_at(prev, inp["start"], comm, day_count)
            f = ONE if ev == "termination" else D(inp.get("scopeDecrease"))
            seg.derec_liab = seg.liab_before * f
            seg.derec_rou = seg.rou_before * f
            seg.penalty = D(inp.get("terminationPenalty"))
            gain = seg.derec_liab - seg.derec_rou - seg.penalty
            seg.L0 = (
                ZERO if ev == "termination" else pv_at(flows, inp["start"], rate, comm, day_count)
            )
            seg.liab_adj = (
                ZERO if ev == "termination" else seg.L0 - (seg.liab_before - seg.derec_liab)
            )
            rou_adj = seg.liab_adj
            rou_mid = seg.rou_before - seg.derec_rou
            if rou_mid + rou_adj < 0:  # ROU cannot go below zero; the rest goes to profit or loss
                gain += -(rou_mid + rou_adj)
                rou_adj = -rou_mid
            seg.rou_adj = rou_adj
            seg.gain_loss = gain
            seg.R0 = ZERO if ev == "termination" else rou_mid + rou_adj
            out.append(seg)
    return out


# ─── Monthly schedule ──────────────────────────────────────────────────────

COLUMNS = [
    "additions",
    "interest",
    "payments",
    "remeasurement",
    "derecognised",
    "rouAdditions",
    "depreciation",
    "rouAdjustment",
    "rouDerecognised",
    "provisionUnwind",
    "gainLoss",
    "penalty",
]


def build_schedule(segs: list[Segment], day_count: str) -> list[dict[str, Any]]:
    if not segs:
        return []
    s0, last = segs[0], segs[-1]
    comm = s0.start
    final = add_days(last.start, 1) if last.event == "termination" else _max(last.end, last.dep_end)

    def prov_at(s: str) -> Decimal:
        return s0.restoration_pv * (ONE + s0.rate) ** tau(comm, _min(s, s0.end), day_count)

    exact: list[dict[str, Any]] = []
    with localcontext(CTX):
        mk = month_key(comm)
        while f"{mk}-01" < final:
            nmk = next_month_key(mk)
            m_start, m_end = _max(f"{mk}-01", comm), _min(f"{nmk}-01", final)
            row: dict[str, Any] = {c: ZERO for c in COLUMNS}
            row.update(month=mk, periodEnd=add_days(f"{nmk}-01", -1), segment=0, hasEvent=False)
            row["provisionUnwind"] = prov_at(m_end) - prov_at(m_start)
            if month_key(comm) == mk:
                row["additions"], row["rouAdditions"] = s0.L0, s0.R0
            for i, seg in enumerate(segs):
                seg_end = segs[i + 1].start if i + 1 < len(segs) else final
                if i > 0 and m_start <= seg.start < m_end:
                    row["hasEvent"] = True
                    row["remeasurement"] += seg.liab_adj
                    row["derecognised"] += seg.derec_liab
                    row["rouAdjustment"] += seg.rou_adj
                    row["rouDerecognised"] += seg.derec_rou
                    row["gainLoss"] += seg.gain_loss
                    row["penalty"] += seg.penalty
                a, b = _max(m_start, seg.start), _min(m_end, seg_end)
                if a >= b:
                    continue
                row["segment"] = i
                paid = sum((f.amount for f in seg.flows if a < f.at <= b), ZERO)
                row["payments"] += paid
                row["interest"] += (
                    _liability_at(seg, b, comm, day_count)
                    - _liability_at(seg, a, comm, day_count)
                    + paid
                )
                row["depreciation"] += _rou_at(seg, a, comm, day_count) - _rou_at(
                    seg, b, comm, day_count
                )
            exact.append(row)
            mk = nmk

        # cumulative rounding per column; the closing balances are the rounded exact balances and
        # interest / depreciation absorb the cent so the roll-forward always ties and ends at 0.00
        cum = {c: ZERO for c in COLUMNS}
        L = R = ZERO
        exact_l = exact_r = ZERO
        out: list[dict[str, Any]] = []
        for e in exact:
            v: dict[str, Decimal] = {}
            for c in COLUMNS:
                prev = cum[c]
                cum[c] = prev + e[c]
                v[c] = r2(cum[c]) - r2(prev)
            exact_l += (
                e["additions"]
                + e["interest"]
                - e["payments"]
                + e["remeasurement"]
                - e["derecognised"]
            )
            exact_r += (
                e["rouAdditions"] - e["depreciation"] + e["rouAdjustment"] - e["rouDerecognised"]
            )
            liab_open, rou_open = L, R
            L, R = r2(exact_l), r2(exact_r)
            v["interest"] = (
                L
                - liab_open
                - v["additions"]
                + v["payments"]
                - v["remeasurement"]
                + v["derecognised"]
            )
            v["depreciation"] = (
                rou_open + v["rouAdditions"] + v["rouAdjustment"] - v["rouDerecognised"] - R
            )
            row_out = {
                "month": e["month"],
                "periodEnd": e["periodEnd"],
                "segment": e["segment"],
                "hasEvent": e["hasEvent"],
                "liabOpening": s2(liab_open),
                "liabClosing": s2(L),
                "rouOpening": s2(rou_open),
                "rouClosing": s2(R),
            }
            row_out.update({c: s2(v[c]) for c in COLUMNS})
            out.append(row_out)
        return out


def group_schedule(
    rows: list[dict[str, Any]], by: str, fy_start_month: int = 1
) -> list[dict[str, Any]]:
    if by == "month":
        return rows

    def key_of(m: str) -> str:
        y, mo = int(m[:4]), int(m[5:7])
        shifted = (mo - fy_start_month + 12) % 12
        fy = (y if mo >= fy_start_month else y - 1) + (0 if fy_start_month == 1 else 1)
        return f"FY{fy}" if by == "year" else f"FY{fy} Q{shifted // 3 + 1}"

    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        k = key_of(r["month"])
        g = groups.get(k)
        if g is None:
            groups[k] = {**r, "month": k}
            continue
        for c in COLUMNS:
            g[c] = s2(D(g[c]) + D(r[c]))
        g["liabClosing"], g["rouClosing"], g["periodEnd"] = (
            r["liabClosing"],
            r["rouClosing"],
            r["periodEnd"],
        )
        g["hasEvent"] = g["hasEvent"] or r["hasEvent"]
    return list(groups.values())


# ─── Balances at a date ────────────────────────────────────────────────────


def _seg_at(segs: list[Segment], s: str) -> Segment:
    for g in reversed(segs):
        if g.start <= s:
            return g
    return segs[0]


def current_split(segs: list[Segment], as_of: str, day_count: str) -> dict[str, str]:
    """Current portion = L(d) − L(d + 12 months), assuming no future events."""
    if not segs:
        return {"current": "0.00", "nonCurrent": "0.00"}
    comm = segs[0].start
    s = add_days(as_of, 1)
    if s < comm:
        return {"current": "0.00", "nonCurrent": "0.00"}
    seg = _seg_at(segs, s)
    now = _liability_at(seg, s, comm, day_count)
    later = _liability_at(seg, add_months(s, 12), comm, day_count)
    return {"current": s2(now - later), "nonCurrent": s2(later)}


def maturity(segs: list[Segment], as_of: str) -> list[str]:
    """Undiscounted remaining payments after as_of by bucket (IFRS 7)."""
    out = [ZERO, ZERO, ZERO, ZERO]
    if not segs:
        return [s2(x) for x in out]
    s = add_days(as_of, 1)
    for f in _seg_at(segs, s).flows:
        if f.at <= s:
            continue
        m = months_between(s, f.at)
        i = 0 if m <= 12 else 1 if m <= 24 else 2 if m <= 60 else 3
        out[i] += f.amount
    return [s2(x) for x in out]


# ─── Journals ──────────────────────────────────────────────────────────────


def _pair(out: list[dict[str, Any]], base: dict[str, Any], dr: str, cr: str, amt: Decimal) -> None:
    if amt == 0:
        return
    d, c = (dr, cr) if amt > 0 else (cr, dr)
    v = s2(abs(amt))
    out.append({**base, "account": d, "debit": v, "credit": "0.00"})
    out.append({**base, "account": c, "debit": "0.00", "credit": v})


def build_journals(rows: list[dict[str, Any]], segs: list[Segment]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    s0 = segs[0]
    for r in rows:

        def base(kind: str, memo: str, r: dict[str, Any] = r) -> dict[str, Any]:
            return {"month": r["month"], "date": r["periodEnd"], "kind": kind, "memo": memo}

        if D(r["additions"]) > 0 or D(r["rouAdditions"]) > 0:
            lines = [
                ["rou_cost", D(r["rouAdditions"]), ZERO],
                ["incentive_receivable", r2(s0.incentives), ZERO],
                ["lease_liability", ZERO, D(r["additions"])],
                ["cash_payable", ZERO, r2(s0.prepaid)],
                ["idc_clearing", ZERO, r2(s0.idc)],
                ["restoration_provision", ZERO, r2(s0.restoration_pv)],
            ]
            # absorb any rounding cent in the provision line so the entry always balances
            lines[5][2] += sum((x[1] - x[2] for x in lines), ZERO)
            for acc, dr, cr in lines:
                if dr == 0 and cr == 0:
                    continue
                out.append(
                    {
                        **base("initial", "Initial recognition"),
                        "account": acc,
                        "debit": s2(dr),
                        "credit": s2(cr),
                    }
                )
        _pair(
            out,
            base("interest", "Interest accrual"),
            "interest_expense",
            "lease_liability",
            D(r["interest"]),
        )
        _pair(
            out,
            base("payment", "Lease payments"),
            "lease_liability",
            "cash_payable",
            D(r["payments"]),
        )
        _pair(
            out,
            base("depreciation", "ROU depreciation"),
            "depreciation_expense",
            "rou_accdep",
            D(r["depreciation"]),
        )
        _pair(
            out,
            base("unwind", "Restoration provision unwinding"),
            "interest_expense",
            "restoration_provision",
            D(r["provisionUnwind"]),
        )
        if r["hasEvent"]:
            # shortcut: derecognises ROU net against cost (no AD split); split it when the ERP needs gross movements.
            dl, dr_, pen = D(r["derecognised"]), D(r["rouDerecognised"]), D(r["penalty"])
            if dl or dr_ or pen:
                gain = dl - dr_ - pen
                for acc, x, y in [
                    ("lease_liability", dl, ZERO),
                    ("rou_cost", ZERO, dr_),
                    ("cash_payable", ZERO, pen),
                    (
                        "modification_gain_loss",
                        -gain if gain < 0 else ZERO,
                        gain if gain > 0 else ZERO,
                    ),
                ]:
                    if x or y:
                        out.append(
                            {
                                **base("derecognition", "Partial / full derecognition"),
                                "account": acc,
                                "debit": s2(x),
                                "credit": s2(y),
                            }
                        )
            ra = D(r["rouAdjustment"])
            _pair(out, base("remeasurement", "Remeasurement"), "rou_cost", "lease_liability", ra)
            # liability reduced by more than the ROU could absorb → excess to profit or loss
            _pair(
                out,
                base("remeasurement", "Remeasurement"),
                "modification_gain_loss",
                "lease_liability",
                D(r["remeasurement"]) - ra,
            )
    return out


def exempt_expense(t: dict[str, Any]) -> list[dict[str, Any]]:
    """Short-term / low-value leases: straight-line expense, no balance sheet."""
    flows = build_cash_flows(t)
    total = sum((f.amount for f in flows), ZERO)
    start, end = t["commencementDate"], add_days(t["endDate"], 1)
    days = days_between(start, end)
    out: list[dict[str, Any]] = []
    if days <= 0 or total == 0:
        return out
    cum = ZERO
    mk = month_key(start)
    with localcontext(CTX):
        while f"{mk}-01" < end:
            nmk = next_month_key(mk)
            a, b = _max(f"{mk}-01", start), _min(f"{nmk}-01", end)
            prev = cum
            cum = cum + total * days_between(a, b) / days
            base = {
                "month": mk,
                "date": add_days(f"{nmk}-01", -1),
                "kind": "expense",
                "memo": "Straight-line lease expense",
            }
            _pair(out, base, "lease_expense", "cash_payable", r2(cum) - r2(prev))
            mk = nmk
    return out


# ─── Integrity checks ──────────────────────────────────────────────────────


def integrity_checks(
    rows: list[dict[str, Any]], journals: list[dict[str, Any]], segs: list[Segment]
) -> list[dict[str, Any]]:
    last = rows[-1] if rows else None

    def total(k: str) -> Decimal:
        return sum((D(r[k]) for r in rows), ZERO)

    dr = sum((D(j["debit"]) for j in journals), ZERO)
    cr = sum((D(j["credit"]) for j in journals), ZERO)
    terminated = bool(segs) and segs[-1].event == "termination"
    liab_in = total("additions") + total("interest") + total("remeasurement")
    liab_out = total("payments") + total("derecognised")
    rou_in = total("rouAdditions") + total("rouAdjustment")
    rou_out = total("depreciation") + total("rouDerecognised")
    tol = Decimal("0.005")
    return [
        {
            "id": "liab-zero",
            "label": "Liability derecognised on termination"
            if terminated
            else "Liability runs off to zero at lease end",
            "ok": bool(last) and D(last["liabClosing"]) == 0,
            "expected": "0.00",
            "actual": last["liabClosing"] if last else "-",
        },
        {
            "id": "liab-roll",
            "label": "Additions + interest + remeasurements = payments + derecognitions",
            "ok": abs(liab_in - liab_out) <= tol or (bool(last) and D(last["liabClosing"]) != 0),
            "expected": s2(liab_out),
            "actual": s2(liab_in),
        },
        {
            "id": "rou-zero",
            "label": "ROU asset fully depreciated / derecognised",
            "ok": bool(last) and D(last["rouClosing"]) == 0,
            "expected": "0.00",
            "actual": last["rouClosing"] if last else "-",
        },
        {
            "id": "rou-roll",
            "label": "ROU additions + adjustments = depreciation + derecognitions",
            "ok": abs(rou_in - rou_out) <= tol,
            "expected": s2(rou_out),
            "actual": s2(rou_in),
        },
        {
            "id": "balanced",
            "label": "Journals balance (debits = credits)",
            "ok": dr == cr,
            "expected": s2(dr),
            "actual": s2(cr),
        },
    ]


@dataclass
class Computation:
    segments: list[Segment]
    rows: list[dict[str, Any]]
    journals: list[dict[str, Any]]
    checks: list[dict[str, Any]]
    flows: list[Flow]

    @property
    def ok(self) -> bool:
        return all(c["ok"] for c in self.checks)

    def to_json(self) -> dict[str, Any]:
        return {
            "segments": [s.to_json() for s in self.segments],
            "rows": self.rows,
            "journals": self.journals,
            "checks": self.checks,
            "flows": [f.to_json() for f in self.flows],
            "engineVersion": ENGINE_VERSION,
        }


def compute(inputs: list[dict[str, Any]], day_count: str = "periodic") -> Computation:
    segs = build_segments(inputs, day_count)
    rows = build_schedule(segs, day_count)
    journals = build_journals(rows, segs)
    checks = integrity_checks(rows, journals, segs)
    flows = build_cash_flows(segs[-1].terms) if segs else []
    return Computation(segs, rows, journals, checks, flows)
