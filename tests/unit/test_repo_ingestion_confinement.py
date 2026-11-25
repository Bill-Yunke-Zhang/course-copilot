"""Repo ingestion never reads outside the clone (D1 in docs/local-acceptance.md).

A cloned repo is untrusted: git checks symlinks out as symlinks, so a file
named like a document can point anywhere the worker can read. These tests
plant harmless sentinel files OUTSIDE the repo and check that no sentinel
text reaches the walked entries, the database, or any LLM prompt, while
ordinary in-repo documents still ingest.
"""

import os
from pathlib import Path

import pytest

from app.core.ingestion import repo_fetch
from app.core.ingestion.service import ingest_resource
from app.models import Resource, ResourceItem
from tests.conftest import FakeLlm

OUTSIDE_SENTINEL = "SENTINEL-OUTSIDE-6f1d2a: this text lives outside the repo"
GIT_SENTINEL = "SENTINEL-GITDIR-93be70: this text lives inside .git"
SENTINELS = (OUTSIDE_SENTINEL, GIT_SENTINEL, "SENTINEL-OUTSIDE-6f1d2a")


def _classification(tags):
    return {"tags": tags, "lecture_number": None, "date": None, "reasoning": "test"}


@pytest.fixture
def hostile_repo(tmp_path) -> Path:
    """A repo with ordinary docs plus every kind of link we refuse or keep."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.md").write_text(OUTSIDE_SENTINEL)
    (outside / "nested").mkdir()
    (outside / "nested" / "deep.md").write_text(OUTSIDE_SENTINEL)

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(GIT_SENTINEL)
    (repo / "docs").mkdir()
    (repo / "src").mkdir()

    # Ordinary repository content: still ingested.
    (repo / "README.md").write_text("# Project\nOrdinary in-repo readme.")
    (repo / "docs" / "guide.md").write_text("Ordinary in-repo guide.")
    (repo / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n")

    # Refused: targets outside the clone.
    os.symlink(outside / "sentinel.md", repo / "notes.md")  # absolute
    os.symlink("../outside/sentinel.md", repo / "relative-escape.md")
    os.symlink("../../outside/sentinel.md", repo / "docs" / "nested-escape.txt")
    os.symlink("notes.md", repo / "chain.md")  # in-repo hop, then outside
    os.symlink(outside, repo / "linked_dir")  # directory link: never walked
    # Refused: aliases into .git/.
    os.symlink(".git/config", repo / "git-alias.md")
    os.symlink(repo / ".git", repo / "gitdir_link")
    # Skipped: broken and cyclic links.
    os.symlink("does-not-exist.md", repo / "broken.md")
    os.symlink("loop-b.md", repo / "loop-a.md")
    os.symlink("loop-a.md", repo / "loop-b.md")
    os.symlink("self.md", repo / "self.md")
    # Kept: a link that stays inside the clone.
    os.symlink("docs/guide.md", repo / "guide-link.md")
    return repo


def test_walk_repo_refuses_links_outside_or_into_git(hostile_repo):
    entries = {e["path"]: e for e in repo_fetch.walk_repo(hostile_repo)}

    assert sorted(entries) == [
        "README.md",
        "docs/guide.md",
        "guide-link.md",
        "src/app.py",
    ]
    assert entries["README.md"]["content"].startswith("# Project")
    assert entries["docs/guide.md"]["content"] == "Ordinary in-repo guide."
    # Non-doc files are still listed, with empty content (project-code).
    assert entries["src/app.py"]["content"] == ""
    # An in-repo link is read through its validated target.
    assert entries["guide-link.md"]["content"] == "Ordinary in-repo guide."
    walked = repr(entries)
    assert not any(s in walked for s in SENTINELS)


@pytest.mark.parametrize(
    "rel",
    [
        "notes.md",
        "relative-escape.md",
        "docs/nested-escape.txt",
        "chain.md",
        "linked_dir/sentinel.md",
        "git-alias.md",
        "gitdir_link/config",
        ".git/config",
        "broken.md",
        "loop-a.md",
        "self.md",
    ],
)
def test_contained_file_refuses_each_unsafe_entry(hostile_repo, rel):
    assert repo_fetch.contained_file(hostile_repo, rel) is None


def test_contained_file_returns_resolved_target_inside_repo(hostile_repo):
    target = repo_fetch.contained_file(hostile_repo, "guide-link.md")
    assert target == (hostile_repo / "docs" / "guide.md").resolve()
    assert not target.is_symlink()


def test_repo_ingestion_keeps_sentinels_out_of_db_and_prompts(
    db, registry, enqueued, monkeypatch, hostile_repo
):
    monkeypatch.setattr(
        repo_fetch, "clone_or_pull", lambda db, url, ref: Path(hostile_repo)
    )
    resource = Resource(
        kind="repo",
        locator="https://gitlab.example.invalid/team/project.git",
        ingest_status="pending",
    )
    db.add(resource)
    db.commit()

    llm = FakeLlm({"Classification": _classification(["project-spec"])})
    result = ingest_resource(db, llm, resource.id)

    assert result.ingest_status == "ready", result.ingest_error
    rows = db.query(ResourceItem).filter_by(resource_id=resource.id).all()
    by_path = {row.path: row for row in rows}
    assert sorted(by_path) == [
        "README.md",
        "docs/guide.md",
        "guide-link.md",
        "src/app.py",
    ]
    # Ordinary docs were classified; code defaults to project-code.
    assert by_path["README.md"].tags == ["project-spec"]
    assert by_path["src/app.py"].tags == ["project-code"]

    stored = repr([(r.path, r.title, r.content_text, r.meta) for r in rows])
    prompts = "\n".join(c.get("prompt", "") for c in llm.calls)
    assert "Ordinary in-repo readme." in prompts
    for sentinel in SENTINELS:
        assert sentinel not in stored
        assert sentinel not in prompts


def test_ordinary_repo_ingestion_unchanged(tmp_path):
    repo = tmp_path / "plain"
    (repo / "slides").mkdir(parents=True)
    (repo / "node_modules" / "pkg").mkdir(parents=True)
    (repo / "README.md").write_text("Readme")
    (repo / "slides" / "week1.md").write_text("Week 1")
    (repo / "main.c").write_text("int main(void) { return 0; }")
    (repo / "node_modules" / "pkg" / "index.md").write_text("vendored")

    entries = repo_fetch.walk_repo(repo)

    assert [e["path"] for e in entries] == ["README.md", "main.c", "slides/week1.md"]
    assert [e["title"] for e in entries] == ["README.md", "main.c", "week1.md"]
    assert [e["content"] for e in entries] == ["Readme", "", "Week 1"]
