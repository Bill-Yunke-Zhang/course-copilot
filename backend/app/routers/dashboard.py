from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.orchestrator import is_stale, latest_output_run
from app.core.registry import get_registry
from app.database import get_db
from app.schemas.api import AgentRunOut, DashboardCard, ManifestOut, OutputItemOut

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db)) -> list[DashboardCard]:
    """One card per registered agent with dashboard_widget=True.

    This is the whole dashboard subsystem: it reuses the same
    manifest-driven registration as everything else, so a newly dropped-in
    agent that sets dashboard_widget appears here with no other wiring.
    """
    cards: list[DashboardCard] = []
    for agent in get_registry().all():
        manifest = agent.manifest
        if not manifest.dashboard_widget:
            continue
        run = latest_output_run(db, manifest.agent_id)
        items = (
            [
                OutputItemOut.model_validate(i)
                for i in run.items
                if i.verification_status == "verified"
            ]
            if run
            else []
        )
        cards.append(
            DashboardCard(
                manifest=ManifestOut(**manifest.model_dump()),
                run=AgentRunOut.model_validate(run) if run else None,
                items=items,
                stale=is_stale(db, manifest.agent_id),
            )
        )
    return cards
