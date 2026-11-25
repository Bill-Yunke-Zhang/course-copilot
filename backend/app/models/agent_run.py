from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AgentRun(Base):
    """One execution (or cache-served request) of an agent.

    Caching model: when a run is requested and the agent is `cacheable` and
    the current input fingerprint matches the last verified run, the harness
    records a new AgentRun with `from_cache=True` and `reused_run_id` set,
    and creates no new output items — the API resolves output through the
    reused run. "N of M requested runs served from cache" (the §13 report)
    is then a simple count.
    """

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(64), index=True)
    # queued | running | verifying | succeeded | failed
    status: Mapped[str] = mapped_column(String(16), default="queued")
    # What caused this run: resource_change | on_demand | upstream_update | manual
    trigger: Mapped[str] = mapped_column(String(24), default="on_demand")
    # Hash of the resolved input state (resource item ids + content hashes,
    # plus upstream run ids). Two runs with equal fingerprints saw identical
    # inputs — the basis of the caching/staleness policy.
    input_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    from_cache: Mapped[bool] = mapped_column(Boolean, default=False)
    reused_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    # The on_demand request payload (e.g. the quiz free-text), if any.
    request_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Filled by the verifier: {"items_total": N, "items_dropped": M, "notes": [...]}
    verification_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    items: Mapped[list["OutputItem"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="OutputItem.position",
    )


class OutputItem(Base):
    """One item of agent output, in one of the declared archetype shapes.

    `payload` holds the archetype fields (see app/core/archetypes.py),
    including `source_refs` — every item must cite at least one.
    """

    __tablename__ = "output_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE")
    )
    archetype: Mapped[str] = mapped_column(String(32))
    position: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict] = mapped_column(JSON)
    # verified | dropped  (items never reach the UI unverified; dropped items
    # are kept for the disclosure report and grading, but excluded from display)
    verification_status: Mapped[str] = mapped_column(String(16), default="verified")
    verification_attempts: Mapped[int] = mapped_column(Integer, default=0)
    verifier_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[AgentRun] = relationship(back_populates="items")
