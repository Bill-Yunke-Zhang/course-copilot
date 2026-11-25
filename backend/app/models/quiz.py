"""Quiz attempt persistence.

The Quiz Agent itself is student work (spec §9.1), but its schema ships in
the starter because the Weak-Topic Identifier pick-list agent consumes this
attempt history — the tables must exist in every team's database regardless
of which pick-list agent the team chooses. The tables ship empty.
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class QuizAttempt(Base):
    __tablename__ = "quiz_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    # The raw free-text request ("Quiz me on unit 1 with 20 MC questions").
    raw_request: Mapped[str] = mapped_column(Text)
    # The agent's parsed {scope, count, format} and how the scope resolved.
    resolved_scope: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    agent_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    questions: Mapped[list["QuizAttemptQuestion"]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan"
    )


class QuizAttemptQuestion(Base):
    __tablename__ = "quiz_attempt_questions"

    id: Mapped[int] = mapped_column(primary_key=True)
    quiz_attempt_id: Mapped[int] = mapped_column(
        ForeignKey("quiz_attempts.id", ondelete="CASCADE")
    )
    # The qa_item payload as presented (question, choices, correct_index,
    # source_refs).
    question_payload: Mapped[dict] = mapped_column(JSON)
    # The topic/scope this question was drawn from — what the Weak-Topic
    # Identifier aggregates over.
    topic: Mapped[str] = mapped_column(String(255), default="")
    student_answer_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    attempt: Mapped[QuizAttempt] = relationship(back_populates="questions")
