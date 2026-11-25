"""RQ job functions — executed by the worker container.

The worker shares this codebase and image with the API (see
docker-compose.yml: `rq worker agents ingestion`). Each job opens its own
database session.
"""

from app.core.llm import LlmClient

_llm = LlmClient()


def run_agent_job(run_id: int) -> None:
    from app.core.orchestrator import execute_run
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        execute_run(db, _llm, run_id)
    finally:
        db.close()


def ingest_resource_job(resource_id: int) -> None:
    from app.core.ingestion.service import ingest_resource
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        ingest_resource(db, _llm, resource_id)
    finally:
        db.close()
