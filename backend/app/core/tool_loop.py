"""The tool loop: let the model call tools, under the harness's rules.

    result = run_tool_loop(ctx, prompt, repo_tools(root), max_steps=6)

The model proposes; the harness disposes. Assume the model can be talked
into asking for anything — by a prompt-injected resource, or by its own
mistakes — and make the loop safe anyway: it runs only the tools this
agent's manifest allows, every tool confines its own arguments
(app/core/tools.py), the number of calls is capped, and every call is
recorded where a person can see it.

============================= STUDENT WORK (T5) ==============================
`run_tool_loop` below is yours. Grading drives it with a scripted model that
behaves like a hijacked one (calls to tools it was never given, paths
outside the repo, a call spiral) and checks that the loop contains it.
===============================================================================
"""

import json
from dataclasses import dataclass, field

from app.core.manifest import AgentContext
from app.core.tools import ToolRefused, ToolSpec
from app.core.untrusted import wrap_untrusted
from app.models import ToolCall

DEFAULT_MAX_STEPS = 8


@dataclass
class ToolCallRecord:
    step: int  # 1-based, in call order
    name: str
    args: dict
    # ok | not_allowed | refused | error
    status: str
    result: dict | None = None
    error: str | None = None


@dataclass
class ToolLoopResult:
    # The model's final answer; empty if the loop stopped at the limit.
    text: str
    calls: list[ToolCallRecord] = field(default_factory=list)
    # answered | step_limit
    stopped: str = "answered"


def run_tool_loop(
    ctx: AgentContext,
    prompt: str,
    tools: list[ToolSpec],
    max_steps: int = DEFAULT_MAX_STEPS,
) -> ToolLoopResult:
    """Run a tool-calling conversation for the agent that owns `ctx`.

    STUDENT WORK (T5). Required behavior:

    1. Allowlist. Only tools in BOTH `tools` and `ctx.allowed_tools` (the
       agent's manifest `tools` field) are declared to the model or run. A
       call to any other name is not run; record it as `not_allowed` and
       tell the model it was refused.
    2. Execution. Run an allowed call's handler with its args. A
       ToolRefused becomes a `refused` record; any other exception an
       `error` record. Either way the loop continues, and the model gets
       the reason back as the tool's response.
    3. Budget. Every requested call counts as one step, run or not. When
       `max_steps` calls have been made, stop: make no further model call
       and return `stopped="step_limit"`. Never loop forever.
       (Team policy: calls one turn requests beyond the budget are not run.
       Each one is still recorded, in request order, as a `refused` record
       whose reason says the step budget is exhausted.)
    4. Trace. Persist every call as a `ToolCall` row (app/models/tool_call.py)
       for `ctx.llm.agent_run_id`, as it happens, so
       GET /api/runs/{run_id}/tool-calls shows the whole trace even if the
       run fails partway.
    5. Ending. When the model answers without function calls, return its
       text with `stopped="answered"`.

    Use `ctx.llm.generate_with_tools(messages, declarations)`; the message
    format is documented in app/core/llm.py. Results you send back become
    model input, so keep them small (the repo tools already cap theirs).
    """
    llm = ctx.llm
    # Allowlist: only tools the caller passed AND the manifest names are
    # declared to the model or run; everything else is refused by name.
    allowed = {t.name: t for t in tools if t.name in set(ctx.allowed_tools)}
    declarations = [t.declaration() for t in allowed.values()]

    messages: list[dict] = [{"role": "user", "text": prompt}]
    calls: list[ToolCallRecord] = []
    # Calls processed within the budget (run, refused or not allowed). Calls
    # the model requests after it is spent are recorded but don't count.
    used = 0

    while True:
        turn = llm.generate_with_tools(messages, declarations)
        if not turn.function_calls:
            return ToolLoopResult(text=turn.text, calls=calls, stopped="answered")

        messages.append(
            {"role": "model", "text": turn.text, "function_calls": turn.function_calls}
        )
        for call in turn.function_calls:
            if used >= max_steps:
                # Past the budget in this same turn: never run, but still
                # traced, in request order, as a refusal, so the trace shows
                # every call the model asked for.
                record = _over_budget(len(calls) + 1, call.name, call.args, max_steps)
            else:
                used += 1
                record = _run_one(len(calls) + 1, call.name, call.args, allowed)
            calls.append(record)
            _persist(llm, record)
            messages.append(
                {"role": "tool", "name": call.name, "response": _response_for(record)}
            )
        if used >= max_steps:
            # Budget spent: no further model call, however the model asks.
            return ToolLoopResult(text="", calls=calls, stopped="step_limit")


# Keep the trace preview and the error text sent back to the model short.
PREVIEW_CHARS = 300
ERROR_CHARS = 300


def _run_one(
    step: int, name: str, args: object, allowed: dict[str, ToolSpec]
) -> ToolCallRecord:
    """Run (or refuse) one requested call and describe what happened."""
    stored_args = args if isinstance(args, dict) else {"_raw": repr(args)}
    tool = allowed.get(name)
    if tool is None:
        return ToolCallRecord(
            step=step,
            name=name,
            args=stored_args,
            status="not_allowed",
            error=f"tool {name!r} is not allowed for this agent",
        )
    if not isinstance(args, dict):
        return ToolCallRecord(
            step=step,
            name=name,
            args=stored_args,
            status="error",
            error="arguments must be a JSON object",
        )
    try:
        result = tool.handler(args)
    except ToolRefused as exc:
        return ToolCallRecord(
            step=step, name=name, args=args, status="refused", error=str(exc)
        )
    except Exception as exc:  # a broken tool must not end the loop
        message = f"{type(exc).__name__}: {exc}"[:ERROR_CHARS]
        return ToolCallRecord(
            step=step, name=name, args=args, status="error", error=message
        )
    return ToolCallRecord(step=step, name=name, args=args, status="ok", result=result)


def _over_budget(step: int, name: str, args: object, max_steps: int) -> ToolCallRecord:
    """A call requested after the step budget was spent: refused, not run."""
    return ToolCallRecord(
        step=step,
        name=name,
        args=args if isinstance(args, dict) else {"_raw": repr(args)},
        status="refused",
        error=(
            f"step budget exhausted ({max_steps} of {max_steps} calls used); "
            "this call was not run"
        ),
    )


def _response_for(record: ToolCallRecord) -> dict:
    """What the model sees back. Tool output is repo text, so it goes through
    wrap_untrusted like any other resource content."""
    if record.status == "ok":
        body = json.dumps(record.result, ensure_ascii=False, default=str)
        return {"result": wrap_untrusted(body, source=f"tool:{record.name}")}
    return {"status": record.status, "error": record.error}


def _persist(llm, record: ToolCallRecord) -> None:
    """Write the trace row now, so a run that fails later still shows it."""
    if record.status == "ok":
        preview = json.dumps(record.result, ensure_ascii=False, default=str)
    else:
        preview = record.error or ""
    llm.db.add(
        ToolCall(
            agent_run_id=llm.agent_run_id,
            step=record.step,
            name=record.name[:128],
            args=record.args,
            status=record.status,
            result_preview=preview[:PREVIEW_CHARS],
        )
    )
    llm.db.commit()
