"""Deadline Agent (shipped, spec §8.1).

Parses schedule/project-spec content into dated timeline items — assignment
due dates, exam dates, release dates — each citing the exact source text it
was derived from. Deliberately a leaf node (no upstream dependencies).
"""

from pydantic import BaseModel

from app.core.manifest import AgentContext, AgentManifest

MANIFEST = AgentManifest(
    agent_id="deadline_agent",
    display_name="Deadlines",
    description=(
        "Extracts every dated event (assignment due dates, project releases, "
        "exams) from the course schedule and project specs into one timeline."
    ),
    required_resource_tags=["schedule", "project-spec"],
    optional_upstream_agents=[],
    output_archetype="timeline_item",
    trigger="on_resource_change",
    cacheable=True,
    dashboard_widget=True,
)


class _SourceRef(BaseModel):
    resource_item_id: int
    excerpt: str


class _Deadline(BaseModel):
    date: str  # ISO YYYY-MM-DD
    title: str
    description: str
    source_refs: list[_SourceRef]


class _Deadlines(BaseModel):
    deadlines: list[_Deadline]


_PROMPT = """Extract every dated event a student must know about from the
course material below: assignment releases and due dates, exam dates,
project milestones. Ignore events with no date.

For each event:
- date: ISO format (YYYY-MM-DD). Infer the year from context.
- title: short ("P2 due", "Midterm exam").
- description: one sentence of detail.
- source_refs: cite the resource item id the event came from, with a SHORT
  VERBATIM excerpt (copied exactly, ≤ 200 characters) of the text stating it.

MATERIALS:
{materials}
"""


def run(ctx: AgentContext) -> list[dict]:
    items = ctx.all_items()
    if not items:
        return []

    materials = "\n\n".join(
        f"=== resource item {i.id}: {i.title} ===\n{i.content}" for i in items
    )
    result = ctx.llm.generate(
        _PROMPT.format(materials=materials), response_schema=_Deadlines
    )
    return [d.model_dump() for d in result.parsed.deadlines]
