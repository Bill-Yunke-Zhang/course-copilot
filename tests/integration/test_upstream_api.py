"""Stale upstream through the HTTP API (SQLite, queue recorded, FakeLlm).

up reads only `reading`; down reads only `slides` and depends on up. Editing
the reading must make down stale and force a real run that refreshes up
first — not a cache hit on down's unchanged fingerprint.
"""

import sys
import textwrap
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.core.registry as registry_module
from app.core import orchestrator
from app.core.ingestion.extract import content_hash
from app.core.registry import AgentRegistry
from app.database import get_db
from app.main import app
from app.models import AgentRun, LlmCall, OutputItem
from tests.conftest import FakeLlm, make_resource
from tests.unit.test_upstream import downstream_src, upstream_src

JUDGE_OK = {"_JudgeResponse": {"supported": True, "reason": "excerpt matches"}}
READING_TEXT = "Chapter 3 covers consistent hashing and virtual nodes."
NEW_READING = "Chapter 3 (revised) covers consistent hashing with bounded loads."
SLIDES_TEXT = "Lecture 9 slides: virtual nodes smooth out the key distribution."


@pytest.fixture
def client(db, enqueued, registry):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def up_down_agents(client, tmp_path, monkeypatch):
    """Temp package with up/down, installed after app startup loaded agents."""
    name = f"upstream_api_agents_{uuid.uuid4().hex[:8]}"
    pkg_dir = tmp_path / name
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    (pkg_dir / "up.py").write_text(textwrap.dedent(upstream_src("up", tag="reading")))
    (pkg_dir / "down.py").write_text(
        textwrap.dedent(downstream_src("down", ["up"], tag="slides"))
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    reg = AgentRegistry(package=name)
    monkeypatch.setattr(registry_module, "_registry", reg)
    yield reg
    for mod in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
        del sys.modules[mod]


def _llm_calls(db):
    return db.scalar(select(func.count()).select_from(LlmCall))


def _agent(client, agent_id):
    return next(
        a
        for a in client.get("/api/agents").json()
        if a["manifest"]["agent_id"] == agent_id
    )


def _up_runs(db):
    return db.scalars(
        select(AgentRun).where(AgentRun.agent_id == "up").order_by(AgentRun.id)
    ).all()


def test_stale_upstream_refreshes_through_the_api(client, db, enqueued, up_down_agents):
    reading = make_resource(
        db,
        locator="https://example.edu/ch3.html",
        items=[{"title": "Ch3", "content": READING_TEXT, "tags": ["reading"]}],
    ).items[0]
    make_resource(
        db,
        locator="https://example.edu/lec9.pdf",
        items=[{"title": "Lec9", "content": SLIDES_TEXT, "tags": ["slides"]}],
    )
    llm = FakeLlm(JUDGE_OK)

    # 1. initial request queues a run; the worker job executes it
    first = client.post("/api/agents/down/runs", json={}).json()
    assert first["status"] == "queued" and first["from_cache"] is False
    assert enqueued == [("agents", "run_agent_job", (first["id"],))]
    assert orchestrator.execute_run(db, llm, first["id"]).status == "succeeded"
    assert _agent(client, "down")["stale"] is False
    first_up_id = _up_runs(db)[-1].id

    # 2. only the upstream's resource changes (fixture mutation, no endpoint)
    reading.content_text = NEW_READING
    reading.content_hash = content_hash(NEW_READING)
    db.commit()

    assert _agent(client, "up")["stale"] is True
    assert _agent(client, "down")["stale"] is True

    # 3. a normal request for down is NOT served from cache
    second = client.post("/api/agents/down/runs", json={}).json()
    assert second["from_cache"] is False and second["status"] == "queued"
    assert enqueued[-1] == ("agents", "run_agent_job", (second["id"],))
    assert [r.id for r in _up_runs(db)] == [first_up_id]  # nothing ran yet

    # 4. executing it refreshes up before down; down sees the new reading
    run = orchestrator.execute_run(db, llm, second["id"])
    assert run.status == "succeeded", run.error
    new_up = _up_runs(db)[-1]
    assert new_up.id != first_up_id and new_up.status == "succeeded"
    assert new_up.finished_at <= run.finished_at
    seen = up_down_agents.get("down").module.SEEN[-1]["up"]
    assert [p["body"] for p in seen] == [NEW_READING]
    assert _agent(client, "down")["stale"] is False

    # 5. the output endpoint returns only verified items
    up_rows = db.scalars(
        select(OutputItem).where(OutputItem.agent_run_id == new_up.id)
    ).all()
    assert sorted(r.verification_status for r in up_rows) == ["dropped", "verified"]
    up_out = client.get("/api/agents/up/output").json()
    assert up_out["run"]["id"] == new_up.id
    assert [i["payload"]["body"] for i in up_out["items"]] == [NEW_READING]
    down_out = client.get("/api/agents/down/output").json()
    assert down_out["run"]["id"] == run.id
    assert [i["verification_status"] for i in down_out["items"]] == ["verified"]

    # 6. unchanged follow-up: cache hit, no enqueue, no LLM calls
    enqueued_before, db_calls, fake_calls = (
        len(enqueued),
        _llm_calls(db),
        len(llm.calls),
    )
    hit = client.post("/api/agents/down/runs", json={}).json()
    assert hit["from_cache"] is True and hit["reused_run_id"] == run.id
    assert len(enqueued) == enqueued_before
    assert _llm_calls(db) == db_calls
    assert len(llm.calls) == fake_calls
