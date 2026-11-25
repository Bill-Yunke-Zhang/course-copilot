"""Directory resources expand into one taggable item per readable file."""

from app.core.ingestion.service import ingest_resource
from app.models import Resource
from tests.conftest import FakeLlm


def _classification(tags):
    return {"tags": tags, "lecture_number": None, "date": None, "reasoning": "test"}


def test_directory_ingests_per_file_items(db, registry, enqueued, tmp_path):
    (tmp_path / "week1").mkdir()
    (tmp_path / "week1" / "notes.md").write_text("# Week 1 notes\nRAG basics.")
    (tmp_path / "cheatsheet.txt").write_text("git commands I forget")
    (tmp_path / "image.png").write_bytes(b"\x89PNG")  # not a doc — skipped
    (tmp_path / ".hidden.md").write_text("secret")  # hidden — skipped

    resource = Resource(kind="file", locator=str(tmp_path), ingest_status="pending")
    db.add(resource)
    db.commit()

    llm = FakeLlm({"Classification": _classification(["personal-notes"])})
    result = ingest_resource(db, llm, resource.id)

    assert result.ingest_status == "ready", result.ingest_error
    paths = sorted(item.path for item in result.items)
    assert paths == ["cheatsheet.txt", "week1/notes.md"]
    assert all(item.tags == ["personal-notes"] for item in result.items)
    assert all(item.content_text for item in result.items)


def test_reingest_preserves_user_tags_and_classifies_only_new_files(
    db, registry, enqueued, tmp_path
):
    (tmp_path / "notes.md").write_text("# Notes")
    resource = Resource(kind="file", locator=str(tmp_path), ingest_status="pending")
    db.add(resource)
    db.commit()

    llm = FakeLlm({"Classification": _classification(["personal-notes"])})
    ingest_resource(db, llm, resource.id)

    # The user corrects the classifier's suggestion…
    from app.models import ResourceItemTag

    item = resource.items[0]
    for row in list(item.tag_rows):
        db.delete(row)
    db.flush()
    db.add(ResourceItemTag(resource_item_id=item.id, tag="slides"))
    db.commit()
    db.expire(item, ["tag_rows"])

    # …a new file appears, and the resource is re-ingested.
    (tmp_path / "extra.md").write_text("# Extra")
    calls_before = len([c for c in llm.calls if c["schema"] == "Classification"])
    result = ingest_resource(db, llm, resource.id)
    calls_after = len([c for c in llm.calls if c["schema"] == "Classification"])

    assert result.ingest_status == "ready", result.ingest_error
    by_path = {i.path: i for i in result.items}
    # The corrected tag survived; only the new file was classified.
    assert by_path["notes.md"].tags == ["slides"]
    assert by_path["extra.md"].tags == ["personal-notes"]
    assert calls_after - calls_before == 1


def test_empty_directory_reports_clear_error(db, registry, enqueued, tmp_path):
    resource = Resource(kind="file", locator=str(tmp_path), ingest_status="pending")
    db.add(resource)
    db.commit()

    result = ingest_resource(db, FakeLlm({}), resource.id)
    assert result.ingest_status == "error"
    assert "no readable" in result.ingest_error
