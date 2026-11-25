"""Automatic upstream-update propagation driven through the HTTP API.

SQLite, FakeLlm, and a FIFO stand-in for the RQ queue that the test drains
explicitly (what one worker would do) — no Redis, no real LLM.

    a (on_resource_change, reading) -> b (on_upstream_update) -> c (on_upstream_update)
    s (on_resource_change, slides)  — an unrelated branch
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.database import get_db
from app.main import app
from app.models import AgentRun, LlmCall
from tests.conftest import FakeLlm, make_resource
from tests.unit import test_upstream_propagation as prop

JUDGE_OK = prop.JUDGE_OK


@pytest.fixture
def jobs(monkeypatch):
    return prop.patch_queue(monkeypatch)


@pytest.fixture
def install(tmp_path, monkeypatch, registry):
    installer, cleanup = prop.agent_installer(tmp_path, monkeypatch)
    yield installer
    cleanup()


@pytest.fixture
def client(db, jobs):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _runs(db, agent_id):
    return db.scalars(
        select(AgentRun).where(AgentRun.agent_id == agent_id).order_by(AgentRun.id)
    ).all()


def _agent(client, agent_id):
    return next(
        a
        for a in client.get("/api/agents").json()
        if a["manifest"]["agent_id"] == agent_id
    )


def test_api_run_and_retag_propagate_through_the_chain(client, db, install, jobs):
    # agents installed after app startup has loaded the shipped registry
    reg, events = install(
        {
            "a": prop.agent("on_resource_change", tags=["reading"]),
            "b": prop.agent("on_upstream_update", ["a"], tags=["reading"]),
            "c": prop.agent("on_upstream_update", ["b"], tags=["reading"]),
            "s": prop.agent("on_resource_change", tags=["slides"]),
        }
    )
    reading = make_resource(
        db,
        locator="https://example.edu/ch3.html",
        items=[{"title": "Ch3", "content": prop.READING_TEXT, "tags": ["reading"]}],
    ).items[0]
    make_resource(
        db,
        locator="https://example.edu/lec9.pdf",
        items=[{"title": "Lec9", "content": prop.SLIDES_TEXT, "tags": ["slides"]}],
    )
    llm = FakeLlm(JUDGE_OK)

    # 1. on-demand request for the root; the worker runs it, then b, then c
    first = client.post("/api/agents/a/runs", json={}).json()
    assert first["status"] == "queued" and jobs.pending == [first["id"]]
    executed = jobs.drain(db, llm)
    assert [(r.agent_id, r.status) for r in executed] == [
        ("a", "succeeded"),
        ("b", "succeeded"),
        ("c", "succeeded"),
    ]
    b_run = client.get(f"/api/runs/{executed[1].id}").json()
    assert b_run["trigger"] == "upstream_update" and b_run["from_cache"] is False
    c_out = client.get("/api/agents/c/output").json()
    assert c_out["run"]["id"] == executed[2].id
    assert [i["verification_status"] for i in c_out["items"]] == ["verified"]
    assert all(_agent(client, x)["stale"] is False for x in "abc")
    assert _runs(db, "s") == []

    # 2. unchanged request: served from cache, no queue, no LLM calls
    llm_calls = db.scalar(select(func.count()).select_from(LlmCall))
    hit = client.post("/api/agents/c/runs", json={}).json()
    assert hit["from_cache"] is True and hit["reused_run_id"] == executed[2].id
    assert jobs.pending == []
    assert db.scalar(select(func.count()).select_from(LlmCall)) == llm_calls
    costs = client.get("/api/costs/summary").json()
    assert costs["runs_from_cache"] >= 1

    # 3. re-tag the reading as slides through the API: the reading chain
    #    re-runs (tag removed) and so does s (tag added)
    events.clear()
    response = client.put(
        f"/api/resource-items/{reading.id}/tags", json={"tags": ["slides"]}
    )
    assert response.status_code == 200
    executed = jobs.drain(db, llm)
    assert [r.agent_id for r in executed] == ["a", "s", "b", "c"]
    assert all(r.status == "succeeded" for r in executed)
    assert events == ["a", "s", "b", "c"]
    assert reg.get("c").module.SEEN[-1]["b"] == []  # b now has no reading
    assert all(_agent(client, x)["stale"] is False for x in "abcs")

    # 4. remove the (now slides-tagged) chapter through the API: only the
    #    slides branch re-runs; the reading chain's inputs are unchanged
    events.clear()
    assert client.delete(f"/api/resources/{reading.resource_id}").status_code == 204
    executed = jobs.drain(db, llm)
    assert [(r.agent_id, r.status) for r in executed] == [("s", "succeeded")]
    assert events == ["s"]
    assert all(_agent(client, x)["stale"] is False for x in "abcs")
