"""Concept Coverage Tracker (shipped, spec §8.2).

Builds the course's topic list from syllabus/project-spec content, then
checks which topics have already appeared in slides/readings/notes,
producing one status item per topic (covered/upcoming, citing where the
topic was first seen). Deliberately a leaf node (no upstream dependencies).
"""

from pydantic import BaseModel

from app.core.manifest import AgentContext, AgentManifest

MANIFEST = AgentManifest(
    agent_id="concept_coverage_agent",
    display_name="Concept Coverage",
    description=(
        "Tracks which syllabus topics have been covered so far in lecture "
        "slides, readings, and the student's own notes, and which are still "
        "upcoming."
    ),
    required_resource_tags=[
        "syllabus",
        "project-spec",
        "slides",
        "reading",
        "personal-notes",
    ],
    optional_upstream_agents=[],
    output_archetype="status_item",
    trigger="on_resource_change",
    cacheable=True,
    dashboard_widget=True,
)


class _SourceRef(BaseModel):
    resource_item_id: int
    excerpt: str


class _Topic(BaseModel):
    name: str
    source_ref: _SourceRef  # where the syllabus/spec names this topic


class _Topics(BaseModel):
    topics: list[_Topic]


_TOPICS_PROMPT = """From the course syllabus / project spec content below,
list the main topics/concepts the course covers (aim for 8–20, one short
name each). For each topic cite the resource item id it came from with a
SHORT VERBATIM excerpt (≤ 150 characters) naming it.

MATERIALS:
{materials}
"""


class _Coverage(BaseModel):
    topic: str
    covered: bool
    detail: str
    source_ref: _SourceRef | None  # where the topic was first seen, if covered


class _Coverages(BaseModel):
    coverages: list[_Coverage]


_COVERAGE_PROMPT = """For each course topic below, decide whether it has
ALREADY been covered, based only on the course materials seen so far
(lecture slides, assigned readings, the student's notes). A topic is
covered if the materials substantively discuss it — a passing mention in a
schedule listing does not count.

For covered topics: cite the resource item id where it is best evidenced,
with a SHORT VERBATIM excerpt (≤ 150 characters), and say in one phrase
where it was first seen. For upcoming topics: source_ref null, detail
"not yet seen in course materials".

TOPICS:
{topics}

MATERIALS SEEN SO FAR:
{materials}
"""


def _materials_block(items, limit_chars=4_000) -> str:
    return "\n\n".join(
        f"=== resource item {i.id}: {i.title} ===\n{i.content[:limit_chars]}"
        for i in items
    )


def run(ctx: AgentContext) -> list[dict]:
    syllabus_items = ctx.resources.get("syllabus", []) + ctx.resources.get(
        "project-spec", []
    )
    if not syllabus_items:
        return []

    topics_result = ctx.llm.generate(
        _TOPICS_PROMPT.format(materials=_materials_block(syllabus_items)),
        response_schema=_Topics,
    )
    topics: list[_Topic] = topics_result.parsed.topics
    if not topics:
        return []

    evidence_items = (
        ctx.resources.get("slides", [])
        + ctx.resources.get("reading", [])
        + ctx.resources.get("personal-notes", [])
    )

    coverage_by_topic: dict[str, _Coverage] = {}
    if evidence_items:
        coverage_result = ctx.llm.generate(
            _COVERAGE_PROMPT.format(
                topics="\n".join(f"- {t.name}" for t in topics),
                materials=_materials_block(evidence_items),
            ),
            response_schema=_Coverages,
        )
        coverage_by_topic = {c.topic: c for c in coverage_result.parsed.coverages}

    output: list[dict] = []
    for topic in topics:
        coverage = coverage_by_topic.get(topic.name)
        refs = [topic.source_ref.model_dump()]
        if coverage is not None and coverage.covered:
            state = "covered"
            detail = coverage.detail
            if coverage.source_ref is not None:
                refs.append(coverage.source_ref.model_dump())
        else:
            state = "upcoming"
            detail = "not yet seen in course materials"
        output.append(
            {
                "label": topic.name,
                "detail": detail,
                "state": state,
                "source_refs": refs,
            }
        )
    return output
