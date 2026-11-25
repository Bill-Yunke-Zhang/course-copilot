"""Shared test infrastructure.

Tests run against in-memory SQLite (no MySQL needed) and a FakeLlm that
never touches the network — canned responses are keyed by the pydantic
response schema each call declares. The RQ queue is replaced by a recorder;
tests that need a job's effect call the orchestrator directly.
"""

import json
import os
import sys
from pathlib import Path

os.environ["APP_ENV"] = "test"
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project-not-used")

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.llm import LlmClient, LlmResult, ToolTurn  # noqa: E402
from app.database import Base  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
    session = TestingSession()
    yield session
    session.close()


class FakeLlm(LlmClient):
    """LlmClient whose API layer is replaced by canned responses.

    `responses` maps a response schema's __name__ to either a dict (always
    returned) or a list of dicts (consumed one per call — lets a test
    script a fail-then-pass verifier sequence).

    `tool_turns` scripts tool-calling turns: each generate_with_tools call
    consumes the next ToolTurn; when they run out, the model "answers" with
    plain text and no calls.
    """

    def __init__(self, responses: dict | None = None, tool_turns=None):
        super().__init__()
        self.responses = responses or {}
        self.tool_turns: list[ToolTurn] = list(tool_turns or [])
        self.calls: list[dict] = []

    def _call_api_tools(self, model, messages, tools):
        self.calls.append(
            {
                "model": model,
                "messages": list(messages),
                "tools": [t["name"] for t in tools],
            }
        )
        if self.tool_turns:
            return self.tool_turns.pop(0)
        return ToolTurn(
            text="done",
            function_calls=[],
            input_tokens=100,
            output_tokens=10,
            model=model,
        )

    def _call_api(self, model, prompt, response_schema):
        schema_name = response_schema.__name__ if response_schema else None
        self.calls.append({"model": model, "prompt": prompt, "schema": schema_name})
        data = self.responses.get(schema_name, {})
        if isinstance(data, list):
            data = data.pop(0) if len(data) > 1 else data[0]
        parsed = response_schema.model_validate(data) if response_schema else None
        return LlmResult(
            text=json.dumps(data),
            parsed=parsed,
            input_tokens=100,
            output_tokens=50,
            model=model,
        )


@pytest.fixture
def enqueued(monkeypatch):
    """Replace the RQ queue with a recorder; returns the record list."""
    calls: list[tuple] = []

    def fake_enqueue(queue_name, func, *args, **kwargs):
        calls.append((queue_name, func.__name__, args))

    import app.core.queue as queue_module

    monkeypatch.setattr(queue_module, "enqueue", fake_enqueue)
    return calls


@pytest.fixture
def registry():
    """A fresh agent registry (re-scanned per test)."""
    from app.core.registry import get_registry, reset_registry

    reset_registry()
    yield get_registry()
    reset_registry()


def make_resource(db, *, kind="url", locator, title="", items):
    """Create a Resource with tagged items.

    `items` is a list of dicts: {path, title, content, tags, meta}.
    """
    from app.core.ingestion.extract import content_hash
    from app.models import Resource, ResourceItem, ResourceItemTag

    resource = Resource(
        kind=kind, locator=locator, title=title or locator, ingest_status="ready"
    )
    db.add(resource)
    db.flush()
    for spec in items:
        item = ResourceItem(
            resource_id=resource.id,
            path=spec.get("path", ""),
            title=spec.get("title", ""),
            content_text=spec.get("content", ""),
            content_hash=content_hash(spec.get("content", "")),
            meta=spec.get("meta", {}),
        )
        db.add(item)
        db.flush()
        for tag in spec.get("tags", []):
            db.add(ResourceItemTag(resource_item_id=item.id, tag=tag))
    db.commit()
    return resource
