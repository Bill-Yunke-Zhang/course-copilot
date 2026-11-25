"""Tool calling: the LLM plumbing, tool declarations and trace endpoint ship working.

Containment (resolve_inside, wrap_untrusted, run_tool_loop) is student work and
is exercised by the team's own tests and by grading."""

import pytest

from app.core.llm import FunctionCall, ToolTurn
from app.core.manifest import AgentManifest
from app.core.tools import KNOWN_TOOLS, repo_tools
from app.models import AgentRun, LlmCall, ToolCall
from tests.conftest import FakeLlm


def test_repo_tools_declare_only_what_the_model_needs(tmp_path):
    tools = repo_tools(tmp_path)
    assert {t.name for t in tools} == KNOWN_TOOLS
    for tool in tools:
        assert set(tool.declaration()) == {"name", "description", "parameters"}
        assert tool.parameters["type"] == "object"
        assert not tool.changes_state


def test_manifest_tools_must_be_known():
    base = dict(
        agent_id="a",
        display_name="A",
        description="d",
        required_resource_tags=["project-code"],
        output_archetype="status_item",
        trigger="on_demand",
        cacheable=False,
        dashboard_widget=False,
    )
    assert AgentManifest(**base).tools == []
    assert AgentManifest(**base, tools=["read_file"]).tools == ["read_file"]
    with pytest.raises(ValueError, match="unknown tools"):
        AgentManifest(**base, tools=["delete_repo"])


def test_generate_with_tools_logs_cost_and_returns_calls(db):
    turn = ToolTurn(
        text="",
        function_calls=[FunctionCall(name="read_file", args={"path": "README.md"})],
        input_tokens=120,
        output_tokens=30,
        model="gemini-3.5-flash-lite",
    )
    llm = FakeLlm(tool_turns=[turn])
    got = llm.generate_with_tools(
        db,
        purpose="generation",
        messages=[{"role": "user", "text": "hi"}],
        tools=[t.declaration() for t in repo_tools(".")],
    )
    assert got.function_calls[0].name == "read_file"
    row = db.query(LlmCall).one()
    assert (row.input_tokens, row.output_tokens) == (120, 30)
    assert row.cost_usd > 0


def test_tool_call_trace_endpoint(db, enqueued, registry):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    run = AgentRun(agent_id="probe", status="succeeded")
    db.add(run)
    db.flush()
    db.add_all(
        [
            ToolCall(
                agent_run_id=run.id,
                step=2,
                name="read_file",
                args={"path": "../x"},
                status="refused",
                result_preview="escapes",
            ),
            ToolCall(
                agent_run_id=run.id,
                step=1,
                name="delete_repo",
                args={},
                status="not_allowed",
                result_preview="",
            ),
        ]
    )
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as client:
            body = client.get(f"/api/runs/{run.id}/tool-calls").json()
            assert [c["name"] for c in body] == ["delete_repo", "read_file"]
            assert body[1]["status"] == "refused"
            assert client.get("/api/runs/999999/tool-calls").status_code == 404
    finally:
        app.dependency_overrides.clear()
