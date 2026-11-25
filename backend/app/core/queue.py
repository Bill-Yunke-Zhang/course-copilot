"""Job queue access (Redis + RQ).

Redis is used here strictly as the agent job broker — not a cache, not a
session store. Two queues:

    "agents"    — agent runs (app.worker.run_agent_job)
    "ingestion" — resource ingestion/classification (app.worker.ingest_resource_job)

Tests monkeypatch `enqueue` to run jobs inline or record them.
"""

from redis import Redis
from rq import Queue

from app.config import get_settings

_connection: Redis | None = None


def _get_connection() -> Redis:
    global _connection
    if _connection is None:
        _connection = Redis.from_url(get_settings().redis_url)
    return _connection


def enqueue(queue_name: str, func, *args, **kwargs):
    queue = Queue(queue_name, connection=_get_connection())
    return queue.enqueue(func, *args, **kwargs)
