"""Quiz Agent (T6): an on-demand quiz from one free-text request.

Request payload (the same for the Quiz page and the MCP `request_quiz` tool):

    {"text": "Quiz me on all content in unit 1 with 20 multiple choice questions"}

The agent does all the interpretation itself, in three steps:

1. **Parse** the free text into {scope, count, format} with one structured
   `generate()` call. Formats this agent can honestly produce as `qa_item`s
   are multiple choice and true/false; anything else (short answer, essay,
   ...) is refused with a clear message rather than silently swapped.
2. **Resolve the scope** to concrete `personal-notes` / `slides` /
   `reading` items. The model sees every candidate item's title, path, tags
   and CURRENT user-editable metadata (lecture number, date, unit, ...),
   plus the Deadline agent's timeline, Concept Coverage's topics and the
   schedule, and picks the in-scope item ids. Nothing about units, dates or
   topics is hard-coded here. A scope that matches nothing, or that the
   model reports as ambiguous, ends the run with an explanation instead of
   quizzing on unrelated material.
3. **Generate** questions from the selected material only. Each question
   cites one selected item with a verbatim excerpt; questions that fail the
   deterministic checks (excerpt not verbatim, cites unselected material,
   malformed choices, duplicate) are discarded and the shortfall is
   regenerated once (bounded). The harness's generic verifier then judges
   every question; whatever it drops is disclosed, never padded.

Every question carries auxiliary fields the `qa_item` archetype allows:
`topic` (what it tests), `scope` (the resolved scope label) and
`quiz_request` (the parsed request and how the scope resolved), so the
attempt service can record requested vs. verified counts without parsing
anything itself.

Resource text, metadata and upstream output reach prompts only through
`wrap_untrusted`, with `UNTRUSTED_NOTICE`. `run()` never touches the
database: attempts are persisted by `app/quiz_service.py`.
"""

import json
import re
from typing import Literal

from pydantic import BaseModel

from app.core.manifest import AgentContext, AgentManifest, ResourceItemView
from app.core.untrusted import UNTRUSTED_NOTICE, wrap_untrusted

MANIFEST = AgentManifest(
    agent_id="quiz_agent",
    display_name="Quiz",
    description=(
        "Builds a multiple-choice or true/false quiz from one free-text "
        "request (scope, question count and format), drawing only on the "
        "student's notes, slides and readings that fall in the requested "
        "scope, with every question citing its source."
    ),
    # Material the questions come from, plus the schedule as scope context.
    required_resource_tags=["personal-notes", "slides", "reading", "schedule"],
    optional_upstream_agents=["deadline_agent", "concept_coverage_agent"],
    output_archetype="qa_item",
    trigger="on_demand",
    cacheable=True,
    dashboard_widget=False,
    tools=[],
)

MATERIAL_TAGS = ("personal-notes", "slides", "reading")
DEFAULT_COUNT = 5
MAX_COUNT = 25
MAX_EXCERPT_CHARS = 300
# Prompt budgets (characters). Excerpts are checked against full content.
_ITEM_CHARS = 6_000
_MATERIAL_CHARS = 48_000
_PREVIEW_CHARS = 240
_SCHEDULE_CHARS = 4_000
# Generation rounds: the first request plus one bounded regeneration.
_MAX_ROUNDS = 2

QuizFormat = Literal["multiple_choice", "true_false"]
_FORMAT_LABELS = {"multiple_choice": "multiple choice", "true_false": "true/false"}
_TRUE_FALSE = ["True", "False"]


class QuizRequestError(ValueError):
    """The request can't be honoured as asked. The message is shown to the
    student as the run's error, so it says what to change."""


# ── Step 1: parse ────────────────────────────────────────────────────────────


class _ParsedRequest(BaseModel):
    # The scope phrase as the student said it ("unit 1", "lecture 5",
    # "gradient descent"); "" when they named none.
    scope: str
    # True when the student asked for everything / named no scope.
    whole_course: bool
    # Number of questions asked for; null/0 when unspecified.
    count: int | None = None
    # unspecified: no format named. other: a format named that isn't MC/TF.
    format: Literal["unspecified", "multiple_choice", "true_false", "other"]
    # The format words as written ("MC", "short answer"), "" if none.
    format_text: str = ""


_PARSE_PROMPT = """A student typed the quiz request below. Extract:
- scope: the part of the course they want to be quizzed on, as they said it
  (e.g. "unit 1", "lecture 5", "the RAG readings"). "" if they named none.
- whole_course: true only if they asked for everything or named no scope.
- count: how many questions they asked for, or null if they did not say.
- format: "multiple_choice", "true_false", "unspecified" if they named no
  format, or "other" for any other format (short answer, essay, fill in
  the blank, ...).
- format_text: the format words exactly as they wrote them, or "".

Do not answer the request; only extract these fields.

REQUEST: {request}
"""


def _parse(ctx: AgentContext, text: str) -> _ParsedRequest:
    return ctx.llm.generate(
        _PARSE_PROMPT.format(request=json.dumps(text)), response_schema=_ParsedRequest
    ).parsed


def _resolve_count(parsed: _ParsedRequest, notes: list[str]) -> int:
    if not parsed.count or parsed.count < 1:
        return DEFAULT_COUNT
    if parsed.count > MAX_COUNT:
        notes.append(
            f"{parsed.count} questions were requested; quizzes are capped at "
            f"{MAX_COUNT}, so {MAX_COUNT} were requested from the generator."
        )
        return MAX_COUNT
    return parsed.count


def _resolve_format(parsed: _ParsedRequest) -> QuizFormat:
    if parsed.format == "other":
        asked = parsed.format_text.strip() or "that format"
        raise QuizRequestError(
            f"This quiz can't produce {asked!s} questions: only multiple-choice "
            "and true/false questions can be scored and verified. Ask again "
            "for multiple-choice or true/false questions."
        )
    if parsed.format == "true_false":
        return "true_false"
    return "multiple_choice"


# ── Step 2: resolve scope ────────────────────────────────────────────────────


class _ScopeSelection(BaseModel):
    status: Literal["matched", "ambiguous", "no_match"]
    in_scope_item_ids: list[int]
    # Human-readable name of what was selected ("Unit 1: lectures 1-4").
    scope_label: str
    # One or two sentences: which evidence (metadata, schedule, upstream)
    # mapped the scope to these items, or why it could not.
    explanation: str


_SCOPE_PROMPT = """{notice}

A student asked to be quizzed on: {scope}

Decide which of the CANDIDATE MATERIALS below fall within that scope. Use
the evidence available: each candidate's metadata (lecture numbers, dates,
units, ...), title, path and opening text; the course schedule; the
deadline timeline; and the course topic list. The metadata shown is the
current, user-edited state and takes precedence over anything else.

Rules:
- Select only candidates that genuinely belong to the scope. If nothing
  does, return status "no_match" with no ids.
- If the scope could mean clearly different sets of material and the
  evidence cannot decide, return status "ambiguous" and explain.
- Otherwise return status "matched" with the ids, a short scope_label, and
  an explanation naming the evidence you used.

CANDIDATE MATERIALS:
{candidates}

COURSE SCHEDULE:
{schedule}

DEADLINE TIMELINE (from the Deadline agent):
{deadlines}

COURSE TOPICS (from the Concept Coverage agent):
{topics}
"""


def _candidate_block(item: ResourceItemView) -> str:
    preview = " ".join(item.content[:_PREVIEW_CHARS].split())
    body = (
        f"id: {item.id}\n"
        f"title: {item.title}\n"
        f"path: {item.path or item.locator}\n"
        f"tags: {', '.join(item.tags)}\n"
        f"metadata: {json.dumps(item.meta or {}, sort_keys=True, default=str)}\n"
        f"opening text: {preview}"
    )
    return wrap_untrusted(body, source=f"resource item {item.id}")


def _upstream_block(ctx: AgentContext, agent_id: str, fields: tuple[str, ...]) -> str:
    rows = ctx.upstream.get(agent_id, [])
    if not rows:
        return "(none available)"
    lines = [" | ".join(str(row.get(f, "")) for f in fields) for row in rows]
    return wrap_untrusted("\n".join(lines), source=agent_id)


def _schedule_block(ctx: AgentContext) -> str:
    items = ctx.resources.get("schedule", [])
    if not items:
        return "(none available)"
    return "\n".join(
        wrap_untrusted(
            i.content[:_SCHEDULE_CHARS], source=i.path or i.title or i.locator
        )
        for i in items
    )


def _resolve_scope(
    ctx: AgentContext, parsed: _ParsedRequest, candidates: list[ResourceItemView]
) -> tuple[list[ResourceItemView], str, str]:
    """(selected items, scope label, explanation). Raises QuizRequestError."""
    scope = parsed.scope.strip()
    if parsed.whole_course or not scope:
        return (
            candidates,
            "All course material",
            "No scope named: every note, slide and reading.",
        )

    selection = ctx.llm.generate(
        _SCOPE_PROMPT.format(
            notice=UNTRUSTED_NOTICE,
            scope=json.dumps(scope),
            candidates="\n".join(_candidate_block(i) for i in candidates),
            schedule=_schedule_block(ctx),
            deadlines=_upstream_block(
                ctx, "deadline_agent", ("date", "title", "description")
            ),
            topics=_upstream_block(
                ctx, "concept_coverage_agent", ("label", "state", "detail")
            ),
        ),
        response_schema=_ScopeSelection,
    ).parsed

    by_id = {i.id: i for i in candidates}
    # Ids the model invented, or that aren't candidates, are ignored.
    selected = [
        by_id[i] for i in dict.fromkeys(selection.in_scope_item_ids) if i in by_id
    ]
    if selection.status == "ambiguous":
        raise QuizRequestError(
            f"The scope {scope!r} is ambiguous: {selection.explanation.strip()} "
            "Name it more precisely (for example a lecture number or topic)."
        )
    if selection.status == "no_match" or not selected:
        detail = selection.explanation.strip()
        raise QuizRequestError(
            f"No notes, slides or readings match the scope {scope!r}."
            + (f" {detail}" if detail else "")
            + " Add or tag material for it on the Resources page, or check its "
            "metadata, then ask again."
        )
    label = selection.scope_label.strip() or scope
    return selected, label, selection.explanation.strip()


# ── Step 3: generate ─────────────────────────────────────────────────────────


class _Question(BaseModel):
    question: str
    choices: list[str]
    correct_index: int
    topic: str
    resource_item_id: int
    excerpt: str


class _Questions(BaseModel):
    questions: list[_Question]


_GENERATE_PROMPT = """{notice}

Write exactly {count} {format_label} quiz questions on the scope named in
the SCOPE block below, using ONLY the study material below. Each question
must be answerable from one short passage of that material.

For each question:
- question: the question text.
- choices: {choice_rule}
- correct_index: 0-based index of the one correct choice.
- topic: a short name for the concept it tests{topic_rule}.
- resource_item_id: the id of the material item the answer comes from.
- excerpt: the sentence(s) that establish the correct answer, copied
  VERBATIM from that item (at most {max_excerpt} characters).
{avoid_rule}
SCOPE:
{scope}
{sections}
STUDY MATERIAL:
{material}
"""

_CHOICE_RULES = {
    "multiple_choice": "four distinct answer options, exactly one correct.",
    "true_false": 'exactly ["True", "False"].',
}


def _material_block(items: list[ResourceItemView]) -> str:
    blocks: list[str] = []
    budget = _MATERIAL_CHARS
    for item in items:
        if budget <= 0:
            break
        text = item.content[: min(_ITEM_CHARS, budget)]
        budget -= len(text)
        blocks.append(
            f"--- material item id {item.id} ---\n"
            + wrap_untrusted(text, source=item.path or item.title or item.locator)
        )
    return "\n".join(blocks)


def _normalize(text: str) -> str:
    # Same normalization the verifier uses for its verbatim check.
    return re.sub(r"\s+", " ", text).lower().strip()


def _accept(
    q: _Question, fmt: QuizFormat, material: dict[int, ResourceItemView], seen: set[str]
) -> dict | None:
    """The question as a qa_item draft, or None if it fails a deterministic check."""
    item = material.get(q.resource_item_id)
    excerpt = q.excerpt.strip()[:MAX_EXCERPT_CHARS]
    key = _normalize(q.question)
    if item is None or not key or key in seen:
        return None
    if not _normalize(excerpt) or _normalize(excerpt) not in _normalize(item.content):
        return None
    choices = [c.strip() for c in q.choices]
    if fmt == "true_false":
        if [c.lower() for c in choices] != ["true", "false"]:
            return None
        choices = list(_TRUE_FALSE)
    elif len(choices) < 3 or len({_normalize(c) for c in choices}) != len(choices):
        return None
    if not all(choices) or not 0 <= q.correct_index < len(choices):
        return None
    seen.add(key)
    return {
        "question": q.question.strip(),
        "choices": choices,
        "correct_index": q.correct_index,
        "topic": q.topic.strip(),
        "source_refs": [{"resource_item_id": item.id, "excerpt": excerpt}],
    }


def _generate(
    ctx: AgentContext,
    selected: list[ResourceItemView],
    count: int,
    fmt: QuizFormat,
    scope_label: str,
) -> list[dict]:
    material = {i.id: i for i in selected}
    material_text = _material_block(selected)
    topics = [
        str(row.get("label", ""))
        for row in ctx.upstream.get("concept_coverage_agent", [])
        if row.get("label")
    ]
    # Everything below derives from resources or upstream output (the scope
    # label and earlier drafts are model output over them), so each piece
    # goes in its own wrapped data block, never into the instructions.
    scope_block = wrap_untrusted(scope_label, source="quiz scope resolution")
    topic_rule = ""
    topics_section = ""
    if topics:
        topic_rule = " (prefer a name from COURSE TOPICS below when one fits)"
        topics_section = "\nCOURSE TOPICS (from the Concept Coverage agent):\n" + (
            wrap_untrusted("\n".join(topics[:30]), source="concept_coverage_agent")
        )
    accepted: list[dict] = []
    seen: set[str] = set()
    for _ in range(_MAX_ROUNDS):
        missing = count - len(accepted)
        if missing <= 0:
            break
        avoid_rule = ""
        avoid_section = ""
        if accepted:
            avoid_rule = "- Do not repeat any question listed under ALREADY WRITTEN.\n"
            avoid_section = "\nALREADY WRITTEN:\n" + wrap_untrusted(
                "\n".join(q["question"] for q in accepted),
                source="earlier quiz drafts",
            )
        result = ctx.llm.generate(
            _GENERATE_PROMPT.format(
                notice=UNTRUSTED_NOTICE,
                count=missing,
                format_label=_FORMAT_LABELS[fmt],
                choice_rule=_CHOICE_RULES[fmt],
                topic_rule=topic_rule,
                max_excerpt=MAX_EXCERPT_CHARS,
                avoid_rule=avoid_rule,
                scope=scope_block,
                sections=topics_section + avoid_section,
                material=material_text,
            ),
            response_schema=_Questions,
        ).parsed
        for q in result.questions:
            if len(accepted) >= count:
                break
            draft = _accept(q, fmt, material, seen)
            if draft is not None:
                accepted.append(draft)
    return accepted


# ── Entry point ──────────────────────────────────────────────────────────────


def request_text(request: dict | None) -> str:
    """The raw free text of a quiz request payload ("" if absent)."""
    text = (request or {}).get("text")
    return text.strip() if isinstance(text, str) else ""


def run(ctx: AgentContext) -> list[dict]:
    text = request_text(ctx.request)
    if not text:
        raise QuizRequestError(
            'A quiz request needs free text, e.g. "Quiz me on lecture 3".'
        )

    parsed = _parse(ctx, text)
    notes: list[str] = []
    fmt = _resolve_format(parsed)
    count = _resolve_count(parsed, notes)

    seen_ids: set[int] = set()
    candidates: list[ResourceItemView] = []
    for tag in MATERIAL_TAGS:
        for item in ctx.resources.get(tag, []):
            if item.id not in seen_ids and item.content.strip():
                seen_ids.add(item.id)
                candidates.append(item)
    if not candidates:
        raise QuizRequestError(
            "There is no study material to quiz on yet: tag your notes, slides "
            "or readings as personal-notes, slides or reading on the Resources page."
        )

    selected, scope_label, explanation = _resolve_scope(ctx, parsed, candidates)
    questions = _generate(ctx, selected, count, fmt, scope_label)
    if not questions:
        raise QuizRequestError(
            f"No question about {scope_label!r} could be written with a verbatim "
            "citation from the selected material, so no quiz was produced."
        )
    if len(questions) < count:
        notes.append(
            f"Only {len(questions)} of {count} requested questions could be drawn "
            "from the selected material with a verbatim citation."
        )

    quiz_request = {
        "text": text,
        "scope": parsed.scope.strip(),
        "count": count,
        "requested_count": parsed.count,
        "format": fmt,
        "scope_label": scope_label,
        "resolution": explanation,
        "material_item_ids": [i.id for i in selected],
        "notes": notes,
    }
    for q in questions:
        q["topic"] = q["topic"] or scope_label
        q["scope"] = scope_label
        q["quiz_request"] = quiz_request
    return questions
