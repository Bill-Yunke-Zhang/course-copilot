"""Output archetypes: the fixed set of shapes agent output items may take.

The frontend renders these generically (frontend/src/archetypes/registry.tsx
is the mirror of this file), and the grounding verifier checks them
generically — which is exactly what lets the harness handle an agent it has
never seen.

Extension point: a team's student-designed agent may register ONE new
archetype here via `register_archetype`, provided (a) every item still
carries at least one source_ref, (b) the frontend registry gains a matching
renderer, and (c) the five shipped archetypes keep rendering and verifying
exactly as before — staff grading agents depend on them.
"""

from pydantic import BaseModel, ConfigDict, model_validator


class SourceRef(BaseModel):
    """A citation grounding one output item in one resource item.

    `excerpt` carries the supporting text for prose-derived claims. For
    `project-code`-derived items, `evidence` instead carries structured
    proof (commit SHA, test-run result, …). At least one must be present.
    """

    model_config = ConfigDict(extra="forbid")

    resource_item_id: int
    excerpt: str | None = None
    evidence: dict | None = None

    @model_validator(mode="after")
    def _excerpt_or_evidence(self) -> "SourceRef":
        if not self.excerpt and not self.evidence:
            raise ValueError("source_ref needs an excerpt or structured evidence")
        return self


class _ArchetypeBase(BaseModel):
    # extra="allow" so agents can carry auxiliary fields (e.g. a status
    # badge) without a schema change; the fields below are the contract.
    model_config = ConfigDict(extra="allow")

    source_refs: list[SourceRef]

    @model_validator(mode="after")
    def _at_least_one_ref(self) -> "_ArchetypeBase":
        if len(self.source_refs) == 0:
            raise ValueError("every output item must carry at least one source_ref")
        return self


class TimelineItem(_ArchetypeBase):
    date: str  # ISO date (YYYY-MM-DD)
    title: str
    description: str


class StatusItem(_ArchetypeBase):
    label: str
    detail: str


class QaItem(_ArchetypeBase):
    question: str
    choices: list[str]
    correct_index: int

    @model_validator(mode="after")
    def _index_in_range(self) -> "QaItem":
        if not (0 <= self.correct_index < len(self.choices)):
            raise ValueError("correct_index out of range")
        return self


class CardItem(_ArchetypeBase):
    front: str
    back: str


class TextBlock(_ArchetypeBase):
    heading: str
    body: str


_ARCHETYPES: dict[str, type[_ArchetypeBase]] = {
    "timeline_item": TimelineItem,
    "status_item": StatusItem,
    "qa_item": QaItem,
    "card_item": CardItem,
    "text_block": TextBlock,
}


def register_archetype(name: str, schema: type[_ArchetypeBase]) -> None:
    """Register a new (team-defined) archetype. See module docstring."""
    if name in _ARCHETYPES:
        raise ValueError(f"archetype {name!r} already registered")
    if not issubclass(schema, _ArchetypeBase):
        raise TypeError("archetype schemas must subclass _ArchetypeBase")
    _ARCHETYPES[name] = schema


def known_archetypes() -> list[str]:
    return list(_ARCHETYPES)


def validate_payload(archetype: str, payload: dict) -> dict:
    """Validate an output item payload against its archetype; returns the
    normalized payload dict. Raises ValueError on any violation."""
    schema = _ARCHETYPES.get(archetype)
    if schema is None:
        raise ValueError(f"unknown archetype: {archetype!r}")
    try:
        return schema.model_validate(payload).model_dump(exclude_none=True)
    except Exception as exc:
        raise ValueError(f"invalid {archetype} payload: {exc}") from exc
