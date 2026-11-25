from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AgentRun, LlmCall
from app.schemas.api import CostByAgent, CostSummary

router = APIRouter(prefix="/api", tags=["costs"])


@router.get("/costs/summary")
def cost_summary(db: Session = Depends(get_db)) -> CostSummary:
    """Cumulative token cost and the effect of the caching policy (§13)."""
    total_cost, total_calls = db.execute(
        select(func.coalesce(func.sum(LlmCall.cost_usd), 0.0), func.count(LlmCall.id))
    ).one()

    by_purpose = {
        purpose: float(cost)
        for purpose, cost in db.execute(
            select(LlmCall.purpose, func.sum(LlmCall.cost_usd)).group_by(
                LlmCall.purpose
            )
        )
    }

    by_agent = [
        CostByAgent(
            agent_id=agent_id,
            calls=calls,
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            cost_usd=float(cost or 0.0),
        )
        for agent_id, calls, input_tokens, output_tokens, cost in db.execute(
            select(
                AgentRun.agent_id,
                func.count(LlmCall.id),
                func.sum(LlmCall.input_tokens),
                func.sum(LlmCall.output_tokens),
                func.sum(LlmCall.cost_usd),
            )
            .join(AgentRun, LlmCall.agent_run_id == AgentRun.id)
            .group_by(AgentRun.agent_id)
        )
    ]

    runs_requested, runs_from_cache = _run_counts(db)

    return CostSummary(
        total_cost_usd=float(total_cost),
        total_calls=int(total_calls),
        by_purpose=by_purpose,
        by_agent=by_agent,
        runs_requested=runs_requested,
        runs_from_cache=runs_from_cache,
    )


def _run_counts(db: Session) -> tuple[int, int]:
    total = db.scalar(select(func.count(AgentRun.id))) or 0
    cached = (
        db.scalar(select(func.count(AgentRun.id)).where(AgentRun.from_cache.is_(True)))
        or 0
    )
    return int(total), int(cached)
