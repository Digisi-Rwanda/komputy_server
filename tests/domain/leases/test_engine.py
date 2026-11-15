"""Golden cases shared with the web engine (src/lib/leases/golden.json is a copy)."""

import json
import time
from decimal import Decimal
from pathlib import Path

import pytest

from ledgerline.leases.domain.dates import add_months, months_between
from ledgerline.leases.domain.engine import compute, current_split, maturity

CASES = json.loads((Path(__file__).parent.parent.parent / "fixtures" / "leases_golden.json").read_text())[
    "cases"
]


def year_sum(rows, col, year):
    return f"{sum((Decimal(r[col]) for r in rows if r['month'].startswith(year)), Decimal(0)):.2f}"


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_golden(case):
    c = compute(case["inputs"], case["dayCount"])
    e = case["expect"]
    assert c.ok, [k for k in c.checks if not k["ok"]]
    s0 = c.segments[0]
    if "L0" in e:
        assert f"{s0.L0:.2f}" == e["L0"]
    if "R0" in e:
        assert f"{s0.R0:.2f}" == e["R0"]
    for col, years in e.get("yearSums", {}).items():
        for y, v in years.items():
            assert year_sum(c.rows, col, y) == v, (col, y)
    for m, v in e.get("closing", {}).items():
        assert next(r for r in c.rows if r["month"] == m)["liabClosing"] == v
    if "firstRow" in e:
        for k, v in e["firstRow"].items():
            assert c.rows[0][k] == v
    if "seg1" in e:
        j = c.segments[1].to_json()
        for k, v in e["seg1"].items():
            assert j[k] == v, k
    if "lastClosing" in e:
        assert c.rows[-1]["liabClosing"] == e["lastClosing"]
    if "flowCount" in e:
        assert len(c.flows) == e["flowCount"]
        assert c.flows[0].date == e["firstFlowDate"]
        assert f"{c.flows[-1].amount:.2f}" == e["lastFlowAmount"]
    if "flowAmounts" in e:
        assert [f"{f.amount:.2f}" for f in c.flows] == e["flowAmounts"]
    if "currentSplit" in e:
        cs = e["currentSplit"]
        assert current_split(c.segments, cs["asOf"], case["dayCount"]) == {
            "current": cs["current"],
            "nonCurrent": cs["nonCurrent"],
        }
    if "maturity" in e:
        assert maturity(c.segments, e["maturity"]["asOf"]) == e["maturity"]["buckets"]


def test_dates():
    assert add_months("2027-01-31", 1) == "2027-02-28"
    assert add_months("2028-01-31", 1) == "2028-02-29"
    assert add_months("2027-01-31", 2, 31) == "2027-03-31"
    assert months_between("2027-01-01", "2028-01-01") == 12


def test_perf_10_year_monthly():
    t0 = time.perf_counter()
    terms = {
        "commencementDate": "2026-01-01",
        "endDate": "2035-12-31",
        "manualLines": [],
        "nonLeasePerPayment": "0",
        "separateNonLease": True,
        "prepaidBeforeCommencement": "0",
        "initialDirectCosts": "0",
        "incentivesReceived": "0",
        "restorationEstimate": "0",
        "paymentRule": {
            "amount": "5000",
            "frequency": "monthly",
            "timing": "arrears",
            "rentFreeMonths": 0,
            "escalation": {"type": "fixed_pct", "pct": "0.03", "everyMonths": 12},
        },
    }
    c = compute([{"start": "2026-01-01", "terms": terms, "rate": "0.09", "event": "initial"}])
    assert c.ok
    assert time.perf_counter() - t0 < 3
