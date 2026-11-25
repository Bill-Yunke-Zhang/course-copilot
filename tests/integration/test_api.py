"""API surface tests (SQLite-backed, queue recorded, no LLM calls)."""

import pytest
from fastapi.testclient import TestClient

from app.config import TAG_VOCABULARY
from app.database import get_db
from app.main import app
from tests.conftest import make_resource


@pytest.fixture
def client(db, enqueued, registry):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_tag_vocabulary_endpoint(client):
    assert client.get("/api/tags").json() == TAG_VOCABULARY


def test_create_resource_enqueues_ingestion(client, enqueued):
    response = client.post(
        "/api/resources",
        json={"kind": "url", "locator": "https://example.edu/schedule.html"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["ingest_status"] == "pending"
    assert ("ingestion", "ingest_resource_job", (body["id"],)) in enqueued


def test_repo_resource_public_vs_private_credential(client):
    # No credential_ref → public repo, cloned anonymously.
    public = client.post(
        "/api/resources",
        json={"kind": "repo", "locator": "https://gitlab.com/x/public.git"},
    )
    assert public.json()["credential_ref"] is None
    # Explicit credential_ref → private repo, fetched via SecretStore.
    private = client.post(
        "/api/resources",
        json={
            "kind": "repo",
            "locator": "https://git.doit.wisc.edu/x/y.git",
            "credential_ref": "gitlab-pat",
        },
    )
    assert private.json()["credential_ref"] == "gitlab-pat"


def test_upload_single_file_creates_resource(client, enqueued, tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        response = client.post(
            "/api/resources/upload",
            files=[("files", ("notes.md", b"# my notes", "text/markdown"))],
            data={"relative_paths": '["notes.md"]', "title": ""},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["kind"] == "file"
        assert body["locator"].endswith("_notes.md")
        from pathlib import Path

        assert Path(body["locator"]).read_bytes() == b"# my notes"
        assert ("ingestion", "ingest_resource_job", (body["id"],)) in enqueued
    finally:
        get_settings.cache_clear()


def test_upload_folder_preserves_relative_paths(client, tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        response = client.post(
            "/api/resources/upload",
            files=[
                ("files", ("a.md", b"aaa", "text/markdown")),
                ("files", ("b.md", b"bbb", "text/markdown")),
            ],
            data={
                "relative_paths": '["notes/week1/a.md", "notes/week2/b.md"]',
                "title": "my notes",
            },
        )
        assert response.status_code == 201
        from pathlib import Path

        root = Path(response.json()["locator"])
        assert (root / "notes/week1/a.md").read_bytes() == b"aaa"
        assert (root / "notes/week2/b.md").read_bytes() == b"bbb"
    finally:
        get_settings.cache_clear()


def test_upload_folder_named_after_picked_folder(client, tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        response = client.post(
            "/api/resources/upload",
            files=[
                ("files", ("a.md", b"aaa", "text/markdown")),
                ("files", ("b.md", b"bbb", "text/markdown")),
            ],
            data={
                "relative_paths": '["lecture-notes/a.md", "lecture-notes/b.md"]',
                "title": "",
            },
        )
        body = response.json()
        # The picked folder's real name, not an anonymous "upload".
        assert body["title"] == "lecture-notes"
        assert body["locator"].endswith("_lecture-notes")
    finally:
        get_settings.cache_clear()


def test_bulk_tags_apply_to_a_subtree_only(client, db, enqueued):
    resource = make_resource(
        db,
        locator="/data/uploads/x_lecture-notes",
        kind="file",
        items=[
            {"path": "week1/a.md", "title": "a", "content": "aa", "tags": ["reading"]},
            {"path": "week1/b.md", "title": "b", "content": "bb", "tags": []},
            {"path": "week2/c.md", "title": "c", "content": "cc", "tags": ["reading"]},
        ],
    )
    enqueued.clear()
    response = client.put(
        f"/api/resources/{resource.id}/bulk-tags",
        json={"path_prefix": "week1", "tags": ["slides"]},
    )
    assert response.status_code == 200
    tags_by_path = {i["path"]: i["tags"] for i in response.json()["items"]}
    assert tags_by_path["week1/a.md"] == ["slides"]
    assert tags_by_path["week1/b.md"] == ["slides"]
    assert tags_by_path["week2/c.md"] == ["reading"]  # untouched
    # the bulk edit fired the resource-change path
    assert ("agents", "run_agent_job") in {(q, f) for q, f, _ in enqueued}

    bad = client.put(
        f"/api/resources/{resource.id}/bulk-tags",
        json={"path_prefix": "week1", "tags": ["office-hours"]},
    )
    assert bad.status_code == 422
    missing = client.put(
        f"/api/resources/{resource.id}/bulk-tags",
        json={"path_prefix": "nope", "tags": ["slides"]},
    )
    assert missing.status_code == 404


def test_missing_container_path_gets_actionable_error(client, db, enqueued):
    from app.core.ingestion.service import ingest_resource
    from tests.conftest import FakeLlm

    created = client.post(
        "/api/resources",
        json={"kind": "file", "locator": "/data/uploads/gone/nope.md"},
    ).json()
    result = ingest_resource(db, FakeLlm({}), created["id"])
    assert result.ingest_status == "error"
    assert "not found inside the container" in result.ingest_error


def test_upload_rejects_path_traversal(client, tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        response = client.post(
            "/api/resources/upload",
            files=[
                ("files", ("evil.md", b"x", "text/markdown")),
                ("files", ("ok.md", b"y", "text/markdown")),
            ],
            data={"relative_paths": '["../evil.md", "ok.md"]', "title": "t"},
        )
        assert response.status_code == 422
    finally:
        get_settings.cache_clear()


def test_invalid_kind_rejected(client):
    response = client.post(
        "/api/resources", json={"kind": "carrier-pigeon", "locator": "x"}
    )
    assert response.status_code == 422


def test_tag_edit_validates_vocabulary_and_fires_change_event(client, db, enqueued):
    resource = make_resource(
        db,
        locator="https://example.edu/schedule.html",
        items=[{"title": "Schedule", "content": "stuff", "tags": ["schedule"]}],
    )
    item_id = resource.items[0].id

    bad = client.put(
        f"/api/resource-items/{item_id}/tags", json={"tags": ["office-hours"]}
    )
    assert bad.status_code == 422

    enqueued.clear()
    good = client.put(
        f"/api/resource-items/{item_id}/tags", json={"tags": ["syllabus"]}
    )
    assert good.status_code == 200
    assert good.json()["tags"] == ["syllabus"]
    # schedule removed + syllabus added → both shipped agents overlap
    assert ("agents", "run_agent_job") in {(q, f) for q, f, _ in enqueued}


def test_agents_listing_shows_manifests_and_staleness(client):
    body = client.get("/api/agents").json()
    ids = {a["manifest"]["agent_id"] for a in body}
    assert {"deadline_agent", "concept_coverage_agent"} <= ids
    assert all(a["stale"] is True for a in body)  # nothing has run yet


def test_dashboard_has_one_card_per_widget_agent(client):
    cards = client.get("/api/dashboard").json()
    ids = [c["manifest"]["agent_id"] for c in cards]
    assert ids == ["concept_coverage_agent", "deadline_agent"]
    assert all(c["items"] == [] for c in cards)


def test_on_demand_run_is_accepted_and_enqueued(client, enqueued):
    enqueued.clear()
    response = client.post("/api/agents/deadline_agent/runs", json={"payload": None})
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert [(q, f) for q, f, _ in enqueued] == [("agents", "run_agent_job")]


def test_unknown_agent_404(client):
    assert client.post("/api/agents/nope/runs", json={}).status_code == 404


def test_cost_summary_empty_state(client):
    body = client.get("/api/costs/summary").json()
    assert body["total_cost_usd"] == 0.0
    assert body["runs_requested"] >= 0
