from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ToolCall(Base):
    """One tool call an agent's model asked for, run or not.

    The trace behind GET /api/runs/{run_id}/tool-calls. The tool loop
    (app/core/tool_loop.py) writes a row per requested call, including
    calls it refused — a refused call is the evidence that containment
    worked. Never store a secret here: `result_preview` is shown in the UI.
    """

    __tablename__ = "tool_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    step: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(128))
    args: Mapped[dict] = mapped_column(JSON, default=dict)
    # ok | not_allowed | refused | error
    status: Mapped[str] = mapped_column(String(16))
    # A short, human-readable slice of the result or the refusal reason.
    result_preview: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
