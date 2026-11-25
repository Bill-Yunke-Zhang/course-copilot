"""Quiz application service: starting quiz runs and persisting attempts.

This is the layer between the Quiz agent and its callers (the Quiz page's
API in app/routers/quiz.py, and later the MCP `request_quiz` tool). The
agent itself never writes to the database; everything here does, through
the shipped `quiz_attempts` / `quiz_attempt_questions` tables.

Flow:
    start_quiz(text)            -> AgentRun (queued, or a cache hit)
    create_attempt(run_id)      -> QuizAttempt snapshotting the run's
                                   VERIFIED questions only
    submit_attempt(id, answers) -> scored from the persisted questions; the
                                   client's view of what is correct is never
                                   trusted

A cache-hit run reuses an earlier run's verified questions, and each
create_attempt() call makes a new, independent attempt, so the same quiz
can be taken again.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.orchestrator import request_run
from app.models import AgentRun, OutputItem, QuizAttempt, QuizAttemptQuestion

QUIZ_AGENT_ID = "quiz_agent"
MAX_REQUEST_CHARS = 1_000


class QuizServiceError(ValueError):
    """A caller error: `status` is the HTTP status the API should answer."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def quiz_request_payload(text: str) -> dict:
    """The one request payload shape for every quiz caller.

    Whitespace is collapsed so trivially different spellings of the same
    request share a cache entry; the words themselves are kept as typed.
    """
    normalized = " ".join((text or "").split())
    if not normalized:
        raise QuizServiceError("Type what you want to be quizzed on.", 422)
    if len(normalized) > MAX_REQUEST_CHARS:
        raise QuizServiceError(
            f"The request is too long (max {MAX_REQUEST_CHARS} characters).", 422
        )
    return {"text": normalized}


def start_quiz(db: Session, text: str, force: bool = False) -> AgentRun:
    """Request a quiz run from free text, exactly as the Quiz page does.

    Returns the AgentRun: `queued` (the worker will run it), or `succeeded`
    with from_cache=True when identical inputs were already quizzed on.
    `force=True` regenerates even when a cached quiz exists.
    """
    payload = quiz_request_payload(text)
    return request_run(
        db, QUIZ_AGENT_ID, trigger="on_demand", request=payload, force=force
    )


# ── Reading a run's quiz ─────────────────────────────────────────────────────


def get_quiz_run(db: Session, run_id: int) -> AgentRun:
    run = db.get(AgentRun, run_id)
    if run is None or run.agent_id != QUIZ_AGENT_ID:
        raise QuizServiceError("quiz run not found", 404)
    return run


def output_run(db: Session, run: AgentRun) -> AgentRun:
    """The run that holds the items (a cache hit points at the reused run)."""
    if run.from_cache and run.reused_run_id is not None:
        reused = db.get(AgentRun, run.reused_run_id)
        if reused is not None:
            return reused
    return run


def _items(db: Session, run_id: int) -> list[OutputItem]:
    return list(
        db.scalars(
            select(OutputItem)
            .where(OutputItem.agent_run_id == run_id)
            .order_by(OutputItem.position)
        ).all()
    )


def quiz_disclosure(db: Session, run: AgentRun) -> dict:
    """Requested vs. verified counts and how the scope resolved, for a run.

    Read from the agent's `quiz_request` field (carried by every item,
    dropped ones included) and the harness's verification summary.
    """
    source = output_run(db, run)
    items = _items(db, source.id)
    meta = (
        next(
            (
                i.payload.get("quiz_request")
                for i in items
                if i.payload.get("quiz_request")
            ),
            None,
        )
        or {}
    )
    verified = sum(1 for i in items if i.verification_status == "verified")
    summary = source.verification_summary or {}
    requested = meta.get("count")
    return {
        "raw_request": (run.request_payload or {}).get("text", ""),
        "scope": meta.get("scope", ""),
        "scope_label": meta.get("scope_label", ""),
        "resolution": meta.get("resolution", ""),
        "format": meta.get("format"),
        "material_item_ids": meta.get("material_item_ids", []),
        "requested_count": requested,
        "asked_count": meta.get("requested_count"),
        "generated_count": len(items),
        "verified_count": verified,
        "dropped_count": summary.get("items_dropped", len(items) - verified),
        "verifier_notes": summary.get("notes", []),
        "agent_notes": meta.get("notes", []),
        "shortfall": requested is not None and verified < requested,
        "output_run_id": source.id,
        "from_cache": run.from_cache,
    }


# ── Attempts ─────────────────────────────────────────────────────────────────


def create_attempt(db: Session, run_id: int) -> QuizAttempt:
    """Start a new attempt on a finished quiz run.

    Snapshots only the verified questions (as presented, with their
    citations) so later re-runs can't change what this attempt asked.
    """
    run = get_quiz_run(db, run_id)
    if run.status != "succeeded":
        raise QuizServiceError(
            f"quiz run {run_id} is {run.status}; an attempt needs a finished quiz", 409
        )
    source = output_run(db, run)
    verified = [i for i in _items(db, source.id) if i.verification_status == "verified"]
    if not verified:
        raise QuizServiceError(
            "none of this quiz's questions passed verification, so there is "
            "nothing to attempt",
            409,
        )
    disclosure = quiz_disclosure(db, run)
    attempt = QuizAttempt(
        raw_request=disclosure["raw_request"],
        resolved_scope={
            key: disclosure[key]
            for key in (
                "scope",
                "scope_label",
                "resolution",
                "format",
                "material_item_ids",
                "requested_count",
                "asked_count",
                "generated_count",
                "verified_count",
                "dropped_count",
                "verifier_notes",
                "agent_notes",
                "output_run_id",
                "from_cache",
            )
        },
        agent_run_id=run.id,
    )
    for item in verified:
        payload = {
            k: item.payload[k]
            for k in (
                "question",
                "choices",
                "correct_index",
                "source_refs",
                "topic",
                "scope",
            )
            if k in item.payload
        }
        attempt.questions.append(
            QuizAttemptQuestion(
                question_payload=payload,
                topic=str(payload.get("topic") or disclosure["scope_label"] or "")[
                    :255
                ],
            )
        )
    db.add(attempt)
    db.commit()
    db.refresh(attempt)
    return attempt


def get_attempt(db: Session, attempt_id: int) -> QuizAttempt:
    attempt = db.get(QuizAttempt, attempt_id)
    if attempt is None:
        raise QuizServiceError("quiz attempt not found", 404)
    return attempt


def list_attempts(db: Session, limit: int = 20) -> list[QuizAttempt]:
    return list(
        db.scalars(
            select(QuizAttempt).order_by(QuizAttempt.id.desc()).limit(limit)
        ).all()
    )


def submit_attempt(
    db: Session, attempt_id: int, answers: dict[int, int | None]
) -> QuizAttempt:
    """Record the student's answers and score them.

    `answers` maps quiz_attempt_questions.id -> chosen choice index (None or
    a missing id means unanswered, which scores as incorrect). Correctness
    comes only from the persisted question payload. An attempt is scored
    once; resubmitting is a 409.
    """
    attempt = get_attempt(db, attempt_id)
    if attempt.completed_at is not None:
        raise QuizServiceError("this attempt was already submitted", 409)
    question_ids = {q.id for q in attempt.questions}
    unknown = sorted(set(answers) - question_ids)
    if unknown:
        raise QuizServiceError(
            f"answers for questions not in this attempt: {unknown}", 422
        )

    score = 0
    for question in attempt.questions:
        choice = answers.get(question.id)
        n_choices = len(question.question_payload.get("choices", []))
        if choice is not None and not 0 <= choice < n_choices:
            raise QuizServiceError(
                f"answer {choice} is out of range for question {question.id}", 422
            )
        question.student_answer_index = choice
        question.is_correct = (
            choice is not None
            and choice == question.question_payload.get("correct_index")
        )
        score += int(question.is_correct)
    attempt.score = score
    attempt.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(attempt)
    return attempt


def attempt_view(attempt: QuizAttempt) -> dict:
    """An attempt as the API returns it.

    Until it is submitted, answers and citations are withheld (the excerpt
    usually gives the answer away); after submission, everything is shown
    for review.
    """
    done = attempt.completed_at is not None
    questions = []
    for q in sorted(attempt.questions, key=lambda q: q.id):
        p = q.question_payload
        row = {
            "id": q.id,
            "question": p.get("question", ""),
            "choices": p.get("choices", []),
            "topic": q.topic,
        }
        if done:
            row.update(
                correct_index=p.get("correct_index"),
                source_refs=p.get("source_refs", []),
                student_answer_index=q.student_answer_index,
                is_correct=q.is_correct,
            )
        questions.append(row)
    return {
        "id": attempt.id,
        "raw_request": attempt.raw_request,
        "resolved_scope": attempt.resolved_scope or {},
        "agent_run_id": attempt.agent_run_id,
        "score": attempt.score,
        "total": len(questions),
        "created_at": attempt.created_at,
        "completed_at": attempt.completed_at,
        "questions": questions,
    }
