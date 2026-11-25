"""Resolution, triggering, caching/staleness, and the upstream stub."""

import pytest

from app.core import orchestrator
from app.core.manifest import AgentManifest
from app.models import AgentRun, ResourceItemTag
from tests.conftest import FakeLlm, make_resource


def _deadline_responses(item_id: int, excerpt: str) -> dict:
    return {
        "_Deadlines": {
            "deadlines": [
                {
                    "date": "2026-11-17",
                    "title": "P4 due",
                    "description": "Course Copilot project due at 11:59 PM",
                    "source_refs": [{"resource_item_id": item_id, "excerpt": excerpt}],
                }
            ]
        },
        "_JudgeResponse": {"supported": True, "reason": "excerpt states the date"},
    }


SCHEDULE_TEXT = "Week 12. P4 — Course Copilot due Tuesday, November 17 at 11:59 PM."


@pytest.fixture
def schedule_resource(db):
    return make_resource(
        db,
        locator="https://example.edu/schedule.html",
        items=[{"title": "Schedule", "content": SCHEDULE_TEXT, "tags": ["schedule"]}],
    )


def test_resolution_maps_each_tag_to_current_items(db, registry, schedule_resource):
    manifest = registry.get("deadline_agent").manifest
    resolved = orchestrator.resolve_resources(db, manifest)
    assert [i.title for i in resolved["schedule"]] == ["Schedule"]
    assert resolved["project-spec"] == []  # missing tag resolves empty, not error


def test_resolution_follows_user_tag_edits(db, registry, schedule_resource):
    manifest = registry.get("deadline_agent").manifest
    item = schedule_resource.items[0]
    # user removes the schedule tag → resolution must see it immediately
    for row in list(item.tag_rows):
        db.delete(row)
    db.commit()
    resolved = orchestrator.resolve_resources(db, manifest)
    assert resolved["schedule"] == []


def test_resource_change_event_matches_tags_deterministically(
    db, registry, schedule_resource, enqueued
):
    runs = orchestrator.resource_change_event(db, {"schedule"})
    # deadline_agent needs schedule; concept_coverage_agent does not.
    # spec_checklist_agent consumes deadline_agent's output, so with T3's
    # transitive input tags a schedule change re-runs it too, after its
    # upstream.
    assert [r.agent_id for r in runs] == ["deadline_agent", "spec_checklist_agent"]
    assert [(q, f) for q, f, _ in enqueued] == [
        ("agents", "run_agent_job"),
        ("agents", "run_agent_job"),
    ]

    runs = orchestrator.resource_change_event(db, {"syllabus", "slides"})
    assert [r.agent_id for r in runs] == ["concept_coverage_agent"]


def test_execute_run_verifies_and_stores_output(
    db, registry, schedule_resource, enqueued
):
    item = schedule_resource.items[0]
    llm = FakeLlm(_deadline_responses(item.id, SCHEDULE_TEXT[:50]))

    run = orchestrator.request_run(db, "deadline_agent", trigger="resource_change")
    assert run.status == "queued"
    run = orchestrator.execute_run(db, llm, run.id)

    assert run.status == "succeeded"
    assert run.verification_summary == {
        "items_total": 1,
        "items_dropped": 0,
        "notes": [],
    }
    assert len(run.items) == 1
    assert run.items[0].verification_status == "verified"
    assert run.items[0].payload["source_refs"][0]["resource_item_id"] == item.id


def test_cacheable_agent_serves_unchanged_inputs_from_cache(
    db, registry, schedule_resource, enqueued
):
    item = schedule_resource.items[0]
    llm = FakeLlm(_deadline_responses(item.id, SCHEDULE_TEXT[:50]))

    first = orchestrator.request_run(db, "deadline_agent", trigger="resource_change")
    orchestrator.execute_run(db, llm, first.id)
    calls_after_first = len(llm.calls)

    second = orchestrator.request_run(db, "deadline_agent", trigger="on_demand")
    assert second.from_cache is True
    assert second.reused_run_id == first.id
    assert len(llm.calls) == calls_after_first  # cache hit costs zero LLM calls

    # display resolves through the reused run
    shown = orchestrator.latest_output_run(db, "deadline_agent")
    assert shown.id == first.id


def test_force_run_bypasses_cache(db, registry, schedule_resource, enqueued):
    item = schedule_resource.items[0]
    llm = FakeLlm(_deadline_responses(item.id, SCHEDULE_TEXT[:50]))

    first = orchestrator.request_run(db, "deadline_agent", trigger="resource_change")
    orchestrator.execute_run(db, llm, first.id)

    # unchanged inputs: a normal request is a cache hit…
    cached = orchestrator.request_run(db, "deadline_agent", trigger="on_demand")
    assert cached.from_cache is True
    # …but force regenerates (the "run/check now" button semantics)
    forced = orchestrator.request_run(
        db, "deadline_agent", trigger="on_demand", force=True
    )
    assert forced.from_cache is False
    assert forced.status == "queued"


def test_editing_content_invalidates_the_cache(
    db, registry, schedule_resource, enqueued
):
    item = schedule_resource.items[0]
    llm = FakeLlm(_deadline_responses(item.id, "November 18"))

    first = orchestrator.request_run(db, "deadline_agent", trigger="resource_change")
    orchestrator.execute_run(db, llm, first.id)

    from app.core.ingestion.extract import content_hash

    item.content_text = SCHEDULE_TEXT.replace("November 17", "November 18")
    item.content_hash = content_hash(item.content_text)
    db.commit()

    third = orchestrator.request_run(db, "deadline_agent", trigger="resource_change")
    assert third.from_cache is False
    assert third.status == "queued"
    assert orchestrator.is_stale(db, "deadline_agent") is True


def test_retagging_invalidates_the_cache(db, registry, schedule_resource, enqueued):
    item = schedule_resource.items[0]
    llm = FakeLlm(_deadline_responses(item.id, SCHEDULE_TEXT[:50]))

    first = orchestrator.request_run(db, "deadline_agent", trigger="resource_change")
    orchestrator.execute_run(db, llm, first.id)

    # a second resource gains the schedule tag → membership changed
    other = make_resource(
        db,
        locator="https://example.edu/extra.html",
        items=[{"title": "Extra", "content": "Final exam Dec 15", "tags": []}],
    )
    db.add(ResourceItemTag(resource_item_id=other.items[0].id, tag="schedule"))
    db.commit()

    second = orchestrator.request_run(db, "deadline_agent", trigger="resource_change")
    assert second.from_cache is False


def test_upstream_resolution_runs_missing_upstream_and_returns_its_output(
    db, registry, schedule_resource, enqueued
):
    # Was a stub check (NotImplementedError); upstream resolution now exists,
    # so assert the real behavior for the same manifest.
    manifest = AgentManifest(
        agent_id="needs_upstream",
        display_name="Downstream",
        description="depends on deadline_agent output",
        required_resource_tags=["reading"],
        optional_upstream_agents=["deadline_agent"],
        output_archetype="text_block",
        trigger="on_demand",
        cacheable=False,
        dashboard_widget=False,
    )
    item = schedule_resource.items[0]
    llm = FakeLlm(_deadline_responses(item.id, "November 17 at 11:59 PM"))
    outputs, run_ids = orchestrator.resolve_upstream_outputs(db, manifest, llm)
    assert [p["title"] for p in outputs["deadline_agent"]] == ["P4 due"]
    assert db.get(AgentRun, run_ids["deadline_agent"]).status == "succeeded"


def test_malformed_agent_output_fails_the_run_loudly(
    db, registry, schedule_resource, enqueued
):
    # agent output missing source_refs → structural failure, not a quiet drop
    llm = FakeLlm(
        {
            "_Deadlines": {
                "deadlines": [
                    {
                        "date": "2026-11-17",
                        "title": "P4 due",
                        "description": "no citation",
                        "source_refs": [],
                    }
                ]
            }
        }
    )
    run = orchestrator.request_run(db, "deadline_agent", trigger="resource_change")
    # pydantic rejects the empty source_refs inside the agent itself (its
    # response schema requires the field) or at validate_payload — either
    # way the run must fail, never succeed with ungrounded output.
    run = orchestrator.execute_run(db, llm, run.id)
    assert run.status == "failed"
    assert db.get(AgentRun, run.id).error
