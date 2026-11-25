"""Spec Checklist Agent (T8, our team's own agent — see docs/custom-agent.md).

Turns the project spec and instructions into a tick-off checklist: one
item per hard requirement (must / must not / graded criterion), grouped by
task, each quoting the sentence it came from, and tagged with its due date
when the Deadline agent's own citation names the same task.

Per resource item, one structured `generate()` call (resource text goes in
through `wrap_untrusted`). Everything after that is deterministic:

- drop a requirement whose excerpt is not verbatim in its source item;
- drop exact duplicates (same normalized excerpt);
- keep a due-date pick only if the picked Deadline item's title or
  description names the same task id, and then copy that Deadline item's
  source_refs in, so the date is borrowed together with its citation.
"""

import re
from typing import Literal

from pydantic import BaseModel

from app.core.manifest import AgentContext, AgentManifest, ResourceItemView
from app.core.untrusted import UNTRUSTED_NOTICE, wrap_untrusted

MANIFEST = AgentManifest(
    agent_id="spec_checklist_agent",
    display_name="Spec Checklist",
    description=(
        "Extracts every hard requirement (must / must not / graded "
        "criterion) from the project spec and instructions into a "
        "tick-off checklist grouped by task, each quoting its source "
        "sentence and tagged with its due date when known."
    ),
    required_resource_tags=["project-spec", "project-instructions"],
    optional_upstream_agents=["deadline_agent"],
    output_archetype="status_item",
    trigger="on_resource_change",
    cacheable=True,
    dashboard_widget=False,
    tools=[],
)

MAX_EXCERPT_CHARS = 200
# Task ids we can match against a Deadline item ("T3", "R0", "P4" ...).
_TASK_ID = re.compile(r"^[A-Za-z]{1,3}\d{1,2}$")


class _Requirement(BaseModel):
    task: str
    state: Literal["must", "must_not", "graded"]
    label: str
    detail: str
    excerpt: str


class _DuePick(BaseModel):
    task: str
    deadline: int | None = None  # 1-based index into the numbered deadlines


class _Checklist(BaseModel):
    requirements: list[_Requirement]
    due: list[_DuePick] = []


_PROMPT = """{notice}

Extract every hard requirement a student's submission must satisfy from the
course document below: each "must", each "must not", and each graded
criterion. Skip advice, examples and background.

For each requirement:
- task: the task or rubric id it belongs to exactly as the document names it
  ("T3", "R0"), or "General" if it belongs to none.
- state: "must", "must_not" or "graded".
- label: a short imperative ("List every task T3-T9 in the timeline").
- detail: one sentence restating what must hold.
- excerpt: the requirement sentence copied VERBATIM from the document
  (at most {max_excerpt} characters; copy a contiguous part if longer).

{deadline_section}
DOCUMENT:
{document}
"""

_DEADLINE_SECTION = """Also, for each task id you used, pick the number of the
deadline below that is the due date of THAT task, or null if none is. Return
these as `due`: [{{"task": ..., "deadline": number or null}}].

DEADLINES:
{deadlines}
"""


# The only documents we read: the project spec and its instructions.
INPUT_TAGS = ("project-spec", "project-instructions")


def _spec_items(ctx: AgentContext) -> list[ResourceItemView]:
    """Items tagged project-spec or project-instructions, deduplicated (one
    file can carry both tags), in stable id order."""
    seen: dict[int, ResourceItemView] = {}
    for tag in INPUT_TAGS:
        for item in ctx.resources.get(tag, []):
            seen[item.id] = item
    return [seen[k] for k in sorted(seen)]


def run(ctx: AgentContext) -> list[dict]:
    items = _spec_items(ctx)
    if not items:
        return []
    deadlines = ctx.upstream.get("deadline_agent", [])

    output: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if not item.content.strip():
            continue
        checklist = ctx.llm.generate(
            _prompt(item, deadlines), response_schema=_Checklist
        ).parsed
        due = _due_dates(checklist.due, deadlines)
        for req in checklist.requirements:
            excerpt = req.excerpt.strip()[:MAX_EXCERPT_CHARS]
            key = _normalize(excerpt)
            if not key or key not in _normalize(item.content) or key in seen:
                continue  # not verbatim in its source, or a duplicate
            seen.add(key)
            task = req.task.strip() or "General"
            payload = {
                "label": req.label.strip(),
                "detail": req.detail.strip(),
                "state": req.state,
                "task": task,
                "source_refs": [{"resource_item_id": item.id, "excerpt": excerpt}],
            }
            picked = due.get(task.upper())
            if picked:
                payload["due_date"] = picked["date"]
                payload["source_refs"] += list(picked.get("source_refs", []))
            output.append(payload)
    return output


def _prompt(item: ResourceItemView, deadlines: list[dict]) -> str:
    section = ""
    if deadlines:
        numbered = "\n".join(
            f"{n}. {d.get('date')}: {d.get('title', '')} — {d.get('description', '')}"
            for n, d in enumerate(deadlines, start=1)
        )
        section = _DEADLINE_SECTION.format(
            deadlines=wrap_untrusted(numbered, source="deadline_agent")
        )
    return _PROMPT.format(
        notice=UNTRUSTED_NOTICE,
        max_excerpt=MAX_EXCERPT_CHARS,
        deadline_section=section,
        document=wrap_untrusted(item.content, source=item.path or item.title),
    )


def _due_dates(picks: list[_DuePick], deadlines: list[dict]) -> dict[str, dict]:
    """task id (upper case) -> the Deadline item it is due by, kept only when
    that item's own text names the task, so its citation backs the link."""
    due: dict[str, dict] = {}
    for pick in picks:
        task = pick.task.strip()
        if pick.deadline is None or not _TASK_ID.match(task):
            continue
        if not 1 <= pick.deadline <= len(deadlines):
            continue
        deadline = deadlines[pick.deadline - 1]
        text = f"{deadline.get('title', '')} {deadline.get('description', '')}"
        if deadline.get("date") and re.search(
            rf"\b{re.escape(task)}\b", text, re.IGNORECASE
        ):
            due[task.upper()] = deadline
    return due


def _normalize(text: str) -> str:
    # Same normalization the verifier uses for its verbatim check.
    return re.sub(r"\s+", " ", text).lower().strip()
