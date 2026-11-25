"""PAT entry API (T4): the token goes in, is stored encrypted, and never
comes back out of any response, error or log."""

import logging

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.core.secret_store import SecretStore
from app.database import get_db
from app.main import app
from app.models import Credential

TOKEN = "glpat-example-api-0123456789"


@pytest.fixture
def client(db, enqueued, registry, tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / "secrets" / "key"))
    monkeypatch.delenv("SECRET_STORE_KEY", raising=False)
    get_settings.cache_clear()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def test_status_before_anything_is_stored(client):
    body = client.get("/api/credentials/gitlab-pat").json()
    assert body == {"credential_ref": "gitlab-pat", "stored": False, "updated_at": None}


def test_save_reports_stored_and_never_echoes_the_token(client, db):
    response = client.put("/api/credentials/gitlab-pat", json={"token": TOKEN})
    assert response.status_code == 200
    assert response.json()["stored"] is True
    assert TOKEN not in response.text

    status = client.get("/api/credentials/gitlab-pat")
    assert status.json()["stored"] is True
    assert status.json()["updated_at"] is not None
    assert TOKEN not in status.text

    # Encrypted at rest, and decryptable by the repo-fetch path.
    assert TOKEN.encode() not in db.query(Credential).one().encrypted_value
    assert SecretStore(db).get("gitlab-pat") == TOKEN


def test_saving_again_replaces_the_token(client, db):
    client.put("/api/credentials/gitlab-pat", json={"token": TOKEN})
    client.put("/api/credentials/gitlab-pat", json={"token": "glpat-example-new"})
    assert SecretStore(db).get("gitlab-pat") == "glpat-example-new"
    assert db.query(Credential).count() == 1


def test_delete_removes_the_token(client, db):
    client.put("/api/credentials/gitlab-pat", json={"token": TOKEN})
    assert client.delete("/api/credentials/gitlab-pat").status_code == 204
    assert client.get("/api/credentials/gitlab-pat").json()["stored"] is False
    assert db.query(Credential).count() == 0
    assert client.delete("/api/credentials/gitlab-pat").status_code == 404


def test_blank_token_is_rejected_without_storing(client, db):
    response = client.put("/api/credentials/gitlab-pat", json={"token": "   "})
    assert response.status_code == 422
    assert "empty" in response.json()["detail"]
    assert db.query(Credential).count() == 0


@pytest.mark.parametrize(
    "body",
    [
        {"token": [TOKEN]},
        {"token": {"value": TOKEN}},
        {"tokn": TOKEN},
    ],
)
def test_malformed_body_errors_never_echo_the_token(client, body):
    response = client.put("/api/credentials/gitlab-pat", json=body)
    assert response.status_code == 422
    assert TOKEN not in response.text


@pytest.mark.parametrize("ref", ["GitLab", "a b", "x" * 65, "..%2Fetc"])
def test_bad_credential_ref_is_rejected(client, ref):
    response = client.put(f"/api/credentials/{ref}", json={"token": TOKEN})
    assert response.status_code in (404, 422)
    assert TOKEN not in response.text


def test_token_never_reaches_the_logs(client, caplog):
    with caplog.at_level(logging.DEBUG):
        client.put("/api/credentials/gitlab-pat", json={"token": TOKEN})
        client.get("/api/credentials/gitlab-pat")
        client.put("/api/credentials/gitlab-pat", json={"token": [TOKEN]})
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("raw", [TOKEN, f'"{TOKEN}"', "{not json " + TOKEN])
def test_non_object_bodies_are_rejected_without_echo(client, raw):
    response = client.put(
        "/api/credentials/gitlab-pat",
        content=raw,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert TOKEN not in response.text
