"""SecretStore encrypts the GitLab PAT at rest (T4); the repo path uses it."""

import base64
import os
import stat

import pytest

from app.config import get_settings
from app.core.ingestion.service import ingest_resource
from app.core.secret_store import MissingCredential, SecretStore, SecretStoreError
from app.models import Credential, Resource
from tests.conftest import FakeLlm

TOKEN = "glpat-example-0123456789"


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    """Point SecretStore at a key file under tmp_path, with no env key."""
    path = tmp_path / "secrets" / "secret_store.key"
    monkeypatch.setenv("SECRET_KEY_FILE", str(path))
    monkeypatch.delenv("SECRET_STORE_KEY", raising=False)
    get_settings.cache_clear()
    yield path
    get_settings.cache_clear()


def test_save_then_get_round_trips(db, key_file):
    store = SecretStore(db)
    store.save("gitlab-pat", TOKEN)
    assert store.get("gitlab-pat") == TOKEN
    # A fresh store (new request) reads the same row with the same key.
    assert SecretStore(db).get("gitlab-pat") == TOKEN


def test_saving_again_replaces_the_token(db, key_file):
    store = SecretStore(db)
    store.save("gitlab-pat", TOKEN)
    store.save("gitlab-pat", "glpat-example-replaced")
    assert store.get("gitlab-pat") == "glpat-example-replaced"
    assert db.query(Credential).count() == 1


def test_surrounding_whitespace_is_stripped(db, key_file):
    SecretStore(db).save("gitlab-pat", f"  {TOKEN}\n")
    assert SecretStore(db).get("gitlab-pat") == TOKEN


def test_empty_token_is_refused(db, key_file):
    with pytest.raises(SecretStoreError, match="empty"):
        SecretStore(db).save("gitlab-pat", "   ")


def test_database_never_holds_the_plaintext(db, key_file):
    SecretStore(db).save("gitlab-pat", TOKEN)
    first = db.query(Credential).one().encrypted_value
    assert TOKEN.encode() not in first
    assert base64.b64encode(TOKEN.encode()) not in first
    # A fresh nonce per save: the same token never encrypts to the same bytes.
    SecretStore(db).save("gitlab-pat", TOKEN)
    assert db.query(Credential).one().encrypted_value != first


def test_key_file_is_generated_private_and_holds_no_token(db, key_file):
    assert not key_file.exists()
    SecretStore(db).save("gitlab-pat", TOKEN)
    assert stat.S_IMODE(os.stat(key_file).st_mode) == 0o600
    assert TOKEN not in key_file.read_text()
    assert len(base64.urlsafe_b64decode(key_file.read_text())) == 32


def test_env_key_wins_over_the_key_file(db, key_file, monkeypatch):
    monkeypatch.setenv("SECRET_STORE_KEY", base64.urlsafe_b64encode(b"k" * 32).decode())
    get_settings.cache_clear()
    SecretStore(db).save("gitlab-pat", TOKEN)
    assert not key_file.exists()
    assert SecretStore(db).get("gitlab-pat") == TOKEN


def test_malformed_env_key_is_a_clear_error(db, key_file, monkeypatch):
    monkeypatch.setenv("SECRET_STORE_KEY", "too-short")
    get_settings.cache_clear()
    with pytest.raises(SecretStoreError, match="32 bytes"):
        SecretStore(db).save("gitlab-pat", TOKEN)


def test_missing_token_is_an_actionable_error(db, key_file):
    with pytest.raises(MissingCredential, match="Settings"):
        SecretStore(db).get("gitlab-pat")


def test_wrong_key_fails_clearly_without_leaking(db, key_file):
    SecretStore(db).save("gitlab-pat", TOKEN)
    key_file.write_text(base64.urlsafe_b64encode(b"x" * 32).decode())
    with pytest.raises(SecretStoreError, match="can't be decrypted") as exc:
        SecretStore(db).get("gitlab-pat")
    assert TOKEN not in str(exc.value)


def test_lost_key_file_fails_clearly(db, key_file):
    SecretStore(db).save("gitlab-pat", TOKEN)
    key_file.unlink()
    with pytest.raises(SecretStoreError, match="key .* is missing"):
        SecretStore(db).get("gitlab-pat")


def test_ciphertext_is_bound_to_its_credential_ref(db, key_file):
    store = SecretStore(db)
    store.save("gitlab-pat", TOKEN)
    store.save("other", "glpat-example-other")
    rows = {r.credential_ref: r for r in db.query(Credential)}
    rows["other"].encrypted_value = rows["gitlab-pat"].encrypted_value
    db.commit()
    with pytest.raises(SecretStoreError, match="can't be decrypted"):
        store.get("other")


def test_private_repo_without_token_reports_actionable_error(db, key_file):
    resource = Resource(
        kind="repo",
        locator="https://git.doit.wisc.edu/example/team-repo.git",
        credential_ref="gitlab-pat",
        ingest_status="pending",
    )
    db.add(resource)
    db.commit()

    result = ingest_resource(db, FakeLlm({}), resource.id)
    assert result.ingest_status == "error"
    assert "No GitLab PAT is stored" in result.ingest_error
    assert "Settings" in result.ingest_error


def test_private_clone_uses_the_stored_token(db, key_file, monkeypatch, tmp_path):
    """End to end through the real store: the decrypted token reaches git as
    the Basic auth header and nowhere in argv."""
    from app.core.ingestion import repo_fetch

    seen = []

    class FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        seen.append((cmd, kwargs.get("env") or {}))
        return FakeCompleted()

    SecretStore(db).save("gitlab-pat", TOKEN)
    monkeypatch.setattr(repo_fetch.subprocess, "run", fake_run)
    monkeypatch.setenv("REPO_CLONE_DIR", str(tmp_path / "repos"))
    get_settings.cache_clear()
    repo_fetch.clone_or_pull(
        db, "https://git.example.edu/team/private.git", "gitlab-pat"
    )
    cmd, env = seen[0]
    assert TOKEN not in " ".join(cmd)
    expected = base64.b64encode(f"oauth2:{TOKEN}".encode()).decode()
    assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {expected}"


def test_public_repo_clones_anonymously_without_secret_store(db, monkeypatch, tmp_path):
    """A repo with no credential_ref never touches SecretStore (whose stub
    would raise) and clones with the plain URL — public repos work in the
    starter."""
    from app.core.ingestion import repo_fetch

    commands = []

    class FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        return FakeCompleted()

    monkeypatch.setattr(repo_fetch.subprocess, "run", fake_run)
    monkeypatch.setenv("REPO_CLONE_DIR", str(tmp_path))
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        dest = repo_fetch.clone_or_pull(
            db, "https://gitlab.com/example/public-repo.git", None
        )
        assert commands[0][:2] == ["git", "clone"]
        assert commands[0][2] == "https://gitlab.com/example/public-repo.git"
        assert "oauth2" not in commands[0][2]
        assert str(dest).startswith(str(tmp_path))
    finally:
        get_settings.cache_clear()


def test_private_clone_never_puts_the_token_in_the_url_or_argv(
    db, monkeypatch, tmp_path
):
    """Git saves the clone URL in .git/config, so a token in the URL would
    sit on disk in plaintext. The token travels as a header via env only."""
    from app.core.ingestion import repo_fetch

    seen = []

    class FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        seen.append((cmd, kwargs.get("env") or {}))
        return FakeCompleted()

    class StubStore:
        def __init__(self, db):
            pass

        def get(self, ref):
            return "glpat-SECRET-123"

    monkeypatch.setattr(repo_fetch.subprocess, "run", fake_run)
    monkeypatch.setattr(repo_fetch, "SecretStore", StubStore)
    monkeypatch.setenv("REPO_CLONE_DIR", str(tmp_path))
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        repo_fetch.clone_or_pull(
            db, "https://git.example.edu/team/private.git", "gitlab-pat"
        )
        cmd, env = seen[0]
        assert "glpat-SECRET-123" not in " ".join(cmd)
        assert cmd[2] == "https://git.example.edu/team/private.git"
        assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
        assert env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    finally:
        get_settings.cache_clear()
