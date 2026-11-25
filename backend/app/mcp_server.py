"""Course Copilot as an MCP server, so any MCP client can use it.

Any MCP client (the MCP Inspector, an IDE, another agent) can ask Course
Copilot "what's due this week?" the same way it calls its own tools. The
server runs locally over stdio, inside the backend container, started by
the client on demand:

    docker compose exec -T backend python -m app.mcp_server

stdio means the protocol owns this process's stdout: never print() here.
Log to stderr if you must.

What it exposes (T7):

- `list_deadlines()`      read-only: upcoming items of the Deadline agent's
                          latest verified output.
- `get_agent_output(id)`  read-only: any registered agent's latest
                          verified output.
- `request_quiz(text)`    the one write: starts a quiz run through
                          `quiz_service.start_quiz`, exactly as the Quiz
                          page does (same payload, cache and queue path).
- `copilot://agents/{agent_id}/output`
                          the same selection as `get_agent_output`, as a
                          JSON resource (both go through `_agent_output`).

Reads never run an agent, enqueue a job or call an LLM, even when output
is missing or stale: they only SELECT through the same models and helpers
the API uses (`orchestrator.latest_output_run`). Only verified items and
the run metadata needed to interpret them are returned — never run
errors, request payloads of other agents, credentials, or resource
content beyond the short excerpts an item already cites.
"""

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp.types import ToolAnnotations
from sqlalchemy.orm import Session

from app import quiz_service
from app.core.orchestrator import latest_output_run
from app.core.registry import get_registry
from app.database import SessionLocal
from app.models import AgentRun, ResourceItem

logger = logging.getLogger("app.mcp_server")

DEADLINE_AGENT_ID = "deadline_agent"

mcp = MCPServer(
    "course-copilot",
    instructions=(
        "Course Copilot's verified, cited course data. Reads return only "
        "output that already passed grounding verification and never start "
        "agent runs. request_quiz is the only tool that changes anything: "
        "it starts (or serves from cache) one quiz run."
    ),
)

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
_STARTS_RUN = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)


# ── Shared reading logic ─────────────────────────────────────────────────────


@contextmanager
def _session() -> Iterator[Session]:
    """One short DB session per call, always closed (reads never commit)."""
    with SessionLocal() as db:
        yield db


def _today() -> date:
    """The date "upcoming" is measured against (the server's local date).

    One function so every deadline filter uses the same source and tests can
    pin it.
    """
    return date.today()


class UnknownAgent(LookupError):
    pass


def _require_agent(agent_id: str) -> None:
    registry = get_registry()
    try:
        registry.get(agent_id)
    except KeyError:
        known = ", ".join(a.manifest.agent_id for a in registry.all())
        raise UnknownAgent(
            f"Unknown agent_id {agent_id!r}. Registered agents: {known}."
        ) from None


def _run_meta(requested: AgentRun | None, source: AgentRun) -> dict[str, Any]:
    summary = source.verification_summary or {}
    return {
        "run_id": source.id,
        "requested_run_id": requested.id if requested else source.id,
        "from_cache": bool(requested and requested.from_cache),
        "finished_at": (source.finished_at.isoformat() if source.finished_at else None),
        "items_total": summary.get("items_total"),
        "items_dropped": summary.get("items_dropped"),
    }


def _latest_verified(
    db: Session, agent_id: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """(run metadata, verified items in position order) — or (None, []).

    Uses the API's own `latest_output_run`: the newest *succeeded* run, so a
    newer queued/running/failed run never hides the last good output, and a
    cache hit resolves to the run it reused (which holds the items).
    """
    _require_agent(agent_id)
    source = latest_output_run(db, agent_id)
    if source is None:
        return None, []
    # latest_output_run already followed reused_run_id; find the run that was
    # actually requested (the newest succeeded one) for honest cache metadata.
    requested = (
        db.query(AgentRun)
        .filter(AgentRun.agent_id == agent_id, AgentRun.status == "succeeded")
        .order_by(AgentRun.id.desc())
        .first()
    )
    items = sorted(
        (i for i in source.items if i.verification_status == "verified"),
        key=lambda i: (i.position, i.id),
    )
    return _run_meta(requested, source), [
        {"position": i.position, "archetype": i.archetype, "payload": i.payload}
        for i in items
    ]


def _agent_output(agent_id: str) -> dict[str, Any]:
    """The one selection behind both `get_agent_output` and the resource."""
    with _session() as db:
        run, items = _latest_verified(db, agent_id)
    return {
        "agent_id": agent_id,
        "has_output": run is not None,
        "run": run,
        "items": items,
    }


def _citations(db: Session, refs: list[dict]) -> list[dict[str, Any]]:
    """An item's source_refs plus each cited item's title (no content)."""
    out = []
    for ref in refs or []:
        cited = dict(ref)
        item_id = ref.get("resource_item_id")
        item = db.get(ResourceItem, item_id) if isinstance(item_id, int) else None
        cited["source_title"] = item.title if item is not None else None
        out.append(cited)
    return out


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


# ── Tools ────────────────────────────────────────────────────────────────────


@mcp.tool(
    annotations=_READ_ONLY,
    description=(
        "List upcoming course deadlines from the Deadline agent's latest "
        "verified output. Each deadline has its date (YYYY-MM-DD), title, "
        "description and citations (the source excerpts it was verified "
        "against). Upcoming means dated today or later, by the server's "
        "local date; today's deadlines are included. Past deadlines, items "
        "that failed verification, and items without a readable date are "
        "left out (their counts are reported). Read-only: it never runs the "
        "Deadline agent, so if there is no successful output yet the list is "
        "empty and has_output is false."
    ),
)
def list_deadlines() -> dict[str, Any]:
    today = _today()
    with _session() as db:
        run, items = _latest_verified(db, DEADLINE_AGENT_ID)
        upcoming, past, undated = [], 0, 0
        for item in items:
            payload = item["payload"]
            when = _parse_date(payload.get("date"))
            if when is None:
                undated += 1
                continue
            if when < today:
                past += 1
                continue
            upcoming.append(
                (
                    when,
                    item["position"],
                    {
                        "date": when.isoformat(),
                        "title": payload.get("title", ""),
                        "description": payload.get("description", ""),
                        "citations": _citations(db, payload.get("source_refs", [])),
                    },
                )
            )
    upcoming.sort(key=lambda row: (row[0], row[1]))
    return {
        "today": today.isoformat(),
        "includes_today": True,
        "has_output": run is not None,
        "run": run,
        "deadlines": [row[2] for row in upcoming],
        "excluded_past": past,
        "excluded_undated": undated,
    }


@mcp.tool(
    annotations=_READ_ONLY,
    description=(
        "Get the latest verified output of any registered Course Copilot "
        "agent (for example deadline_agent or quiz_agent), in display order. "
        "Each item has its archetype and payload, including source_refs "
        "citations. Only items that passed verification are returned; a "
        "cache-hit run is resolved to the run whose output it reused, and a "
        "newer queued or failed run does not hide the last successful "
        "output. Read-only: it never starts a run, so an agent with no "
        "successful output returns has_output false and no items. An "
        "unknown agent_id is an error that lists the registered agents."
    ),
)
def get_agent_output(agent_id: str) -> dict[str, Any]:
    try:
        return _agent_output(agent_id)
    except UnknownAgent as exc:
        raise ToolError(str(exc)) from None


@mcp.tool(
    annotations=_STARTS_RUN,
    description=(
        "Start a quiz run from free text, exactly as the Quiz page does "
        "(e.g. 'Quiz me on unit 2 with 5 questions'). This is the only tool "
        "that changes anything: it records one quiz run. If the same request "
        "on unchanged material was already quizzed on, the run is served "
        "from cache (status succeeded, from_cache true); otherwise it is "
        "queued for the background worker (status queued) and questions are "
        "generated and verified later. Returns the run id and status, not "
        "the questions; once the run has succeeded, "
        "get_agent_output('quiz_agent') returns the latest quiz's verified "
        "questions. Empty text or text over 1000 characters is an error."
    ),
)
def request_quiz(text: str) -> dict[str, Any]:
    with _session() as db:
        try:
            run = quiz_service.start_quiz(db, text)
        except quiz_service.QuizServiceError as exc:
            raise ToolError(str(exc)) from None
        except Exception:
            logger.exception("request_quiz failed")
            raise ToolError(
                "Could not start the quiz run (the job queue may be unavailable)."
            ) from None
        return {
            "run_id": run.id,
            "agent_id": run.agent_id,
            "status": run.status,
            "from_cache": run.from_cache,
            "reused_run_id": run.reused_run_id,
            "request": (run.request_payload or {}).get("text", ""),
        }


# ── Resource ─────────────────────────────────────────────────────────────────


@mcp.resource(
    "copilot://agents/{agent_id}/output",
    name="agent_output",
    title="Agent output",
    description=(
        "The latest verified output of a registered agent as JSON — the same "
        "selection get_agent_output returns. Read-only; never starts a run."
    ),
    mime_type="application/json",
)
def agent_output_resource(agent_id: str) -> dict[str, Any]:
    try:
        return _agent_output(agent_id)
    except UnknownAgent as exc:
        raise ResourceNotFoundError(str(exc)) from None


def main() -> None:
    # stdout belongs to the protocol; every diagnostic goes to stderr.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
