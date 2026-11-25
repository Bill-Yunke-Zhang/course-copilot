"""run_tool_loop contains a hijacked model (T5): allowlist, refusals, budget,
and a ToolCall trace row per requested call."""

import pytest

from app.core.llm import BoundLlm, FunctionCall, ToolTurn
from app.core.manifest import AgentContext
from app.core.tool_loop import run_tool_loop
from app.core.tools import ToolSpec, repo_tools
from app.core.untrusted import OPEN_TAG
from app.models import AgentRun, ToolCall
from tests.conftest import FakeLlm

REPO_TOOLS = ["list_files", "read_file", "git_log"]


def _turn(*calls: tuple[str, dict], text: str = "") -> ToolTurn:
    return ToolTurn(
        text=text,
        function_calls=[FunctionCall(name=n, args=a) for n, a in calls],
        input_tokens=10,
        output_tokens=5,
        model="gemini-3.5-flash-lite",
    )


def _answer(text: str) -> ToolTurn:
    return _turn(text=text)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("print('hi')\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n")
    (tmp_path / "secret.txt").write_text("outside\n")
    return root


@pytest.fixture
def make_ctx(db):
    """Build an AgentContext on a fresh AgentRun, scripted by `turns`."""

    def _make(turns, allowed=REPO_TOOLS):
        run = AgentRun(agent_id="probe", status="running")
        db.add(run)
        db.commit()
        fake = FakeLlm(tool_turns=turns)
        ctx = AgentContext(
            resources={},
            llm=BoundLlm(client=fake, db=db, agent_run_id=run.id),
            allowed_tools=list(allowed),
        )
        return ctx, fake, run

    return _make


def _trace(db, run):
    rows = db.query(ToolCall).filter_by(agent_run_id=run.id).order_by(ToolCall.step)
    return [(r.step, r.name, r.status) for r in rows]


def _tool_messages(fake):
    """The tool responses the model saw on its last turn."""
    return [m for m in fake.calls[-1]["messages"] if m["role"] == "tool"]


def test_answer_without_tool_calls_returns_text(make_ctx, repo, db):
    ctx, fake, run = make_ctx([_answer("all done")])
    result = run_tool_loop(ctx, "check the repo", repo_tools(repo))
    assert (result.text, result.stopped, result.calls) == ("all done", "answered", [])
    assert fake.calls[0]["messages"] == [{"role": "user", "text": "check the repo"}]
    assert _trace(db, run) == []


def test_allowed_call_runs_and_its_result_is_wrapped_as_untrusted(make_ctx, repo, db):
    ctx, fake, run = make_ctx(
        [_turn(("read_file", {"path": "src/main.py"})), _answer("looks fine")]
    )
    result = run_tool_loop(ctx, "read it", repo_tools(repo))
    assert result.stopped == "answered" and result.text == "looks fine"
    assert [(c.step, c.status) for c in result.calls] == [(1, "ok")]
    assert result.calls[0].result["content"] == "print('hi')\n"
    assert _trace(db, run) == [(1, "read_file", "ok")]

    (response,) = _tool_messages(fake)
    wrapped = response["response"]["result"]
    assert wrapped.startswith(f'{OPEN_TAG} source="tool:read_file">')
    assert "print(" in wrapped


def test_only_allowlisted_tools_are_declared(make_ctx, repo):
    ctx, fake, _ = make_ctx([_answer("ok")], allowed=["read_file"])
    run_tool_loop(ctx, "p", repo_tools(repo))
    assert fake.calls[0]["tools"] == ["read_file"]


def test_tool_not_on_the_allowlist_is_refused_and_never_run(make_ctx, db):
    ran = []
    spy = ToolSpec(
        name="git_log",
        description="spy",
        parameters={"type": "object", "properties": {}},
        handler=lambda args: ran.append(args) or {},
    )
    # git_log is passed in but the manifest only allows read_file; delete_repo
    # is not a tool at all.
    ctx, fake, run = make_ctx(
        [_turn(("git_log", {}), ("delete_repo", {"all": True})), _answer("ok")],
        allowed=["read_file"],
    )
    result = run_tool_loop(ctx, "p", [spy])
    assert ran == []
    assert [c.status for c in result.calls] == ["not_allowed", "not_allowed"]
    assert _trace(db, run) == [
        (1, "git_log", "not_allowed"),
        (2, "delete_repo", "not_allowed"),
    ]
    responses = _tool_messages(fake)
    assert all("not allowed" in m["response"]["error"] for m in responses)
    assert result.stopped == "answered"


@pytest.mark.parametrize(
    "path", ["../secret.txt", "../../etc/passwd", "/etc/passwd", ".git/config"]
)
def test_path_outside_the_repo_is_refused_and_the_loop_continues(
    make_ctx, repo, db, path
):
    ctx, fake, run = make_ctx(
        [
            _turn(("read_file", {"path": path})),
            _turn(("read_file", {"path": "src/main.py"})),
            _answer("done"),
        ]
    )
    result = run_tool_loop(ctx, "p", repo_tools(repo))
    assert [c.status for c in result.calls] == ["refused", "ok"]
    assert _trace(db, run) == [(1, "read_file", "refused"), (2, "read_file", "ok")]
    refusal = fake.calls[1]["messages"][-1]["response"]
    assert refusal["status"] == "refused"
    assert "outside" not in str(refusal)  # the secret file's content never leaks
    assert result.stopped == "answered"


def test_refusals_never_echo_the_requested_path_to_the_model(
    make_ctx, repo, db, tmp_path
):
    """A refused call sends back a reason only. The requested path is model-
    or attacker-chosen text (here it carries a sentinel), so it must not come
    back to the model; the trace still records the args."""
    sentinel = "SENTINEL-7f3a"
    (tmp_path / f"{sentinel}.txt").write_text(f"{sentinel} content\n")
    (repo / f"link-{sentinel}").symlink_to(tmp_path / f"{sentinel}.txt")
    requests = [
        ("read_file", {"path": f"../{sentinel}.txt"}),  # escapes
        ("read_file", {"path": str(tmp_path / f"{sentinel}.txt")}),  # absolute
        ("read_file", {"path": f"link-{sentinel}"}),  # symlink out
        ("read_file", {"path": f".git/{sentinel}"}),  # .git
        ("read_file", {"path": f"src/{sentinel}-missing.py"}),  # not a file
        ("list_files", {"path": f"src/{sentinel}-missing"}),  # not a directory
    ]
    ctx, fake, run = make_ctx([_turn(*requests), _answer("done")])
    result = run_tool_loop(ctx, "p", repo_tools(repo), max_steps=10)

    assert [c.status for c in result.calls] == ["refused"] * len(requests)
    responses = [m["response"] for m in _tool_messages(fake)]
    assert len(responses) == len(requests)
    for response, (_, args) in zip(responses, requests, strict=True):
        assert set(response) == {"status", "error"}
        assert response["status"] == "refused"
        assert args["path"] not in str(response)
        assert sentinel not in str(response)
    # The trace keeps what the model asked for.
    assert [c.args for c in result.calls] == [a for _, a in requests]


def test_call_spiral_stops_at_the_budget(make_ctx, repo, db):
    spiral = [_turn(("list_files", {})) for _ in range(50)]
    ctx, fake, run = make_ctx(spiral)
    result = run_tool_loop(ctx, "p", repo_tools(repo), max_steps=4)
    assert result.stopped == "step_limit"
    assert result.text == ""
    assert len(result.calls) == 4
    # One model turn per call, and no model call after the budget is spent.
    assert len(fake.calls) == 4
    assert _trace(db, run) == [(i, "list_files", "ok") for i in range(1, 5)]


def test_refused_and_unknown_calls_count_against_the_budget(make_ctx, repo, db):
    spiral = [_turn(("delete_repo", {})) for _ in range(10)]
    ctx, fake, run = make_ctx(spiral)
    result = run_tool_loop(ctx, "p", repo_tools(repo), max_steps=3)
    assert result.stopped == "step_limit"
    assert len(_trace(db, run)) == 3 and len(fake.calls) == 3


BUDGET_REASON = "step budget exhausted"


def _counting_tool(name="read_file", fail_on=()):
    """A ToolSpec that counts handler invocations; raises for paths in fail_on."""
    seen: list[str] = []

    def handler(args):
        seen.append(args.get("path"))
        if args.get("path") in fail_on:
            raise OSError(f"cannot read {args['path']}")
        return {"path": args.get("path"), "content": "x"}

    spec = ToolSpec(
        name=name,
        description="counting probe",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
        handler=handler,
    )
    return spec, seen


def test_calls_past_the_budget_in_one_turn_are_recorded_but_not_run(make_ctx, db):
    tool, seen = _counting_tool()
    paths = [f"f{i}.txt" for i in range(13)]
    turn = _turn(*[("read_file", {"path": p}) for p in paths])
    ctx, fake, run = make_ctx([turn, _answer("never reached")])

    result = run_tool_loop(ctx, "p", [tool], max_steps=10)

    assert result.stopped == "step_limit" and result.text == ""
    # Only the first 10 handlers ran; the 3 overflow calls never did.
    assert seen == paths[:10]
    # No model call after the budget was spent.
    assert len(fake.calls) == 1
    # All 13 requests are traced, in request order.
    assert _trace(db, run) == [(i, "read_file", "ok") for i in range(1, 11)] + [
        (i, "read_file", "refused") for i in range(11, 14)
    ]
    rows = db.query(ToolCall).filter_by(agent_run_id=run.id).order_by(ToolCall.step)
    assert [r.args["path"] for r in rows] == paths
    overflow = [r for r in rows if r.step > 10]
    assert all(BUDGET_REASON in r.result_preview for r in overflow)
    assert all("not run" in r.result_preview for r in overflow)
    assert [c.status for c in result.calls] == ["ok"] * 10 + ["refused"] * 3


def test_budget_overflow_after_ordinary_errors_and_refusals(make_ctx, repo, db):
    """Errors, refusals and not_allowed calls inside the budget keep their own
    statuses and count against it; only the calls after it are budget refusals."""
    tool, seen = _counting_tool(fail_on={"boom.txt"})
    turn = _turn(
        ("read_file", {"path": "boom.txt"}),  # handler raises → error
        ("git_log", {}),  # not in this agent's tools → not_allowed
        ("read_file", {"path": "ok.txt"}),  # runs → ok (budget now spent)
        ("read_file", {"path": "late-1.txt"}),  # overflow
        ("delete_repo", {}),  # overflow, even for an unknown tool
    )
    ctx, fake, run = make_ctx([turn], allowed=["read_file"])

    result = run_tool_loop(ctx, "p", [tool], max_steps=3)

    assert result.stopped == "step_limit" and len(fake.calls) == 1
    assert seen == ["boom.txt", "ok.txt"]
    assert _trace(db, run) == [
        (1, "read_file", "error"),
        (2, "git_log", "not_allowed"),
        (3, "read_file", "ok"),
        (4, "read_file", "refused"),
        (5, "delete_repo", "refused"),
    ]
    assert "cannot read boom.txt" in result.calls[0].error
    assert BUDGET_REASON not in (result.calls[0].error or "")
    assert all(BUDGET_REASON in c.error for c in result.calls[3:])


def test_turn_that_exactly_fills_the_budget_has_no_overflow_rows(make_ctx, db):
    tool, seen = _counting_tool()
    turn = _turn(*[("read_file", {"path": f"f{i}"}) for i in range(3)])
    ctx, fake, run = make_ctx([turn, _answer("never reached")])
    result = run_tool_loop(ctx, "p", [tool], max_steps=3)
    assert result.stopped == "step_limit" and len(fake.calls) == 1
    assert len(seen) == 3
    assert _trace(db, run) == [(i, "read_file", "ok") for i in range(1, 4)]


def test_handler_exception_becomes_an_error_record(make_ctx, db):
    def boom(args):
        raise OSError("disk on fire")

    broken = ToolSpec(
        name="read_file",
        description="broken",
        parameters={"type": "object", "properties": {}},
        handler=boom,
    )
    ctx, fake, run = make_ctx([_turn(("read_file", {"path": "x"})), _answer("ok")])
    result = run_tool_loop(ctx, "p", [broken])
    assert result.calls[0].status == "error"
    assert "disk on fire" in result.calls[0].error
    assert _trace(db, run) == [(1, "read_file", "error")]
    assert _tool_messages(fake)[0]["response"]["status"] == "error"
    assert result.stopped == "answered"


def test_non_object_arguments_are_an_error_not_a_crash(make_ctx, repo, db):
    ctx, _, run = make_ctx([_turn(("read_file", "src/main.py")), _answer("ok")])
    result = run_tool_loop(ctx, "p", repo_tools(repo))
    assert result.calls[0].status == "error"
    assert _trace(db, run) == [(1, "read_file", "error")]


def test_trace_is_persisted_before_a_later_failure(make_ctx, repo, db):
    class FailingLlm(FakeLlm):
        def _call_api_tools(self, model, messages, tools):
            if len(self.calls) >= 1:
                raise RuntimeError("model went away")
            return super()._call_api_tools(model, messages, tools)

    ctx, _, run = make_ctx([])
    ctx.llm.client = FailingLlm(tool_turns=[_turn(("list_files", {}))])
    with pytest.raises(RuntimeError, match="went away"):
        run_tool_loop(ctx, "p", repo_tools(repo))
    db.expire_all()
    assert _trace(db, run) == [(1, "list_files", "ok")]


def test_trace_endpoint_shows_the_whole_hijack(make_ctx, repo, db):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    ctx, _, run = make_ctx(
        [
            _turn(("delete_repo", {})),
            _turn(("read_file", {"path": "../../etc/passwd"})),
            _turn(("list_files", {})),
            _turn(("list_files", {})),
        ]
    )
    result = run_tool_loop(ctx, "p", repo_tools(repo), max_steps=3)
    assert result.stopped == "step_limit"

    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as client:
            body = client.get(f"/api/runs/{run.id}/tool-calls").json()
    finally:
        app.dependency_overrides.clear()
    assert [(c["step"], c["name"], c["status"]) for c in body] == [
        (1, "delete_repo", "not_allowed"),
        (2, "read_file", "refused"),
        (3, "list_files", "ok"),
    ]


def test_trace_endpoint_lists_every_overflow_request(make_ctx, db):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    tool, seen = _counting_tool()
    turn = _turn(*[("read_file", {"path": f"f{i}.txt"}) for i in range(13)])
    ctx, _, run = make_ctx([turn])
    run_tool_loop(ctx, "p", [tool], max_steps=10)

    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as client:
            body = client.get(f"/api/runs/{run.id}/tool-calls").json()
    finally:
        app.dependency_overrides.clear()
    assert len(seen) == 10
    assert [c["step"] for c in body] == list(range(1, 14))
    assert [c["args"]["path"] for c in body] == [f"f{i}.txt" for i in range(13)]
    assert [c["status"] for c in body] == ["ok"] * 10 + ["refused"] * 3
    assert all(BUDGET_REASON in c["result_preview"] for c in body[10:])
