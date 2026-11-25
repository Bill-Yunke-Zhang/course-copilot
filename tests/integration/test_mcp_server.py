"""The MCP server (T7) through the SDK's real in-process Client.

Every check goes over the protocol — `Client(mcp)` against the actual
`MCPServer` instance — so tool/resource discovery, structured results and
error mapping are what an MCP client sees, not what a Python helper
returns. SQLite + recorded queue + FakeLlm, as everywhere else.
"""

import asyncio
import json
import sys
import textwrap
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

import app.mcp_server as mcp_server
from app.core import orchestrator
from app.models import (
    AgentRun,
    Credential,
    LlmCall,
    OutputItem,
    QuizAttempt,
    ToolCall,
)
from tests.conftest import make_resource
from tests.integration.test_quiz_api import NOTES, _fake

AGENTS_DIR = Path(__file__).resolve().parents[2] / "backend" / "app" / "agents"
TEMP_AGENT = "tmp_mcp_mystery_agent"
PLANTED_PAT = "glpat-PLANTED-mcp-test-token-0123456789"
RAW_REPO_TEXT = "PRIVATE_REPO_RAW_FILE def secret_solution(): return 42"


# ── Fixtures and helpers ─────────────────────────────────────────────────────


@pytest.fixture
def server(db, enqueued, registry, monkeypatch):
    """The real MCPServer, reading the test database, with today pinned."""
    monkeypatch.setattr(
        mcp_server,
        "SessionLocal",
        sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False),
    )
    today = date.today()  # captured once so a test can't straddle midnight
    monkeypatch.setattr(mcp_server, "_today", lambda: today)
    return mcp_server.mcp


@pytest.fixture
def temp_agent(registry):
    """A mystery agent dropped into app/agents/, as grading does."""
    from app.core.registry import get_registry, reset_registry

    path = AGENTS_DIR / f"{TEMP_AGENT}.py"
    path.write_text(
        textwrap.dedent(
            f"""
            from app.core.manifest import AgentContext, AgentManifest

            MANIFEST = AgentManifest(
                agent_id="{TEMP_AGENT}",
                display_name="Temp",
                description="dropped in by the MCP test",
                required_resource_tags=["syllabus"],
                optional_upstream_agents=[],
                output_archetype="text_block",
                trigger="on_demand",
                cacheable=False,
                dashboard_widget=False,
            )

            def run(ctx: AgentContext):
                return []
            """
        )
    )
    try:
        reset_registry()
        get_registry()
        yield TEMP_AGENT
    finally:
        path.unlink(missing_ok=True)
        sys.modules.pop(f"app.agents.{TEMP_AGENT}", None)
        reset_registry()


def mcp_call(server, fn):
    """Run `await fn(client)` against the server over an in-process Client."""

    async def go():
        async with Client(server) as client:
            return await fn(client)

    return asyncio.run(go())


def call_tool(server, name, args=None):
    return mcp_call(server, lambda c: c.call_tool(name, args or {}))


def read_resource(server, uri):
    async def go(client):
        result = await client.read_resource(uri, cache_mode="bypass")
        return json.loads(result.contents[0].text)

    return mcp_call(server, go)


def ref(item_id, excerpt="cited text"):
    return {"resource_item_id": item_id, "excerpt": excerpt}


def add_run(db, agent_id, items=(), status="succeeded", **kw):
    """A run with (position, payload, verification_status) items."""
    run = AgentRun(
        agent_id=agent_id,
        status=status,
        trigger="on_demand",
        finished_at=datetime.utcnow() if status in ("succeeded", "failed") else None,
        verification_summary={
            "items_total": len(items),
            "items_dropped": sum(1 for *_, s in items if s == "dropped"),
            "notes": [],
        },
        **kw,
    )
    for position, payload, verification in items:
        run.items.append(
            OutputItem(
                archetype=payload.pop("_archetype", "text_block"),
                position=position,
                payload=payload,
                verification_status=verification,
            )
        )
    db.add(run)
    db.commit()
    return run


def text_item(heading, item_id):
    return {
        "heading": heading,
        "body": f"{heading} body",
        "source_refs": [ref(item_id)],
    }


def deadline(when, title, item_id, excerpt):
    return {
        "_archetype": "timeline_item",
        "date": when if isinstance(when, str) else when.isoformat(),
        "title": title,
        "description": f"{title} details",
        "source_refs": [ref(item_id, excerpt)],
    }


@pytest.fixture
def schedule_id(db):
    resource = make_resource(
        db,
        locator="https://example.edu/schedule",
        items=[
            {"title": "Course schedule", "content": "P1 due ...", "tags": ["schedule"]}
        ],
    )
    return resource.items[0].id


def counts(db, enqueued):
    db.expire_all()
    return {
        "runs": db.scalar(select(func.count()).select_from(AgentRun)),
        "items": db.scalar(select(func.count()).select_from(OutputItem)),
        "llm_calls": db.scalar(select(func.count()).select_from(LlmCall)),
        "tool_calls": db.scalar(select(func.count()).select_from(ToolCall)),
        "attempts": db.scalar(select(func.count()).select_from(QuizAttempt)),
        "jobs": len(enqueued),
    }


# ── Discovery ────────────────────────────────────────────────────────────────


def test_lists_exactly_the_three_tools_and_the_output_resource(server):
    async def go(client):
        return (
            await client.list_tools(),
            await client.list_resource_templates(),
            await client.list_resources(),
        )

    tools, templates, resources = mcp_call(server, go)
    by_name = {t.name: t for t in tools.tools}
    assert set(by_name) == {"list_deadlines", "get_agent_output", "request_quiz"}
    assert by_name["list_deadlines"].annotations.read_only_hint is True
    assert by_name["get_agent_output"].annotations.read_only_hint is True
    assert by_name["request_quiz"].annotations.read_only_hint is False
    assert by_name["get_agent_output"].input_schema["required"] == ["agent_id"]
    assert by_name["request_quiz"].input_schema["required"] == ["text"]
    assert [t.uri_template for t in templates.resource_templates] == [
        "copilot://agents/{agent_id}/output"
    ]
    assert resources.resources == []  # no static resources (e.g. raw files)


# ── list_deadlines ───────────────────────────────────────────────────────────


def test_list_deadlines_upcoming_verified_with_citations(server, db, schedule_id):
    today = mcp_server._today()
    add_run(
        db,
        "deadline_agent",
        [
            (
                0,
                deadline(today - timedelta(days=1), "P0 due", schedule_id, "P0"),
                "verified",
            ),
            (
                1,
                deadline(today + timedelta(days=7), "P2 due", schedule_id, "P2"),
                "verified",
            ),
            (2, deadline(today, "P1 due", schedule_id, "P1 due today"), "verified"),
            (
                3,
                deadline(today + timedelta(days=3), "Fake exam", schedule_id, "x"),
                "dropped",
            ),
            (4, deadline("sometime in May", "Vague", schedule_id, "May"), "verified"),
        ],
    )

    result = call_tool(server, "list_deadlines")
    assert result.is_error is False
    body = result.structured_content
    assert body["today"] == today.isoformat() and body["includes_today"] is True
    assert body["has_output"] is True
    # Sorted by date; today included; past, dropped and undated left out.
    assert [(d["date"], d["title"]) for d in body["deadlines"]] == [
        (today.isoformat(), "P1 due"),
        ((today + timedelta(days=7)).isoformat(), "P2 due"),
    ]
    assert body["deadlines"][0]["citations"] == [
        {
            "resource_item_id": schedule_id,
            "excerpt": "P1 due today",
            "source_title": "Course schedule",
        }
    ]
    assert body["excluded_past"] == 1 and body["excluded_undated"] == 1


def test_list_deadlines_resolves_cache_hit_and_ignores_newer_failed_run(
    server, db, schedule_id
):
    today = mcp_server._today()
    real = add_run(
        db,
        "deadline_agent",
        [
            (
                0,
                deadline(today + timedelta(days=2), "P3 due", schedule_id, "P3"),
                "verified",
            )
        ],
    )
    hit = add_run(db, "deadline_agent", from_cache=True, reused_run_id=real.id)
    add_run(db, "deadline_agent", status="failed", error="boom")

    body = call_tool(server, "list_deadlines").structured_content
    assert [d["title"] for d in body["deadlines"]] == ["P3 due"]
    assert body["run"]["run_id"] == real.id
    assert body["run"]["requested_run_id"] == hit.id
    assert body["run"]["from_cache"] is True


def test_list_deadlines_without_output_is_honestly_empty(server, db, enqueued):
    add_run(db, "deadline_agent", status="failed", error="no schedule")
    before = counts(db, enqueued)
    result = call_tool(server, "list_deadlines")
    assert result.is_error is False
    assert result.structured_content["has_output"] is False
    assert result.structured_content["deadlines"] == []
    assert counts(db, enqueued) == before  # reading never triggers generation


# ── get_agent_output and the resource ────────────────────────────────────────


def test_output_is_verified_only_in_position_order_for_a_temp_agent(
    server, db, temp_agent, schedule_id
):
    add_run(
        db,
        temp_agent,
        [
            (2, text_item("Third", schedule_id), "verified"),
            (0, text_item("First", schedule_id), "verified"),
            (1, text_item("Unsupported", schedule_id), "dropped"),
            (3, text_item("Fourth", schedule_id), "verified"),
        ],
    )
    result = call_tool(server, "get_agent_output", {"agent_id": temp_agent})
    assert result.is_error is False
    body = result.structured_content
    assert body["agent_id"] == temp_agent and body["has_output"] is True
    assert [i["payload"]["heading"] for i in body["items"]] == [
        "First",
        "Third",
        "Fourth",
    ]
    assert [i["position"] for i in body["items"]] == [0, 2, 3]
    assert all(i["payload"]["source_refs"] for i in body["items"])
    assert body["run"]["items_dropped"] == 1


def test_cache_hit_resolves_and_newer_failed_or_queued_run_does_not_hide_output(
    server, db, schedule_id
):
    agent = "concept_coverage_agent"
    add_run(db, agent, [(0, text_item("Old", schedule_id), "verified")])
    real = add_run(db, agent, [(0, text_item("Current", schedule_id), "verified")])
    hit = add_run(db, agent, from_cache=True, reused_run_id=real.id)
    add_run(db, agent, status="failed", error="LLM timeout")
    add_run(db, agent, status="queued")

    body = call_tool(server, "get_agent_output", {"agent_id": agent}).structured_content
    assert [i["payload"]["heading"] for i in body["items"]] == ["Current"]
    assert body["run"] == {
        "run_id": real.id,
        "requested_run_id": hit.id,
        "from_cache": True,
        "finished_at": real.finished_at.isoformat(),
        "items_total": 1,
        "items_dropped": 0,
    }


def test_known_agent_without_output_is_empty_not_an_error(server, temp_agent):
    result = call_tool(server, "get_agent_output", {"agent_id": temp_agent})
    assert result.is_error is False
    assert result.structured_content == {
        "agent_id": temp_agent,
        "has_output": False,
        "run": None,
        "items": [],
    }
    assert read_resource(server, f"copilot://agents/{temp_agent}/output") == (
        result.structured_content
    )


def test_unknown_agent_is_a_tool_error_and_a_resource_error(server):
    result = call_tool(server, "get_agent_output", {"agent_id": "nope"})
    assert result.is_error is True
    message = result.content[0].text
    assert "Unknown agent_id 'nope'" in message and "deadline_agent" in message

    async def read_unknown(client):
        with pytest.raises(MCPError) as excinfo:
            await client.read_resource("copilot://agents/nope/output")
        return excinfo.value

    error = mcp_call(server, read_unknown)
    assert error.code == -32602  # invalid params: no such resource
    assert "Unknown agent_id 'nope'" in str(error)


def test_tool_and_resource_agree(server, db, schedule_id):
    today = mcp_server._today()
    real = add_run(
        db,
        "deadline_agent",
        [
            (1, deadline(today + timedelta(days=1), "B", schedule_id, "B"), "verified"),
            (0, deadline(today + timedelta(days=4), "A", schedule_id, "A"), "verified"),
            (2, deadline(today, "dropped", schedule_id, "x"), "dropped"),
        ],
    )
    add_run(db, "deadline_agent", from_cache=True, reused_run_id=real.id)
    add_run(db, "deadline_agent", status="failed", error="boom")

    tool = call_tool(server, "get_agent_output", {"agent_id": "deadline_agent"})
    resource = read_resource(server, "copilot://agents/deadline_agent/output")
    assert resource == tool.structured_content
    assert [i["payload"]["title"] for i in resource["items"]] == ["A", "B"]


# ── request_quiz ─────────────────────────────────────────────────────────────


@pytest.fixture
def notes_id(db):
    resource = make_resource(
        db,
        kind="file",
        locator="/data/notes/lecture2.md",
        items=[
            {
                "title": "Lecture 2 notes",
                "content": NOTES,
                "tags": ["personal-notes"],
                "meta": {"lecture": 2, "unit": 1},
            }
        ],
    )
    return resource.items[0].id


def test_request_quiz_enqueues_the_quiz_page_request_then_serves_cache(
    server, db, enqueued, notes_id
):
    first = call_tool(server, "request_quiz", {"text": "  Quiz me on   unit 1  "})
    assert first.is_error is False
    body = first.structured_content
    assert body["status"] == "queued" and body["from_cache"] is False
    assert body["agent_id"] == "quiz_agent"
    run_id = body["run_id"]
    # Same queue path and payload shape as POST /api/quiz/runs.
    assert enqueued == [("agents", "run_agent_job", (run_id,))]
    db.expire_all()
    row = db.get(AgentRun, run_id)
    assert row.trigger == "on_demand"
    assert row.request_payload == {"text": "Quiz me on unit 1"}
    # Nothing generated synchronously, no attempt created.
    assert db.scalar(select(func.count()).select_from(LlmCall)) == 0
    assert db.scalar(select(func.count()).select_from(OutputItem)) == 0
    assert db.scalar(select(func.count()).select_from(QuizAttempt)) == 0

    # The worker runs it; the identical request is then a cache hit.
    orchestrator.execute_run(db, _fake(notes_id), run_id)
    llm_calls = db.scalar(select(func.count()).select_from(LlmCall))
    again = call_tool(server, "request_quiz", {"text": "Quiz me on unit 1"})
    assert again.structured_content["status"] == "succeeded"
    assert again.structured_content["from_cache"] is True
    assert again.structured_content["reused_run_id"] == run_id
    assert len(enqueued) == 1  # no new job
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(LlmCall)) == llm_calls
    assert db.scalar(select(func.count()).select_from(QuizAttempt)) == 0

    # The verified questions are then readable through the read tool.
    out = call_tool(server, "get_agent_output", {"agent_id": "quiz_agent"})
    assert out.structured_content["run"]["run_id"] == run_id
    assert out.structured_content["run"]["from_cache"] is True
    assert len(out.structured_content["items"]) == 2  # one question was dropped


@pytest.mark.parametrize("text", ["", "   ", "x" * 1001])
def test_invalid_quiz_text_is_a_tool_error(server, db, enqueued, text):
    before = counts(db, enqueued)
    result = call_tool(server, "request_quiz", {"text": text})
    assert result.is_error is True
    assert result.content[0].text  # the quiz service's own message
    assert counts(db, enqueued) == before


def test_missing_quiz_text_is_a_tool_error(server):
    result = call_tool(server, "request_quiz", {})
    assert result.is_error is True


def test_enqueue_failure_is_a_tool_error(server, monkeypatch):
    import app.core.queue as queue_module

    def broken(*args, **kwargs):
        raise ConnectionError("redis down")

    monkeypatch.setattr(queue_module, "enqueue", broken)
    result = call_tool(server, "request_quiz", {"text": "Quiz me on unit 1"})
    assert result.is_error is True
    assert "Could not start the quiz run" in result.content[0].text


# ── Least privilege ──────────────────────────────────────────────────────────


def test_reads_create_no_runs_jobs_or_llm_calls(
    server, db, enqueued, temp_agent, schedule_id, monkeypatch
):
    add_run(
        db,
        "deadline_agent",
        [(0, deadline(date.today(), "P", schedule_id, "P"), "verified")],
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("a read operation tried to start or execute a run")

    monkeypatch.setattr(orchestrator, "request_run", forbidden)
    monkeypatch.setattr(orchestrator, "execute_run", forbidden)
    monkeypatch.setattr(mcp_server.quiz_service, "request_run", forbidden)
    before = counts(db, enqueued)

    async def go(client):
        results = [
            await client.call_tool("list_deadlines", {}),
            await client.call_tool("get_agent_output", {"agent_id": "deadline_agent"}),
            await client.call_tool("get_agent_output", {"agent_id": temp_agent}),
            await client.call_tool("get_agent_output", {"agent_id": "quiz_agent"}),
            await client.call_tool("get_agent_output", {"agent_id": "nope"}),
        ]
        await client.read_resource("copilot://agents/deadline_agent/output")
        await client.read_resource(f"copilot://agents/{temp_agent}/output")
        return results

    results = mcp_call(server, go)
    assert [r.is_error for r in results] == [False, False, False, False, True]
    assert counts(db, enqueued) == before


def test_responses_never_expose_credentials_or_raw_repo_content(
    server, db, schedule_id
):
    db.add(
        Credential(credential_ref="gitlab-pat", encrypted_value=PLANTED_PAT.encode())
    )
    repo = make_resource(
        db,
        kind="gitlab_repo",
        locator="https://git.example.edu/team/private-repo",
        items=[
            {
                "path": "src/solution.py",
                "title": "src/solution.py",
                "content": RAW_REPO_TEXT,
                "tags": ["project-code"],
            }
        ],
    )
    code_id = repo.items[0].id
    add_run(
        db,
        "repo_progress_agent",
        [
            (
                0,
                {
                    "_archetype": "status_item",
                    "label": "Part 1",
                    "detail": "implemented",
                    "source_refs": [ref(code_id, "def secret_solution")],
                },
                "verified",
            )
        ],
    )
    add_run(
        db,
        "deadline_agent",
        [(0, deadline(date.today(), "P", code_id, "P"), "verified")],
    )

    async def go(client):
        texts = []
        for tool in (await client.list_tools()).tools:
            texts.append(tool.model_dump_json())
        for args in (
            ("list_deadlines", {}),
            ("get_agent_output", {"agent_id": "repo_progress_agent"}),
            ("get_agent_output", {"agent_id": "deadline_agent"}),
        ):
            texts.append((await client.call_tool(*args)).model_dump_json())
        for agent in ("repo_progress_agent", "deadline_agent"):
            uri = f"copilot://agents/{agent}/output"
            texts.append((await client.read_resource(uri)).model_dump_json())
        return "\n".join(texts)

    everything = mcp_call(server, go)
    assert "secret_solution" in everything  # the cited excerpt is fine…
    assert RAW_REPO_TEXT not in everything  # …the raw file is not
    assert "return 42" not in everything
    assert PLANTED_PAT not in everything
    assert "glpat-" not in everything
