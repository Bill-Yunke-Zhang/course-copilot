"""Quiz API end to end (T6): free text -> queued run -> harness execution with
verification -> attempts snapshotting verified questions -> server-side
scoring; plus cache reuse/invalidation and independent attempts."""

import pytest
from fastapi.testclient import TestClient

from app.core import orchestrator
from app.database import get_db
from app.main import app
from app.models import AgentRun, LlmCall, OutputItem, QuizAttempt, QuizAttemptQuestion
from tests.conftest import FakeLlm, make_resource

NOTES = (
    "Gradient descent updates weights by stepping against the gradient. "
    "The learning rate controls the step size. "
    "Momentum accumulates past gradients."
)
TEXT = "Quiz me on unit 1 with 3 multiple choice questions"

QUESTIONS = [
    {
        "question": "What does the learning rate control?",
        "choices": ["Step size", "Batch size", "Depth", "Width"],
        "correct_index": 0,
        "topic": "Learning rate",
        "resource_item_id": None,  # filled per test
        "excerpt": "The learning rate controls the step size.",
    },
    {
        "question": "Gradient descent steps in which direction?",
        "choices": ["Along the gradient", "Against the gradient", "Randomly"],
        "correct_index": 1,
        "topic": "Gradient descent",
        "resource_item_id": None,
        "excerpt": "stepping against the gradient",
    },
    {
        "question": "What does momentum accumulate?",
        "choices": ["Past gradients", "Labels", "Layers"],
        "correct_index": 0,
        "topic": "Momentum",
        "resource_item_id": None,
        "excerpt": "Momentum accumulates past gradients.",
    },
]


@pytest.fixture
def client(db, enqueued, registry):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def notes_id(db):
    resource = make_resource(
        db,
        kind="file",
        locator="/data/notes/lecture2.md",
        items=[
            {
                "title": "Lecture 2 notes",
                "content": NOTES,
                "tags": ["personal-notes"],
                "meta": {"lecture": 2, "unit": 1},
            }
        ],
    )
    return resource.items[0].id


def _fake(notes_id, judge=None):
    questions = [{**q, "resource_item_id": notes_id} for q in QUESTIONS]
    return FakeLlm(
        {
            "_ParsedRequest": {
                "scope": "unit 1",
                "whole_course": False,
                "count": 3,
                "format": "multiple_choice",
                "format_text": "multiple choice",
            },
            "_ScopeSelection": {
                "status": "matched",
                "in_scope_item_ids": [notes_id],
                "scope_label": "Unit 1",
                "explanation": "Lecture 2 notes carry unit 1 metadata.",
            },
            "_Questions": {"questions": questions},
            # q1 verified; q2 fails twice -> dropped; q3 verified.
            "_JudgeResponse": judge
            or [
                {"supported": True, "reason": "ok"},
                {"supported": False, "reason": "excerpt does not say this"},
                {"supported": False, "reason": "still unsupported"},
                {"supported": True, "reason": "ok"},
            ],
            "_Deadlines": {"deadlines": []},
            "_Topics": {"topics": []},
        }
    )


def _start(client, text=TEXT, force=False):
    response = client.post("/api/quiz/runs", json={"text": text, "force": force})
    assert response.status_code == 202, response.text
    return response.json()["run"]


def _run_to_completion(client, db, fake, text=TEXT):
    run = _start(client, text)
    assert run["status"] == "queued"
    orchestrator.execute_run(db, fake, run["id"])
    return client.get(f"/api/quiz/runs/{run['id']}").json()


def test_start_quiz_enqueues_raw_text_payload(client, db, enqueued, notes_id):
    run = _start(client, "  Quiz me on   unit 1  ")
    assert ("agents", "run_agent_job", (run["id"],)) in enqueued
    row = db.get(AgentRun, run["id"])
    assert row.agent_id == "quiz_agent"
    assert row.trigger == "on_demand"
    # One payload shape for every caller; whitespace collapsed, words kept.
    assert row.request_payload == {"text": "Quiz me on unit 1"}


def test_blank_request_is_rejected(client):
    assert client.post("/api/quiz/runs", json={"text": "   "}).status_code == 422


def test_run_discloses_requested_vs_verified_and_never_pads(client, db, notes_id):
    body = _run_to_completion(client, db, _fake(notes_id))
    assert body["run"]["status"] == "succeeded"
    d = body["disclosure"]
    assert d["requested_count"] == 3
    assert d["generated_count"] == 3
    assert d["verified_count"] == 2
    assert d["dropped_count"] == 1
    assert d["shortfall"] is True
    assert d["scope_label"] == "Unit 1"
    assert d["material_item_ids"] == [notes_id]
    assert d["verifier_notes"] == ["still unsupported"]


def test_attempt_snapshots_only_verified_questions_and_hides_answers(
    client, db, notes_id
):
    body = _run_to_completion(client, db, _fake(notes_id))
    run_id = body["run"]["id"]
    response = client.post("/api/quiz/attempts", json={"run_id": run_id})
    assert response.status_code == 201
    attempt = response.json()
    assert attempt["total"] == 2
    assert [q["question"] for q in attempt["questions"]] == [
        QUESTIONS[0]["question"],
        QUESTIONS[2]["question"],
    ]
    # Not yet submitted: no answers or (answer-revealing) citations.
    for q in attempt["questions"]:
        assert "correct_index" not in q and "source_refs" not in q

    row = db.get(QuizAttempt, attempt["id"])
    assert row.raw_request == TEXT
    assert row.agent_run_id == run_id
    assert row.resolved_scope["scope"] == "unit 1"
    assert row.resolved_scope["format"] == "multiple_choice"
    assert row.resolved_scope["requested_count"] == 3
    assert row.resolved_scope["verified_count"] == 2
    assert row.resolved_scope["material_item_ids"] == [notes_id]
    assert row.resolved_scope["verifier_notes"] == ["still unsupported"]
    assert row.score is None and row.completed_at is None
    questions = sorted(row.questions, key=lambda q: q.id)
    assert [q.topic for q in questions] == ["Learning rate", "Momentum"]
    assert questions[0].question_payload["source_refs"] == [
        {"resource_item_id": notes_id, "excerpt": QUESTIONS[0]["excerpt"]}
    ]
    assert questions[0].question_payload["correct_index"] == 0


def test_submit_scores_from_persisted_questions(client, db, notes_id):
    body = _run_to_completion(client, db, _fake(notes_id))
    attempt = client.post(
        "/api/quiz/attempts", json={"run_id": body["run"]["id"]}
    ).json()
    q1, q3 = attempt["questions"]

    # Client-supplied correctness/score fields are ignored entirely.
    response = client.post(
        f"/api/quiz/attempts/{attempt['id']}/submit",
        json={
            "answers": [
                {"question_id": q1["id"], "answer_index": 0, "is_correct": False},
                {"question_id": q3["id"], "answer_index": 2, "is_correct": True},
            ],
            "score": 99,
        },
    )
    assert response.status_code == 200, response.text
    graded = response.json()
    assert graded["score"] == 1 and graded["total"] == 2
    wrong = [q for q in graded["questions"] if not q["is_correct"]]
    assert len(wrong) == 1
    assert wrong[0]["student_answer_index"] == 2
    assert wrong[0]["correct_index"] == 0
    assert wrong[0]["source_refs"][0]["excerpt"] == QUESTIONS[2]["excerpt"]

    row = db.get(QuizAttempt, attempt["id"])
    assert row.score == 1 and row.completed_at is not None
    persisted = {q.id: (q.student_answer_index, q.is_correct) for q in row.questions}
    assert persisted == {q1["id"]: (0, True), q3["id"]: (2, False)}

    # Scored once.
    again = client.post(
        f"/api/quiz/attempts/{attempt['id']}/submit", json={"answers": []}
    )
    assert again.status_code == 409


def test_submit_validates_answers(client, db, notes_id):
    body = _run_to_completion(client, db, _fake(notes_id))
    attempt = client.post(
        "/api/quiz/attempts", json={"run_id": body["run"]["id"]}
    ).json()
    q1, q3 = attempt["questions"]
    url = f"/api/quiz/attempts/{attempt['id']}/submit"
    out_of_range = {"answers": [{"question_id": q1["id"], "answer_index": 9}]}
    assert client.post(url, json=out_of_range).status_code == 422
    foreign = {"answers": [{"question_id": 10_000, "answer_index": 0}]}
    assert client.post(url, json=foreign).status_code == 422
    # Nothing was recorded by the rejected submissions; unanswered = wrong.
    graded = client.post(
        url, json={"answers": [{"question_id": q1["id"], "answer_index": 0}]}
    ).json()
    assert graded["score"] == 1
    unanswered = next(q for q in graded["questions"] if q["id"] == q3["id"])
    assert unanswered["student_answer_index"] is None
    assert unanswered["is_correct"] is False


def test_count_cap_is_disclosed_even_when_every_question_verifies(client, db, notes_id):
    fake = _fake(notes_id, judge={"supported": True, "reason": "ok"})
    fake.responses["_ParsedRequest"]["count"] = 500
    fake.responses["_Questions"] = {
        "questions": [
            {
                **QUESTIONS[n % 3],
                "question": f"Question {n}?",
                "resource_item_id": notes_id,
            }
            for n in range(25)
        ]
    }
    body = _run_to_completion(client, db, fake, text="Quiz me on unit 1 with 500 MC")
    d = body["disclosure"]
    # Original request, applied limit and verified count are all disclosed.
    assert d["raw_request"] == "Quiz me on unit 1 with 500 MC"
    assert (d["asked_count"], d["requested_count"], d["verified_count"]) == (
        500,
        25,
        25,
    )
    assert d["shortfall"] is False  # full success, yet the cap is still reported
    assert any("capped at 25" in n for n in d["agent_notes"])

    attempt = client.post("/api/quiz/attempts", json={"run_id": body["run"]["id"]})
    scope = attempt.json()["resolved_scope"]
    assert (scope["asked_count"], scope["requested_count"]) == (500, 25)
    assert len(attempt.json()["questions"]) == 25


def test_cached_quiz_reuses_output_and_allows_independent_attempts(
    client, db, notes_id
):
    fake = _fake(notes_id)
    first = _run_to_completion(client, db, fake)
    calls_after_first = db.query(LlmCall).count()
    items_after_first = db.query(OutputItem).count()

    hit = _start(client)  # identical request, unchanged inputs
    assert hit["status"] == "succeeded"
    assert hit["from_cache"] is True
    assert hit["reused_run_id"] == first["run"]["id"]
    assert db.query(LlmCall).count() == calls_after_first  # zero LLM calls
    assert db.query(OutputItem).count() == items_after_first

    a1 = client.post("/api/quiz/attempts", json={"run_id": first["run"]["id"]}).json()
    a2 = client.post("/api/quiz/attempts", json={"run_id": hit["id"]}).json()
    assert a1["id"] != a2["id"]
    assert [q["question"] for q in a1["questions"]] == [
        q["question"] for q in a2["questions"]
    ]
    assert {q["id"] for q in a1["questions"]}.isdisjoint(
        {q["id"] for q in a2["questions"]}
    )
    assert a2["agent_run_id"] == hit["id"]
    assert a2["resolved_scope"]["from_cache"] is True
    assert a2["resolved_scope"]["output_run_id"] == first["run"]["id"]

    def answer(attempt, index):
        return {
            "answers": [
                {"question_id": q["id"], "answer_index": index}
                for q in attempt["questions"]
            ]
        }

    s1 = client.post(f"/api/quiz/attempts/{a1['id']}/submit", json=answer(a1, 0)).json()
    s2 = client.post(f"/api/quiz/attempts/{a2['id']}/submit", json=answer(a2, 1)).json()
    assert (s1["score"], s2["score"]) == (2, 0)
    assert db.get(QuizAttempt, a1["id"]).score == 2  # unaffected by a2
    assert db.query(QuizAttemptQuestion).count() == 4

    history = client.get("/api/quiz/attempts").json()
    assert [a["id"] for a in history] == [a2["id"], a1["id"]]


def test_different_request_misses_and_returning_to_earlier_request_regenerates(
    client, db, notes_id
):
    """A, then B, then A again: only the latest materialized run is a cache
    candidate, so the third request regenerates instead of reviving A's
    run. Displayed output, what a downstream consumer receives, and the
    upstream run id it would fingerprint must all name the same run."""
    from app.agents import quiz_agent
    from app.core.registry import get_registry

    probe = quiz_agent.MANIFEST.model_copy(
        update={"agent_id": "probe_agent", "optional_upstream_agents": ["quiz_agent"]}
    )

    def assert_consistent(expected_run_id):
        shown = orchestrator.latest_output_run(db, "quiz_agent")
        payloads, consumed_ids = orchestrator.resolve_upstream_outputs(db, probe)
        fingerprinted = orchestrator.current_upstream_run_ids(db, probe)
        assert shown.id == expected_run_id
        assert consumed_ids == fingerprinted == {"quiz_agent": expected_run_id}
        stored = [
            row.payload
            for row in db.query(OutputItem)
            .filter_by(agent_run_id=expected_run_id, verification_status="verified")
            .order_by(OutputItem.position)
        ]
        assert payloads["quiz_agent"] == stored

    assert get_registry().get("quiz_agent").manifest.agent_id == "quiz_agent"
    fake = _fake(notes_id, judge={"supported": True, "reason": "ok"})
    a = _run_to_completion(client, db, fake)
    assert_consistent(a["run"]["id"])

    # B is distinguishable from A: one different question, a different request.
    only_b = {**QUESTIONS[0], "question": "Which quantity sets the step size?"}
    fake_b = _fake(notes_id, judge={"supported": True, "reason": "ok"})
    fake_b.responses["_Questions"] = {
        "questions": [{**only_b, "resource_item_id": notes_id}]
    }
    b = _start(client, "Quiz me on unit 1 with 1 multiple choice question")
    assert b["status"] == "queued"  # different request -> regenerate
    orchestrator.execute_run(db, fake_b, b["id"])
    assert_consistent(b["id"])
    b_questions = [p["question"] for p in _payloads(db, b["id"])]
    assert b_questions == ["Which quantity sets the step size?"]

    # Asking A again is a miss: no historical run is revived.
    again = _start(client)
    assert again["status"] == "queued" and again["from_cache"] is False
    assert_consistent(b["id"])  # until it runs, B is still what everyone sees
    orchestrator.execute_run(db, fake, again["id"])
    assert_consistent(again["id"])

    # Now unchanged, A is an ordinary cache hit on its newest run.
    hit = _start(client)
    assert hit["from_cache"] is True and hit["reused_run_id"] == again["id"]
    assert_consistent(again["id"])

    # force=True ("new questions") always regenerates.
    assert _start(client, force=True)["status"] == "queued"


def _payloads(db, run_id):
    return [
        row.payload
        for row in db.query(OutputItem)
        .filter_by(agent_run_id=run_id)
        .order_by(OutputItem.position)
    ]


def test_metadata_only_change_invalidates_cache(client, db, notes_id):
    fake = _fake(notes_id, judge={"supported": True, "reason": "ok"})
    first = _run_to_completion(client, db, fake)
    assert _start(client)["from_cache"] is True

    # Same tags, only the user-editable metadata changes (the scope input).
    response = client.put(
        f"/api/resource-items/{notes_id}/tags",
        json={"tags": ["personal-notes"], "meta": {"lecture": 7, "unit": 3}},
    )
    assert response.status_code == 200
    miss = _start(client)
    assert miss["status"] == "queued" and miss["from_cache"] is False

    orchestrator.execute_run(db, fake, miss["id"])
    scope_prompts = [
        c["prompt"] for c in fake.calls if c.get("schema") == "_ScopeSelection"
    ]
    assert '"lecture": 7' in scope_prompts[-1]  # resolution saw the new metadata
    assert (
        db.get(AgentRun, miss["id"]).input_fingerprint
        != db.get(AgentRun, first["run"]["id"]).input_fingerprint
    )


def test_content_and_upstream_changes_invalidate_cache(client, db, notes_id):
    fake = _fake(notes_id, judge={"supported": True, "reason": "ok"})
    _run_to_completion(client, db, fake)
    assert _start(client)["from_cache"] is True

    # A new upstream output (Deadline agent re-ran) is a different input.
    deadline = AgentRun(agent_id="deadline_agent", status="queued", trigger="manual")
    db.add(deadline)
    db.commit()
    orchestrator.execute_run(db, fake, deadline.id)
    assert _start(client)["status"] == "queued"

    # So is new material in a quizzed tag.
    run = _start(client, "another request")
    orchestrator.execute_run(db, fake, run["id"])
    assert _start(client, "another request")["from_cache"] is True
    make_resource(
        db,
        locator="/data/notes/lecture3.md",
        items=[{"content": "More notes.", "tags": ["reading"]}],
    )
    assert _start(client, "another request")["status"] == "queued"


def test_honest_failure_is_reported_and_not_attemptable(client, db, notes_id):
    fake = _fake(notes_id)
    fake.responses["_ScopeSelection"] = {
        "status": "no_match",
        "in_scope_item_ids": [],
        "scope_label": "",
        "explanation": "No item has unit 9 metadata.",
    }
    body = _run_to_completion(client, db, fake, text="Quiz me on unit 9")
    assert body["run"]["status"] == "failed"
    assert "unit 9" in body["run"]["error"]
    assert body["disclosure"]["verified_count"] == 0
    response = client.post("/api/quiz/attempts", json={"run_id": body["run"]["id"]})
    assert response.status_code == 409
    # A failed run is never served from cache.
    assert _start(client, "Quiz me on unit 9")["status"] == "queued"


def test_all_dropped_is_an_empty_quiz_not_an_attempt(client, db, notes_id):
    fake = _fake(notes_id, judge={"supported": False, "reason": "unsupported"})
    body = _run_to_completion(client, db, fake)
    assert body["run"]["status"] == "succeeded"
    assert body["disclosure"]["verified_count"] == 0
    assert body["disclosure"]["requested_count"] == 3
    assert body["disclosure"]["dropped_count"] == 3
    response = client.post("/api/quiz/attempts", json={"run_id": body["run"]["id"]})
    assert response.status_code == 409


def test_quiz_endpoints_reject_non_quiz_runs_and_unknown_ids(client, db):
    other = AgentRun(agent_id="deadline_agent", status="succeeded")
    db.add(other)
    db.commit()
    assert client.get(f"/api/quiz/runs/{other.id}").status_code == 404
    assert (
        client.post("/api/quiz/attempts", json={"run_id": other.id}).status_code == 404
    )
    assert client.get("/api/quiz/attempts/12345").status_code == 404


def test_start_quiz_service_is_reusable_outside_the_router(
    db, enqueued, registry, notes_id
):
    from app import quiz_service

    run = quiz_service.start_quiz(db, "Quiz me on unit 1")
    assert run.request_payload == {"text": "Quiz me on unit 1"}
    assert ("agents", "run_agent_job", (run.id,)) in enqueued
