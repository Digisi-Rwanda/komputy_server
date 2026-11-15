"""End-to-end engine flow against real Postgres + S3 (MinIO): the same path the web app drives.

create run → browser-style multipart upload straight to storage → validate (queue job) → accept → compute (queue job)
→ results → review: preparer can't sign off, reviewer returns, preparer resubmits, reviewer signs off → run locked.

Needs `make dev` (Postgres :5440 with the web app's seed, MinIO :9010). Tokens are signed with a test key whose JWKS
is injected, exactly as the web app's RS256 tokens are verified.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from ledgerline.db import AsyncSessionLocal
from tests.integration.conftest import drain, make_workspace

pytestmark = pytest.mark.integration
WB = Path(__file__).parent.parent / "fixtures" / "workbooks"


@pytest.fixture(scope="module")
async def ctx(client: httpx.AsyncClient) -> dict[str, Any]:
    ws = await make_workspace("ECL Bank")
    return {"c": client, "tid": ws.tid, "prep": ws.h("prep"), "rev": ws.h("rev"), "admin": ws.h("admin"),
            "prep_as_rev": __import__("tests.integration.conftest", fromlist=["token"]).token(ws.users["prep"], ws.tid, "member", ecl="reviewer"),
            "base": f"/api/v1/tenants/{ws.tid}/runs"}


async def _drain(queues: list[str]) -> None:
    await drain(queues)


async def _upload(c: httpx.AsyncClient, base: str, rid: str, h: dict[str, str], kind: str, path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    r = await c.post(f"{base}/{rid}/uploads/direct", headers=h, json={"kind": kind, "filename": path.name, "size": len(data)})
    assert r.status_code == 201, r.text
    plan = r.json()["data"]
    parts = []
    async with httpx.AsyncClient() as s3:  # the browser's job: PUT each part to its presigned URL
        for i, url in enumerate(plan["urls"]):
            chunk = data[i * plan["partSize"]:(i + 1) * plan["partSize"]]
            put = await s3.put(url, content=chunk)
            assert put.status_code == 200, put.text
            parts.append({"partNumber": i + 1, "etag": put.headers["etag"]})
    r = await c.post(f"{base}/{rid}/uploads/direct/complete", headers=h,
                     json={"kind": kind, "filename": path.name, "key": plan["key"], "uploadId": plan["uploadId"], "parts": parts})
    assert r.status_code == 201, r.text
    return r.json()["data"]


async def _validate(c: httpx.AsyncClient, base: str, rid: str, h: dict[str, str], scope: str) -> dict[str, Any]:
    r = await c.post(f"{base}/{rid}/validate?scope={scope}", headers=h, json={})
    assert r.status_code == 202, r.text
    job_id = r.json()["data"]["jobId"]
    await _drain(["io"])
    job = (await c.get(f"{base}/{rid}/validation-jobs/{job_id}", headers=h)).json()["data"]
    assert job["status"] == "complete", job
    return job["result"]


async def test_full_ecl_run_with_review(ctx: dict[str, Any]) -> None:
    c, base, prep, rev = ctx["c"], ctx["base"], ctx["prep"], ctx["rev"]
    r = await c.post(base, headers=prep, json={"name": f"IT run {int(time.time())}", "reporting_period": "Mar 2025"})
    assert r.status_code == 201, r.text
    rid = r.json()["data"]["fullId"]

    up = await _upload(c, base, rid, prep, "PD", WB / "PD_2025_Jan_Feb.xlsx")
    assert up["rowCount"] if "rowCount" in up else up.get("row_count")
    await _upload(c, base, rid, prep, "PD", WB / "PD_2025_Mar_Apr.xlsx")
    await _upload(c, base, rid, prep, "LGD", WB / "LGD_2025_03.xlsx")
    await _upload(c, base, rid, prep, "EAD", WB / "EAD_2025_03.xlsx")

    for scope in ("pd", "lgd", "ead"):
        result = await _validate(c, base, rid, prep, scope)
        assert result["status"] != "error", result
        acc = await c.post(f"{base}/{rid}/validation/{scope}/accept", headers=prep, json={})
        assert acc.status_code == 200, acc.text

    r = await c.post(f"{base}/{rid}/execute", headers=prep)
    assert r.status_code == 202, r.text
    await _drain(["calc"])
    await _drain(["io"])  # workbooks
    run = (await c.get(f"{base}/{rid}", headers=prep)).json()["data"]
    assert run["status"] == "success", run.get("failureMessage") or run

    # Web-shaped views (what the Next.js pages render)
    views = f"/api/v1/tenants/{ctx['tid']}/ecl/views/runs"
    v = (await c.get(f"{views}/{rid}", headers=prep)).json()["data"]
    assert v["status"] == "needs-review" and v["loans"] > 0 and v["totalEcl"] > 0
    assert abs(sum(st["ecl"] for st in v["stages"]) - v["totalEcl"]) < 0.01, "stage split ties to the total"
    assert abs(sum(sg["ecl"] for sg in v["segments"]) - v["totalEcl"]) < 0.01, "segment split ties to the total"
    page1 = (await c.get(f"{views}/{rid}/loans?limit=50", headers=prep)).json()["data"]
    assert len(page1["rows"]) == 50 and page1["next"] and page1["total"] == v["loans"]
    page2 = (await c.get(f"{views}/{rid}/loans?limit=50&after={page1['next']}", headers=prep)).json()["data"]
    assert page2["rows"][0]["id"] > page1["rows"][-1]["id"], "keyset pagination moves forward"
    assert isinstance((await c.get(f"{views}/{rid}/exceptions", headers=prep)).json()["data"], list)
    ex = (await c.get(f"{views}/{rid}/explore", headers=prep)).json()["data"]
    assert ex["loans"] == v["loans"] and abs(sum(b["ecl"] for b in ex["sizes"]) - v["totalEcl"]) < 0.01, "size buckets tie to the total"
    top = ex["top"][0]
    ld = (await c.get(f"{views}/{rid}/loans/{top['id']}", headers=prep)).json()["data"]
    assert ld["schedule"] and abs(ld["ecl"] - top["ecl"]) < 1e-6
    cmp = (await c.get(f"/api/v1/tenants/{ctx['tid']}/ecl/views/compare?a={rid}&b={rid}", headers=prep)).json()["data"]
    mv = {m["label"]: m["value"] for m in cmp["movement"]}
    assert abs(mv["Opening"] - v["totalEcl"]) < 0.01 and abs(mv["Closing"] - v["totalEcl"]) < 0.01 and mv["New loans"] == 0
    assert sum(cmp["migration"][i][i] for i in range(3)) == v["loans"], "a run compared with itself stays on the diagonal"
    assert len((await c.get(f"{views}/{rid}/audit", headers=prep)).json()["data"]) >= 1

    for kind in ("stage", "coverage", "exceptions", "recon", "board"):
        rep = await c.get(f"/api/v1/tenants/{ctx['tid']}/ecl/runs/{rid}/reports/{kind}", headers=prep)
        assert rep.status_code == 200 and rep.content[:2] == b"PK", (kind, rep.status_code, rep.text[:200])
    rf = await c.get(f"/api/v1/tenants/{ctx['tid']}/ecl/runs/{rid}/reports/rollforward?fmt=csv&compare={rid}", headers=prep)
    assert rf.status_code == 200 and b"Opening" in rf.content

    # Review: maker/checker
    r = await c.post(f"{base}/{rid}/review/sign-off", headers=prep, json={})
    assert r.status_code == 403 and r.json()["code"] == "INSUFFICIENT_ROLE"
    r = await c.post(f"{base}/{rid}/review/sign-off", headers=ctx["prep_as_rev"], json={})
    assert r.status_code == 403 and r.json()["code"] == "SEGREGATION_OF_DUTIES"
    r = await c.post(f"{base}/{rid}/review/return", headers=rev, json={"comment": "Check the stage 3 overlay"})
    assert r.status_code == 200 and r.json()["data"]["state"] == "returned"
    r = await c.post(f"{base}/{rid}/review/resubmit", headers=prep, json={"comment": "Checked"})
    assert r.json()["data"]["state"] == "needs_review"
    r = await c.post(f"{base}/{rid}/review/sign-off", headers=rev, json={"comment": "Approved"})
    assert r.status_code == 200, r.text
    review = r.json()["data"]
    assert review["state"] == "signed_off" and review["lockedAt"]
    assert [e["action"] for e in review["events"]] == ["returned", "submitted", "signed_off"]

    r = await c.delete(f"{base}/{rid}", headers=ctx["admin"])
    assert r.status_code == 409 and r.json()["code"] == "RUN_LOCKED"

    async with AsyncSessionLocal() as db:
        events = (await db.execute(text("SELECT payload->>'event' FROM engine.outbox_events WHERE payload->>'runId' = :r"), {"r": rid})).scalars().all()
    assert {"completed", "signoff"} <= set(events)


async def test_tenant_isolation(ctx: dict[str, Any]) -> None:
    c, tid = ctx["c"], ctx["tid"]
    from tests.integration.conftest import token

    other = token("01SOMEONEELSE0000000000000", "01OTHERTENANT0000000000000", "owner", ecl="analyst")
    r = await c.get(f"/api/v1/tenants/{tid}/runs", headers=other)
    assert r.status_code == 403
    r = await c.get(f"/api/v1/tenants/{tid}/runs")
    assert r.status_code == 401


async def test_sample_run_is_ready_to_validate(ctx: dict[str, Any]) -> None:
    c = ctx["c"]
    r = await c.post(f"/api/v1/tenants/{ctx['tid']}/ecl/sample-run", headers=ctx["prep"])
    assert r.status_code == 201, r.text
    rid = r.json()["data"]["fullId"]
    v = (await c.get(f"/api/v1/tenants/{ctx['tid']}/ecl/views/runs/{rid}", headers=ctx["prep"])).json()["data"]
    assert sorted(i["kind"] for i in v["inputs"]) == ["EAD", "LGD", "PD", "PD"]
    assert (await _validate(c, ctx["base"], rid, ctx["prep"], "pd"))["status"] != "blocking"
