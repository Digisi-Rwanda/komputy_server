"""Accuracy beyond the golden cases.

1. Independent check: for regular annuities the engine's per-flow discounting must equal the
   closed-form annuity formula (different maths, same answer) to the cent.
2. Property test: hundreds of random leases with random events — every chain must pass every
   integrity check, run off to exactly 0.00, and balance its journals month by month.
"""

import random
from collections import defaultdict
from decimal import Decimal, localcontext

import pytest

from ledgerline.leases.domain.dates import add_days, add_months
from ledgerline.leases.domain.engine import CTX, compute

FREQ = {"monthly": 1, "quarterly": 3, "semiannual": 6, "annual": 12}


def terms(start, years, amount, freq, timing, **extra):
    return {
        "commencementDate": start,
        "endDate": add_days(add_months(start, years * 12), -1),
        "manualLines": [],
        "nonLeasePerPayment": "0",
        "separateNonLease": True,
        "prepaidBeforeCommencement": "0",
        "initialDirectCosts": extra.get("idc", "0"),
        "incentivesReceived": extra.get("inc", "0"),
        "restorationEstimate": extra.get("rest", "0"),
        "paymentRule": {
            "amount": amount,
            "frequency": freq,
            "timing": timing,
            "rentFreeMonths": extra.get("free", 0),
            "escalation": extra.get("esc", {"type": "none"}),
        },
    }


def closed_form(amount, rate, freq, timing, years):
    with localcontext(CTX):
        n = years * 12 // FREQ[freq]
        i = (Decimal(1) + Decimal(rate)) ** (Decimal(FREQ[freq]) / 12) - 1
        pv = Decimal(amount) * (1 - (1 + i) ** -n) / i
        if timing == "advance":  # first payment is at commencement → in the ROU asset, not the liability
            pv = pv * (1 + i) - Decimal(amount)
        return pv.quantize(Decimal("0.01"))


@pytest.mark.parametrize("freq", list(FREQ))
@pytest.mark.parametrize("timing", ["arrears", "advance"])
@pytest.mark.parametrize("rate,years", [("0.05", 3), ("0.0875", 5), ("0.12", 10), ("0.02", 20)])
def test_matches_closed_form_annuity(freq, timing, rate, years):
    c = compute(
        [
            {
                "start": "2027-01-01",
                "terms": terms("2027-01-01", years, "12345.67", freq, timing),
                "rate": rate,
                "event": "initial",
            }
        ]
    )
    assert c.ok
    assert f"{c.segments[0].L0:.2f}" == f"{closed_form('12345.67', rate, freq, timing, years)}"


def random_lease(rng: random.Random):
    start = f"{rng.randint(2022, 2028)}-{rng.randint(1, 12):02d}-{rng.choice(['01', '15', '28', '31'] if rng.random() < 0.3 else ['01'])}"
    if start.endswith("-31"):
        start = start[:-2] + "01"
    years = rng.choice([1, 2, 3, 5, 7, 10, 15])
    freq = rng.choice(list(FREQ))
    t = terms(
        start,
        years,
        f"{rng.randint(100, 900000)}.{rng.randint(0, 99):02d}",
        freq,
        rng.choice(["arrears", "advance"]),
        free=rng.choice([0, 0, 0, 1, 3, 6]),
        idc=rng.choice(["0", "1500.55"]),
        inc=rng.choice(["0", "0", "700"]),
        rest=rng.choice(["0", "0", "25000"]),
        esc=rng.choice(
            [
                {"type": "none"},
                {
                    "type": "fixed_pct",
                    "pct": rng.choice(["0.03", "0.05", "0.10"]),
                    "everyMonths": 12,
                },
            ]
        ),
    )
    rate = f"0.{rng.randint(10, 250):04d}"
    inputs = [{"start": start, "terms": t, "rate": rate, "event": "initial"}]
    last = t
    for _ in range(rng.choice([0, 0, 1, 2, 3])):
        prev_start = inputs[-1]["start"]
        eff = add_months(prev_start[:8] + "01", rng.randint(2, 18))
        if eff >= last["endDate"]:
            break
        ev = rng.choice(["index", "term", "modification", "termination"])
        nt = {
            **last,
            "paymentRule": {
                **last["paymentRule"],
                "amountSteps": [
                    *(last["paymentRule"].get("amountSteps") or []),
                    {"from": eff, "amount": f"{rng.randint(100, 900000)}"},
                ],
            },
        }
        inp = {
            "start": eff,
            "terms": nt,
            "rate": rate if ev == "index" else f"0.{rng.randint(10, 250):04d}",
            "event": ev,
        }
        if ev == "term":
            nt["endDate"] = add_days(add_months(nt["endDate"], rng.choice([-6, 12, 24])), 0)
            if nt["endDate"] <= eff:
                break
        if ev == "modification" and rng.random() < 0.4:
            inp["scopeDecrease"] = rng.choice(["0.25", "0.5"])
        if ev == "termination":
            inp["terminationPenalty"] = rng.choice(["0", "5000"])
            inputs.append(inp)
            break
        inputs.append(inp)
        last = nt
    return inputs


def test_random_portfolio_invariants():
    rng = random.Random(16)
    for i in range(400):
        inputs = random_lease(rng)
        c = compute(inputs)
        assert c.ok, (i, inputs, [k for k in c.checks if not k["ok"]])
        assert c.rows[-1]["liabClosing"] == "0.00" and c.rows[-1]["rouClosing"] == "0.00", i
        per_month = defaultdict(lambda: Decimal(0))
        for j in c.journals:
            per_month[j["month"]] += Decimal(j["debit"]) - Decimal(j["credit"])
        assert all(v == 0 for v in per_month.values()), (
            i,
            {k: v for k, v in per_month.items() if v},
        )
        # each row rolls forward exactly
        for r in c.rows:
            liab = (
                Decimal(r["liabOpening"])
                + Decimal(r["additions"])
                + Decimal(r["interest"])
                - Decimal(r["payments"])
                + Decimal(r["remeasurement"])
                - Decimal(r["derecognised"])
            )
            assert liab == Decimal(r["liabClosing"]), (i, r)
