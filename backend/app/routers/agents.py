from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.orchestrator import is_stale, latest_output_run, request_run
from app.core.registry import get_registry
from app.database import get_db
from app.models import AgentRun, ToolCall
from app.schemas.api import (
    AgentOut,
    AgentRunOut,
    ManifestOut,
    OutputItemOut,
    RunRequest,
    ToolCallOut,
)

router = APIRouter(prefix="/api", tags=["agents"])


def _latest_run(db: Session, agent_id: str) -> AgentRun | None:
    return db.scalars(
        select(AgentRun)
        .where(AgentRun.agent_id == agent_id)
        .order_by(AgentRun.id.desc())
        .limit(1)
    ).first()


@router.get("/agents")
def list_agents(db: Session = Depends(get_db)) -> list[AgentOut]:
    out = []
    for agent in get_registry().all():
        manifest = agent.manifest
        latest = _latest_run(db, manifest.agent_id)
        out.append(
            AgentOut(
                manifest=ManifestOut(**manifest.model_dump()),
                latest_run=AgentRunOut.model_validate(latest) if latest else None,
                stale=is_stale(db, manifest.agent_id),
            )
        )
    return out


@router.post("/agents/{agent_id}/runs", status_code=202)
def trigger_run(
    agent_id: str, body: RunRequest, db: Session = Depends(get_db)
) -> AgentRunOut:
    try:
        get_registry().get(agent_id)
    except KeyError:
        raise HTTPException(404, f"no agent {agent_id!r}") from None
    run = request_run(
        db, agent_id, trigger="on_demand", request=body.payload, force=body.force
    )
    return AgentRunOut.model_validate(run)


@router.get("/agents/{agent_id}/output")
def agent_output(agent_id: str, db: Session = Depends(get_db)) -> dict:
    """The latest verified output for display (resolves cache-hit runs)."""
    try:
        get_registry().get(agent_id)
    except KeyError:
        raise HTTPException(404, f"no agent {agent_id!r}") from None
    run = latest_output_run(db, agent_id)
    if run is None:
        return {"run": None, "items": []}
    items = [
        OutputItemOut.model_validate(i)
        for i in run.items
        if i.verification_status == "verified"
    ]
    return {
        "run": AgentRunOut.model_validate(run).model_dump(),
        "items": [i.model_dump() for i in items],
    }


@router.get("/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db)) -> AgentRunOut:
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return AgentRunOut.model_validate(run)


@router.get("/runs/{run_id}/tool-calls")
def get_run_tool_calls(run_id: int, db: Session = Depends(get_db)) -> list[ToolCallOut]:
    """The run's tool-call trace, in call order (refused calls included)."""
    if db.get(AgentRun, run_id) is None:
        raise HTTPException(404, "run not found")
    calls = db.scalars(
        select(ToolCall)
        .where(ToolCall.agent_run_id == run_id)
        .order_by(ToolCall.step, ToolCall.id)
    ).all()
    return [ToolCallOut.model_validate(c) for c in calls]
