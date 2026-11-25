"""Resource classification (spec §4.3).

One LLM call per resource item assigns tags from the fixed vocabulary plus
optional lecture/date metadata. The output is a SUGGESTION — every tag is
shown in the Resource List UI and is user-editable, and resolution treats
classifier tags and user corrections identically.
"""

from enum import Enum

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import TAG_VOCABULARY
from app.core.llm import LlmClient

# Built dynamically so the classifier's tag space can never drift from the
# fixed vocabulary in app/config.py.
TagEnum = Enum("TagEnum", {t.replace("-", "_"): t for t in TAG_VOCABULARY})


class Classification(BaseModel):
    tags: list[TagEnum]
    # Lecture/date metadata, when inferable (used by e.g. the Quiz agent's
    # scope filtering). Same user-editable principle as tags.
    lecture_number: int | None = None
    date: str | None = None  # ISO date the content is associated with
    reasoning: str


_PROMPT = """You classify one resource for a single-course study assistant.
Assign one or more tags from this fixed vocabulary (and nothing else):

- schedule: the course schedule page itself
- syllabus: the course syllabus page itself
- project-spec: project overview/requirements content
- reading: an assigned pre-class reading
- video: an assigned pre-class video
- reference: optional/supplemental curated resource
- slides: a lecture slide deck
- activity: in-class activity instructions/code
- personal-notes: the student's own authored notes
- project-instructions: instructions/rubric-style docs inside a team project repo
- project-code: actual project codebase files (code, configs, tests)

Also extract a lecture number and/or an ISO date the content belongs to, if
clearly inferable (e.g. from a filename like "lecture-07-notes.md" or a
heading); otherwise leave them null.

RESOURCE KIND: {kind}
LOCATION: {locator}
SUB-PATH: {path}
CONTEXT: {context}

CONTENT (may be truncated):
{content}
"""

# Content cap for the classification prompt — classification doesn't need
# the whole document.
CLASSIFY_CONTENT_CHARS = 8_000


def classify_item(
    db: Session,
    llm: LlmClient,
    *,
    kind: str,
    locator: str,
    path: str,
    content: str,
    context: str = "",
    resource_id: int | None = None,
) -> Classification:
    prompt = _PROMPT.format(
        kind=kind,
        locator=locator,
        path=path or "(none)",
        context=context or "(none)",
        content=content[:CLASSIFY_CONTENT_CHARS],
    )
    result = llm.generate(
        db,
        purpose="classification",
        prompt=prompt,
        response_schema=Classification,
        resource_id=resource_id,
    )
    return result.parsed
