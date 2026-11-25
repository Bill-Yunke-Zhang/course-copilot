import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import (
    agents,
    costs,
    credentials,
    dashboard,
    health,
    quiz,
    resources,
)

logger = logging.getLogger(__name__)


def _migrate_with_retry(attempts: int = 30, delay_seconds: float = 2.0) -> None:
    """Run alembic upgrade head, waiting for MySQL to come up on a fresh stack."""
    from alembic import command
    from alembic.config import Config

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", get_settings().database_url)
    for attempt in range(1, attempts + 1):
        try:
            command.upgrade(config, "head")
            return
        except Exception as exc:
            if attempt == attempts:
                raise
            logger.info("Waiting for database (%s/%s): %s", attempt, attempts, exc)
            time.sleep(delay_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if get_settings().app_env != "test":
        _migrate_with_retry()
    # Fail fast on a broken agents/ directory rather than on first request.
    from app.core.registry import get_registry

    get_registry()
    yield


app = FastAPI(title="Course Copilot", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(resources.router)
app.include_router(agents.router)
app.include_router(dashboard.router)
app.include_router(costs.router)
app.include_router(credentials.router)
app.include_router(quiz.router)
