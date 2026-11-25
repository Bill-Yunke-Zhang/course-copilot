import pytest

from app.core.archetypes import SourceRef, validate_payload


REF = {"resource_item_id": 1, "excerpt": "some supporting text"}


def test_valid_timeline_item():
    payload = validate_payload(
        "timeline_item",
        {
            "date": "2026-11-17",
            "title": "P4 due",
            "description": "x",
            "source_refs": [REF],
        },
    )
    assert payload["date"] == "2026-11-17"


def test_every_item_requires_a_source_ref():
    with pytest.raises(ValueError, match="source_ref"):
        validate_payload(
            "timeline_item",
            {
                "date": "2026-11-17",
                "title": "P4 due",
                "description": "x",
                "source_refs": [],
            },
        )


def test_source_ref_needs_excerpt_or_evidence():
    with pytest.raises(ValueError):
        SourceRef(resource_item_id=1)
    # structured evidence alone is fine (project-code items)
    ref = SourceRef(resource_item_id=1, evidence={"head_sha": "abc123"})
    assert ref.evidence["head_sha"] == "abc123"


def test_unknown_archetype_rejected():
    with pytest.raises(ValueError, match="unknown archetype"):
        validate_payload("mystery_shape", {"source_refs": [REF]})


def test_qa_item_correct_index_range():
    with pytest.raises(ValueError):
        validate_payload(
            "qa_item",
            {
                "question": "?",
                "choices": ["a", "b"],
                "correct_index": 5,
                "source_refs": [REF],
            },
        )


def test_extra_fields_allowed():
    payload = validate_payload(
        "status_item",
        {
            "label": "Topic",
            "detail": "covered",
            "state": "covered",
            "source_refs": [REF],
        },
    )
    assert payload["state"] == "covered"
