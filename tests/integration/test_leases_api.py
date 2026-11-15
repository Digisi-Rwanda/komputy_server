"""End-to-end API tests for the leases module (real Postgres; object storage stubbed in memory).

Ported from ECL-Server: setup → wizard → review → events → close → lock/reopen → imports, plus the guards:
optimistic lock, maker-checker, tenant isolation, idempotent posting. Auth is the web app's token (per-service roles).
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncGenerator
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient

from tests.integration.conftest import Workspace, make_workspace

pytestmark = pytest.mark.integration
_FILES: dict[str, bytes] = {}


@pytest.fixture(autouse=True)
def _storage(monkeypatch: pytest.MonkeyPatch):
    async def up(key, f, ct):
        data = f.read() if hasattr(f, "read") else bytes(f)
        _FILES[key] = data
        return "x", len(data)

    async def presign(key, expires_seconds=900):
        return f"https://storage.test/{key}"

    async def rm(key):
        _FILES.pop(key, None)

    monkeypatch.setattr("ledgerline.apps.leases.router.upload_stream", up)
    monkeypatch.setattr("ledgerline.apps.leases.router.presign_download", presign)
    monkeypatch.setattr("ledgerline.apps.leases.router.delete_object", rm)
    yield
    _FILES.clear()


class T(Workspace):
    def u(self, path: str) -> str:
        return f"/api/v1/tenants/{self.tid}/leases{path}"


async def _tenant(name: str) -> T:
    ws = await make_workspace(name, viewer=False)
    return T(ws.tid, ws.users)


@pytest_asyncio.fixture
async def t() -> AsyncGenerator[T, None]:
    yield await _tenant("Zenith")


async def ok(r):
    assert r.status_code < 300, r.text
    return r.json()["data"]


async def setup(c: AsyncClient, t: T, period: str = "2027-01") -> None:
    await ok(
        await c.put(
            t.u("/entity"),
            json={
                "name": "Zenith Bank",
                "currency": "USD",
                "country": "Rwanda",
                "fyStartMonth": 1,
                "bookName": "IFRS book",
            },
            headers=t.h("admin"),
        )
    )
    await ok(await c.post(t.u("/periods"), json={"key": period}, headers=t.h("admin")))
    await ok(
        await c.post(
            t.u("/setup/policies"),
            json={
                "effectiveFrom": "2020-01-01",
                "data": {
                    "dayCount": "periodic",
                    "shortTermClasses": ["vehicles"],
                    "lowValueElection": True,
                    "lowValueThreshold": "5000",
                    "separateNonLeaseClasses": ["property"],
                    "capitalisationThreshold": "0",
                },
            },
            headers=t.h("admin"),
        )
    )
    pts = [{"currency": "USD", "termYears": y, "rate": r} for y, r in ((1, "0.08"), (3, "0.10"), (5, "0.11"))]
    await ok(
        await c.post(
            t.u("/setup/curves"),
            json={
                "effectiveFrom": "2020-01-01",
                "data": {"source": "Treasury memo", "points": pts},
            },
            headers=t.h("admin"),
        )
    )
    accounts = {
        k: str(1000 + i)
        for i, k in enumerate(
            [
                "rou_cost",
                "rou_accdep",
                "lease_liability",
                "interest_expense",
                "depreciation_expense",
                "cash_payable",
                "restoration_provision",
                "idc_clearing",
                "incentive_receivable",
                "modification_gain_loss",
                "lease_expense",
            ]
        )
    }
    await ok(
        await c.post(
            t.u("/setup/gl"),
            json={"effectiveFrom": "2020-01-01", "data": {"accounts": accounts}},
            headers=t.h("admin"),
        )
    )


TERMS = {
    "commencementDate": "2027-01-01",
    "endDate": "2029-12-31",
    "manualLines": [],
    "nonLeasePerPayment": "0",
    "separateNonLease": True,
    "prepaidBeforeCommencement": "0",
    "initialDirectCosts": "0",
    "incentivesReceived": "0",
    "restorationEstimate": "0",
    "paymentRule": {
        "amount": "10000",
        "frequency": "annual",
        "timing": "arrears",
        "escalation": {"type": "none"},
        "rentFreeMonths": 0,
    },
}
ASSESS = {
    "identifiedAsset": True,
    "rightToControl": True,
    "exemption": "none",
    "exemptionRationale": "",
    "options": [],
    "purchaseOption": {"exists": False, "reasonablyCertain": None, "rationale": ""},
    "termRationale": "Contract",
}


async def approved_lease(c: AsyncClient, t: T, terms: dict | None = None) -> dict:
    cp = await ok(await c.post(t.u("/counterparties"), json={"name": "Vision City Estates"}, headers=t.h("prep")))
    d = await ok(
        await c.post(
            t.u(""),
            json={"name": "HQ", "assetClass": "property", "counterpartyId": cp["id"]},
            headers=t.h("prep"),
        )
    )
    rev = (
        await ok(
            await c.patch(
                t.u(f"/{d['id']}"),
                json={
                    "rev": d["rev"],
                    "lease": {"contractEndDate": "2029-12-31", "wizardStep": 4},
                    "version": {
                        "terms": terms or TERMS,
                        "assessment": ASSESS,
                        "rate": {
                            "value": "0.10",
                            "source": "override",
                            "reason": "Blueprint example",
                        },
                    },
                },
                headers=t.h("prep"),
            )
        )
    )["rev"]
    calc = await ok(await c.post(t.u(f"/{d['id']}/calculate"), json={"rev": rev}, headers=t.h("prep")))
    rev = (
        await ok(
            await c.post(
                t.u(f"/{d['id']}/submit"),
                json={"rev": calc["rev"], "approverId": t.users["rev"]},
                headers=t.h("prep"),
            )
        )
    )["rev"]
    await ok(
        await c.post(
            t.u(f"/{d['id']}/decision"),
            json={"rev": rev, "approve": True, "text": "ok"},
            headers=t.h("rev"),
        )
    )
    return await ok(await c.get(t.u(f"/{d['id']}"), headers=t.h("prep")))


async def test_setup_gate_and_versioning(client: AsyncClient, t: T) -> None:
    r = await client.post(t.u(""), json={"name": "x"}, headers=t.h("prep"))
    assert r.status_code == 422 and "setup" in r.json()["detail"].lower()
    r = await client.put(t.u("/entity"), json={"name": "Z", "currency": "USD", "bookName": "B"}, headers=t.h("prep"))
    assert r.status_code == 403
    await setup(client, t)
    st = await ok(await client.get(t.u("/state"), headers=t.h("rev")))
    assert st["setup"] == {
        "entity": True,
        "periods": True,
        "policy": True,
        "rates": True,
        "gl": True,
    }
    assert {p["role"] for p in st["members"]} == {"administrator", "analyst", "reviewer"}
    # later version needs approval by someone else; one pending at a time
    pts = [{"currency": "USD", "termYears": 1, "rate": "0.09"}]
    r = await client.post(
        t.u("/setup/curves"),
        json={"effectiveFrom": "2027-06-01", "data": {"source": "Q2", "points": pts}},
        headers=t.h("admin"),
    )
    assert r.status_code == 422  # reason required
    await ok(
        await client.post(
            t.u("/setup/curves"),
            json={
                "effectiveFrom": "2027-06-01",
                "reason": "Q2 refresh",
                "data": {"source": "Q2", "points": pts},
            },
            headers=t.h("admin"),
        )
    )
    r = await client.post(
        t.u("/setup/curves"),
        json={
            "effectiveFrom": "2027-07-01",
            "reason": "again",
            "data": {"source": "Q3", "points": pts},
        },
        headers=t.h("admin"),
    )
    assert r.status_code == 409
    assert (
        await client.post(t.u("/setup/curves/2/decision"), json={"approve": True}, headers=t.h("admin"))
    ).status_code == 403
    await ok(await client.post(t.u("/setup/curves/2/decision"), json={"approve": True}, headers=t.h("rev")))
    bad = [{"currency": "USD", "termYears": 1, "rate": "10"}]
    assert (
        await client.post(
            t.u("/setup/curves"),
            json={
                "effectiveFrom": "2028-01-01",
                "reason": "x",
                "data": {"source": "s", "points": bad},
            },
            headers=t.h("admin"),
        )
    ).status_code == 422


async def test_lease_lifecycle_and_guards(client: AsyncClient, t: T) -> None:
    await setup(client, t)
    cp = await ok(await client.post(t.u("/counterparties"), json={"name": "Vision City Estates"}, headers=t.h("prep")))
    d = await ok(
        await client.post(
            t.u(""),
            json={"name": "HQ", "assetClass": "property", "counterpartyId": cp["id"]},
            headers=t.h("prep"),
        )
    )
    assert d["code"] == "L-0001"
    body = {
        "lease": {"contractEndDate": "2029-12-31"},
        "version": {
            "terms": TERMS,
            "assessment": ASSESS,
            "rate": {"value": "0.10", "source": "override", "reason": "x"},
        },
    }
    rev = (await ok(await client.patch(t.u(f"/{d['id']}"), json={"rev": d["rev"], **body}, headers=t.h("prep"))))["rev"]
    # stale rev → 409; reviewer can't edit
    assert (
        await client.patch(t.u(f"/{d['id']}"), json={"rev": d["rev"], **body}, headers=t.h("prep"))
    ).status_code == 409
    assert (await client.patch(t.u(f"/{d['id']}"), json={"rev": rev, **body}, headers=t.h("rev"))).status_code == 403
    # submit before calculate fails
    assert (
        await client.post(
            t.u(f"/{d['id']}/submit"),
            json={"rev": rev, "approverId": t.users["rev"]},
            headers=t.h("prep"),
        )
    ).status_code == 422
    calc = await ok(await client.post(t.u(f"/{d['id']}/calculate"), json={"rev": rev}, headers=t.h("prep")))
    seg = calc["computation"]["segments"][0]
    assert seg["L0"] == "24868.52"
    # an unchanged autosave keeps the calculation
    rev = (await ok(await client.patch(t.u(f"/{d['id']}"), json={"rev": calc["rev"], **body}, headers=t.h("prep"))))[
        "rev"
    ]
    lease = await ok(await client.get(t.u(f"/{d['id']}"), headers=t.h("prep")))
    assert lease["versions"][0]["calculated"]["liability"] == "24868.52"
    # self-approval blocked at submit and at decision
    assert (
        await client.post(
            t.u(f"/{d['id']}/submit"),
            json={"rev": rev, "approverId": t.users["prep"]},
            headers=t.h("prep"),
        )
    ).status_code == 403
    rev = (
        await ok(
            await client.post(
                t.u(f"/{d['id']}/submit"),
                json={"rev": rev, "approverId": t.users["rev"]},
                headers=t.h("prep"),
            )
        )
    )["rev"]
    assert (
        await client.post(t.u(f"/{d['id']}/decision"), json={"rev": rev, "approve": True}, headers=t.h("prep"))
    ).status_code == 403
    # return needs a comment; then resubmit and approve
    assert (
        await client.post(
            t.u(f"/{d['id']}/decision"),
            json={"rev": rev, "approve": False, "text": ""},
            headers=t.h("rev"),
        )
    ).status_code == 422
    rev = (
        await ok(
            await client.post(
                t.u(f"/{d['id']}/decision"),
                json={
                    "rev": rev,
                    "approve": False,
                    "text": "Attach the contract",
                    "field": "documents",
                },
                headers=t.h("rev"),
            )
        )
    )["rev"]
    up = await ok(
        await client.post(
            t.u(f"/{d['id']}/documents"),
            files={"file": ("contract.pdf", b"%PDF-1.4 hello", "application/pdf")},
            headers=t.h("prep"),
        )
    )
    # same bytes again → duplicate refused
    assert (
        await client.post(
            t.u(f"/{d['id']}/documents"),
            files={"file": ("copy.pdf", b"%PDF-1.4 hello", "application/pdf")},
            headers=t.h("prep"),
        )
    ).status_code == 409
    url = await ok(await client.get(t.u(f"/{d['id']}/documents/{up['id']}/url"), headers=t.h("rev")))
    assert url["url"].startswith("https://storage.test/")
    rev = (
        await ok(
            await client.post(
                t.u(f"/{d['id']}/submit"),
                json={"rev": up["rev"], "approverId": t.users["rev"]},
                headers=t.h("prep"),
            )
        )
    )["rev"]
    await ok(
        await client.post(
            t.u(f"/{d['id']}/decision"),
            json={"rev": rev, "approve": True, "text": "ok"},
            headers=t.h("rev"),
        )
    )
    st = await ok(await client.get(t.u("/state"), headers=t.h("prep")))
    lz = next(x for x in st["leases"] if x["id"] == d["id"])
    assert lz["versions"][0]["status"] == "approved"
    # approved docs can't be removed; notifications delivered
    assert (await client.delete(t.u(f"/{d['id']}/documents/{up['id']}"), headers=t.h("prep"))).status_code == 422
    # Portfolio read model (web overview + register) ties to the books.
    pf = (await client.get(t.u("/views/portfolio?period=2027-06"), headers=t.h("prep"))).json()["data"]
    row = next(r for r in pf["register"] if r["id"] == d["id"])
    assert row["status"] in ("approved", "modified") and row["liability"] and row["liability"] > 0
    ov = pf["overview"]
    assert (
        abs(
            ov["liability"]
            - sum(r["liability"] or 0 for r in pf["register"] if r["status"] in ("approved", "modified", "expiring"))
        )
        < 0.01
    )
    assert abs(ov["current"] + ov["nonCurrent"] - ov["liability"]) < 0.01
    lv = (await client.get(t.u(f"/views/lease/{d['id']}"), headers=t.h("prep"))).json()["data"]
    assert lv["monthly"] and lv["annual"] and lv["lease"]["code"] == "L-0001"
    assert abs(float(lv["annual"][-1]["liabClosing"]) - float(lv["monthly"][-1]["liabClosing"])) < 0.005
    rf = {x["label"]: x["value"] for x in ov["rollForward"]}
    assert (
        abs(rf["Opening"] + rf["Additions"] + rf["Interest"] + rf["Payments"] + rf["Modifications"] - rf["Closing"])
        < 0.01
    )

    # Notices go to the web app through the outbox (it applies people's preferences and delivers them).
    from sqlalchemy import text

    from ledgerline.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        titles = (
            (
                await db.execute(
                    text("SELECT payload->>'title' FROM engine.outbox_events WHERE tenant_id = :t"), {"t": t.tid}
                )
            )
            .scalars()
            .all()
        )
    assert any("approved" in (x or "") for x in titles), titles
    assert any(a["action"] == "lease.approve" for a in st["audit"])


async def test_events_match_golden(client: AsyncClient, t: T) -> None:
    await setup(client, t)
    lease = await approved_lease(client, t)
    ev = {
        "event": "index",
        "effectiveDate": "2028-01-01",
        "newAmount": "10500",
        "reason": "CPI +5%",
    }
    pv = await ok(await client.post(t.u(f"/{lease['id']}/events/preview"), json=ev, headers=t.h("prep")))
    assert pv["segment"]["liabAdj"] == "867.77" and pv["ok"]
    rev = (
        await ok(
            await client.post(
                t.u(f"/{lease['id']}/events"),
                json={**ev, "rev": lease["rev"], "approverId": t.users["rev"]},
                headers=t.h("prep"),
            )
        )
    )["rev"]
    # second pending change refused
    assert (
        await client.post(
            t.u(f"/{lease['id']}/events"),
            json={**ev, "rev": rev, "approverId": t.users["rev"]},
            headers=t.h("prep"),
        )
    ).status_code == 409
    await ok(
        await client.post(
            t.u(f"/{lease['id']}/decision"),
            json={"rev": rev, "approve": True, "text": "ok"},
            headers=t.h("rev"),
        )
    )
    mod = {
        "event": "modification",
        "effectiveDate": "2028-01-01",
        "newAmount": "5000",
        "scopeDecrease": "0.5",
        "reason": "Half the space",
        "rate": {"value": "0.10", "source": "override", "reason": "same"},
    }
    fresh = await approved_lease(client, t)
    pv = await ok(await client.post(t.u(f"/{fresh['id']}/events/preview"), json=mod, headers=t.h("prep")))
    assert pv["segment"]["gainLoss"] == "388.18"
    term = {
        "event": "termination",
        "effectiveDate": "2028-01-01",
        "terminationPenalty": "1000",
        "reason": "Exit",
    }
    pv = await ok(await client.post(t.u(f"/{fresh['id']}/events/preview"), json=term, headers=t.h("prep")))
    assert pv["segment"]["gainLoss"] == "-223.64"


async def test_close_catch_up_post_lock_reopen(client: AsyncClient, t: T) -> None:
    await setup(client, t, period="2027-01")
    lease = await approved_lease(client, t)
    for p in ("2027-01", "2027-02"):
        await ok(await client.post(t.u(f"/close/{p}/start"), headers=t.h("prep")))
        for s in ("cutoff", "rates", "events"):
            await ok(await client.post(t.u(f"/close/{p}/steps/{s}/complete"), json={}, headers=t.h("prep")))
        b = await ok(await client.post(t.u(f"/close/{p}/generate"), headers=t.h("prep")))
        await ok(await client.post(t.u(f"/close/{p}/payments/confirm-scheduled"), json={}, headers=t.h("prep")))
        await ok(await client.post(t.u(f"/close/{p}/steps/matching/complete"), json={}, headers=t.h("prep")))
        iss = await ok(await client.get(t.u(f"/close/{p}/issues"), headers=t.h("prep")))
        assert all(r["ok"] for r in iss["reconciliations"]), iss["reconciliations"]
        await ok(await client.post(t.u(f"/close/{p}/steps/reconcile/complete"), json={}, headers=t.h("prep")))
        # generator can't approve
        assert (
            await client.post(t.u(f"/close/{p}/steps/approve/complete"), json={}, headers=t.h("prep"))
        ).status_code == 403
        await ok(await client.post(t.u(f"/close/{p}/steps/approve/complete"), json={}, headers=t.h("rev")))
        csv = (await client.get(t.u(f"/batches/{b['id']}/export"), headers=t.h("admin"))).text
        assert "Lease liability" in csv and ",1002," in csv  # label + mapped GL code
        await ok(await client.post(t.u(f"/batches/{b['id']}/post"), json={"erpRef": f"SAP-{p}"}, headers=t.h("admin")))
        await ok(
            await client.post(t.u(f"/batches/{b['id']}/post"), json={"erpRef": f"SAP-{p}"}, headers=t.h("admin"))
        )  # idempotent
        assert (
            await client.post(t.u(f"/batches/{b['id']}/post"), json={"erpRef": "OTHER"}, headers=t.h("admin"))
        ).status_code == 409
        await ok(await client.post(t.u(f"/periods/{p}/lock"), headers=t.h("admin")))
    # back-dated CPI effective in locked Feb → catch-up lines in March
    ev = {
        "event": "index",
        "effectiveDate": "2027-02-01",
        "newAmount": "11000",
        "reason": "late CPI notice",
        "rev": lease["rev"],
        "approverId": t.users["rev"],
    }
    rev = (await ok(await client.post(t.u(f"/{lease['id']}/events"), json=ev, headers=t.h("prep"))))["rev"]
    await ok(
        await client.post(
            t.u(f"/{lease['id']}/decision"),
            json={"rev": rev, "approve": True, "text": "ok"},
            headers=t.h("rev"),
        )
    )
    await ok(await client.post(t.u("/close/2027-03/start"), headers=t.h("prep")))
    for s in ("cutoff", "rates", "events"):
        await ok(await client.post(t.u(f"/close/2027-03/steps/{s}/complete"), json={}, headers=t.h("prep")))
    b = await ok(await client.post(t.u("/close/2027-03/generate"), headers=t.h("prep")))
    st = await ok(await client.get(t.u("/state"), headers=t.h("prep")))
    batch = next(x for x in st["batches"] if x["id"] == b["id"])
    catch = [ln for ln in batch["lines"] if ln["catchUp"]]
    assert catch and sum(Decimal(ln["debit"]) for ln in batch["lines"]) == sum(
        Decimal(ln["credit"]) for ln in batch["lines"]
    )
    await ok(await client.post(t.u("/close/2027-03/payments/confirm-scheduled"), json={}, headers=t.h("prep")))
    await ok(await client.post(t.u("/close/2027-03/steps/matching/complete"), json={}, headers=t.h("prep")))
    iss = await ok(await client.get(t.u("/close/2027-03/issues"), headers=t.h("prep")))
    assert all(r["ok"] for r in iss["reconciliations"]), iss["reconciliations"]
    # reopen Feb is refused while March close exists; reopen requires a reason
    assert (
        await client.post(t.u("/periods/2027-02/reopen"), json={"reason": "x"}, headers=t.h("admin"))
    ).status_code == 422
    st = await ok(await client.get(t.u("/state"), headers=t.h("admin")))
    assert [p["status"] for p in st["periods"]] == ["locked", "locked", "closing"]


async def test_reopen_reverses_batch(client: AsyncClient, t: T) -> None:
    await setup(client, t, period="2027-01")
    await approved_lease(client, t)
    p = "2027-01"
    await ok(await client.post(t.u(f"/close/{p}/start"), headers=t.h("prep")))
    for s in ("cutoff", "rates", "events"):
        await ok(await client.post(t.u(f"/close/{p}/steps/{s}/complete"), json={}, headers=t.h("prep")))
    b = await ok(await client.post(t.u(f"/close/{p}/generate"), headers=t.h("prep")))
    await ok(await client.post(t.u(f"/close/{p}/payments/confirm-scheduled"), json={}, headers=t.h("prep")))
    for s, who in (("matching", "prep"), ("reconcile", "prep"), ("approve", "rev")):
        await ok(await client.post(t.u(f"/close/{p}/steps/{s}/complete"), json={}, headers=t.h(who)))
    await client.get(t.u(f"/batches/{b['id']}/export"), headers=t.h("admin"))
    await ok(await client.post(t.u(f"/batches/{b['id']}/post"), json={"erpRef": "SAP-1"}, headers=t.h("admin")))
    await ok(await client.post(t.u(f"/periods/{p}/lock"), headers=t.h("admin")))
    assert (
        await client.post(t.u(f"/periods/{p}/reopen"), json={"reason": ""}, headers=t.h("admin"))
    ).status_code == 422
    await ok(
        await client.post(t.u(f"/periods/{p}/reopen"), json={"reason": "Auditor adjustment"}, headers=t.h("admin"))
    )
    st = await ok(await client.get(t.u("/state"), headers=t.h("admin")))
    assert [x["status"] for x in st["periods"]] == ["open"] and st["periods"][0]["reopenLog"][0][
        "reason"
    ] == "Auditor adjustment"
    statuses = {x["type"]: x["status"] for x in st["batches"]}
    assert statuses == {"period": "reversed", "reversal": "posted"}
    assert all(Decimal(v) == 0 for per in st["posted"].values() for v in per.values())


async def _wait_import(client: AsyncClient, t: T, code: str, until: str) -> dict:
    from tests.integration.conftest import drain

    for _ in range(100):
        b = await ok(await client.get(t.u(f"/imports/{code}"), headers=t.h("prep")))
        if b["status"] == until:
            return b
        await asyncio.sleep(0.05)
        await drain(["io"])  # import jobs run on the queue
    raise AssertionError(f"import stuck in {b['status']}")


async def test_import_mixed_rows(client: AsyncClient, t: T) -> None:
    await setup(client, t)
    hdr = "lease_ref,name,asset_class,lessor,location,currency,commencement_date,end_date,payment_amount,frequency,timing,rent_free_months,escalation_pct,escalation_every_months,discount_rate_pct,initial_direct_costs,incentives,restoration_estimate,exemption"
    rows = [
        "IMP-1,Branch A,property,Vision City Estates,Kigali,USD,2027-01-01,2031-12-31,4500,monthly,advance,2,5,12,,0,0,0,none",
        "IMP-2,Fleet B,vehicles,Rwanda Motor Leasing,Kigali,USD,2027-02-01,2030-01-31,1850,monthly,arrears,0,,,0.145,0,0,0,none",
        "IMP-3,Bad dates,property,Akagera,Kigali,USD,45658,2030-01-31,1000,monthly,arrears,0,,,,0,0,0,none",
        "IMP-4,Wrong currency,property,Akagera,Kigali,EUR,2027-01-01,2030-01-31,1000,monthly,arrears,0,,,,0,0,0,none",
        "IMP-6,Negative,equipment,Equip Ltd,Kigali,USD,2027-01-01,2028-12-31,-50,monthly,arrears,0,,,,0,0,0,none",
    ]
    raw = ("\n".join([hdr, *rows]) + "\n").encode()
    created = await ok(
        await client.post(t.u("/imports"), files={"file": ("leases.csv", raw, "text/csv")}, headers=t.h("prep"))
    )
    code = created["id"]
    await ok(
        await client.put(
            t.u(f"/imports/{code}/mapping"),
            json={"mapping": (await ok(await client.get(t.u(f"/imports/{code}"), headers=t.h("prep"))))["mapping"]},
            headers=t.h("prep"),
        )
    )
    b = await _wait_import(client, t, code, "validated")
    msgs = " ".join(i["message"] for r in b["rows"] for i in r["issues"])
    for s in ("Excel date number", "Only USD", "isn't a positive amount", "looks like a decimal"):
        assert s in msgs, s
    assert (
        await client.post(t.u(f"/imports/{code}/commit"), headers=t.h("admin"))
    ).status_code == 422  # warnings not accepted
    await ok(await client.post(t.u(f"/imports/{code}/accept-warnings"), headers=t.h("prep")))
    assert (await client.post(t.u(f"/imports/{code}/commit"), headers=t.h("prep"))).status_code == 403  # admin commits
    await ok(await client.post(t.u(f"/imports/{code}/commit"), headers=t.h("admin")))
    b = await _wait_import(client, t, code, "committed")
    assert sorted(r["outcome"] for r in b["rows"]) == [
        "created",
        "created",
        "skipped",
        "skipped",
        "skipped",
    ]
    n = await ok(
        await client.post(t.u(f"/imports/{code}/submit"), json={"approverId": t.users["rev"]}, headers=t.h("prep"))
    )
    assert n["submitted"] == 2
    # same file again → refused
    assert (
        await client.post(t.u("/imports"), files={"file": ("again.csv", raw, "text/csv")}, headers=t.h("prep"))
    ).status_code == 409
    errs = (await client.get(t.u(f"/imports/{code}/errors.csv"), headers=t.h("prep"))).text
    assert "Excel date number" in errs


async def test_tenant_isolation(client: AsyncClient, t: T) -> None:
    await setup(client, t)
    lease = await approved_lease(client, t)
    other = await _tenant("Other")
    # other tenant's users can't read through their own tenant path, nor ours
    assert (await client.get(other.u(f"/{lease['id']}"), headers=other.h("admin"))).status_code == 404
    assert (await client.get(t.u(f"/{lease['id']}"), headers=other.h("admin"))).status_code == 403
    assert (await client.get(t.u("/state"), headers=other.h("prep"))).status_code == 403
    _ = io


async def test_scheduled_jobs(client: AsyncClient, t: T) -> None:
    from datetime import date

    from sqlalchemy import update

    from ledgerline.apps.leases.jobs import daily_alerts, verify_books
    from ledgerline.apps.leases.models import Lease, LeaseBooks
    from ledgerline.db import AsyncSessionLocal as TestSessionLocal

    await setup(client, t, period="2026-01")
    lease = await approved_lease(client, t)
    async with TestSessionLocal() as db:
        # lease ends 2029-12-31 → expiry alert 30 days before; open Jan 2026 period has ended by Mar → close alert
        assert await daily_alerts(db, date(2029, 12, 1)) >= 2
        assert await daily_alerts(db, date(2029, 12, 2)) == 0  # deduped
        assert await verify_books(db) == 0
        await db.execute(update(LeaseBooks).where(LeaseBooks.lease_id == lease["id"]).values(chain_checksum="tampered"))
        await db.commit()
        assert await verify_books(db) == 1
        assert (await db.get(Lease, lease["id"])).verification_failed
