"""Quiz page API (T6). All logic lives in app/quiz_service.py."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import quiz_service
from app.database import get_db
from app.schemas.api import AgentRunOut

router = APIRouter(prefix="/api/quiz", tags=["quiz"])


class QuizStart(BaseModel):
    text: str
    # Regenerate even if an identical request is cached ("new questions").
    force: bool = False


class AttemptCreate(BaseModel):
    run_id: int


class AnswerIn(BaseModel):
    question_id: int
    answer_index: int | None = None


class AttemptSubmit(BaseModel):
    answers: list[AnswerIn]


def _fail(exc: quiz_service.QuizServiceError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


@router.post("/runs", status_code=202)
def start_quiz(body: QuizStart, db: Session = Depends(get_db)) -> dict:
    try:
        run = quiz_service.start_quiz(db, body.text, force=body.force)
    except quiz_service.QuizServiceError as exc:
        raise _fail(exc) from None
    return {"run": AgentRunOut.model_validate(run).model_dump()}


@router.get("/runs/{run_id}")
def quiz_run(run_id: int, db: Session = Depends(get_db)) -> dict:
    """Run status plus requested-vs-verified disclosure (no answers)."""
    try:
        run = quiz_service.get_quiz_run(db, run_id)
    except quiz_service.QuizServiceError as exc:
        raise _fail(exc) from None
    return {
        "run": AgentRunOut.model_validate(run).model_dump(),
        "disclosure": quiz_service.quiz_disclosure(db, run),
    }


@router.post("/attempts", status_code=201)
def create_attempt(body: AttemptCreate, db: Session = Depends(get_db)) -> dict:
    try:
        attempt = quiz_service.create_attempt(db, body.run_id)
    except quiz_service.QuizServiceError as exc:
        raise _fail(exc) from None
    return quiz_service.attempt_view(attempt)


@router.get("/attempts")
def list_attempts(db: Session = Depends(get_db)) -> list[dict]:
    return [quiz_service.attempt_view(a) for a in quiz_service.list_attempts(db)]


@router.get("/attempts/{attempt_id}")
def get_attempt(attempt_id: int, db: Session = Depends(get_db)) -> dict:
    try:
        return quiz_service.attempt_view(quiz_service.get_attempt(db, attempt_id))
    except quiz_service.QuizServiceError as exc:
        raise _fail(exc) from None


@router.post("/attempts/{attempt_id}/submit")
def submit_attempt(
    attempt_id: int, body: AttemptSubmit, db: Session = Depends(get_db)
) -> dict:
    answers: dict[int, int | None] = {}
    for answer in body.answers:
        if answer.question_id in answers:
            raise HTTPException(422, f"question {answer.question_id} answered twice")
        answers[answer.question_id] = answer.answer_index
    try:
        attempt = quiz_service.submit_attempt(db, attempt_id, answers)
    except quiz_service.QuizServiceError as exc:
        raise _fail(exc) from None
    return quiz_service.attempt_view(attempt)
