"""Resource ingestion: extract → classify → (crawl) → fire change event.

Runs on the "ingestion" queue (see app/worker.py) because it makes LLM
calls and network fetches. When ingestion finishes, the resource-change
event fires so agents whose required tags overlap get (cheaply) re-checked.
"""

import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import TAG_VOCABULARY
from app.core.ingestion import classifier, crawler, extract, repo_fetch
from app.core.llm import LlmClient
from app.core.orchestrator import resource_change_event
from app.models import Resource, ResourceItem, ResourceItemTag

logger = logging.getLogger(__name__)


def ingest_resource(db: Session, llm: LlmClient, resource_id: int) -> Resource:
    resource = db.get(Resource, resource_id)
    if resource is None:
        raise KeyError(f"no resource with id {resource_id}")

    resource.ingest_status = "ingesting"
    resource.ingest_error = None
    # Tags carried before re-ingestion: an item that disappears (or whose
    # re-classified tags differ) changes what those tags resolve to too.
    tags_before = {t for item in resource.items for t in item.tags}
    db.commit()

    try:
        if resource.kind == "file":
            path = Path(resource.locator)
            if not path.exists():
                raise ValueError(
                    f"path not found inside the container: {resource.locator}. "
                    "Uploaded resources live in the shared 'uploads' volume — "
                    "if the stack was started before that volume existed, "
                    "recreate the containers (docker compose up -d) and "
                    "re-upload. Host paths must be mounted into BOTH the "
                    "backend and worker services (see docker-compose.yml)."
                )
            if path.is_dir():
                _ingest_directory(db, llm, resource)
            else:
                _ingest_single(
                    db, llm, resource, extract.extract_file(resource.locator)
                )
        elif resource.kind == "url":
            html = extract.fetch_url(resource.locator)
            item = _ingest_single(db, llm, resource, extract.html_to_text(html))
            # The schedule/syllabus pages are crawled for outbound links,
            # each registered as its own discovered resource (§4.4).
            if {"schedule", "syllabus"} & set(item.tags):
                _crawl_page(db, llm, resource, html)
        elif resource.kind == "repo":
            _ingest_repo(db, llm, resource)
        else:
            raise ValueError(f"unknown resource kind {resource.kind!r}")

        resource.ingest_status = "ready"
        db.commit()

        changed = tags_before | {t for item in resource.items for t in item.tags}
        resource_change_event(db, changed)
        return resource
    except Exception as exc:
        db.rollback()
        resource.ingest_status = "error"
        resource.ingest_error = str(exc)
        db.commit()
        logger.exception("Ingestion failed for resource %s", resource_id)
        return resource


def _replace_items(
    db: Session, resource: Resource, new_items: list[ResourceItem]
) -> set[str]:
    """Swap in a fresh set of items, carrying tags/meta across by path.

    User tag corrections are never ground truth to be re-derived — an item
    at the same path keeps its existing tags and metadata through
    re-ingestion (content still refreshes), and callers skip re-classifying
    it. Only genuinely new paths get a classification call. Returns the set
    of carried paths.
    """
    carried_state: dict[str, tuple[list[str], dict]] = {
        old.path: (old.tags, old.meta or {}) for old in resource.items
    }
    for old in list(resource.items):
        db.delete(old)
    db.flush()
    for item in new_items:
        db.add(item)
    db.flush()

    carried_paths: set[str] = set()
    for item in new_items:
        tags, old_meta = carried_state.get(item.path, ([], {}))
        if not tags:
            continue
        for tag in tags:
            db.add(ResourceItemTag(resource_item_id=item.id, tag=tag))
        # Old user-editable meta survives; freshly computed keys (e.g. a
        # repo item's git evidence) win where they overlap.
        item.meta = {**old_meta, **(item.meta or {})}
        carried_paths.add(item.path)
    db.flush()
    # The relationship collection was loaded (pre-add) above — expire it so
    # later reads in this session (e.g. the change-event tag sweep) see the
    # new items instead of a stale cache.
    db.expire(resource, ["items"])
    return carried_paths


def _apply_tags(db: Session, item: ResourceItem, tags: list[str]) -> None:
    for tag in tags:
        if tag not in TAG_VOCABULARY:
            logger.warning("Classifier produced non-vocabulary tag %r — ignored", tag)
            continue
        db.add(ResourceItemTag(resource_item_id=item.id, tag=tag))
    db.flush()


def _ingest_single(
    db: Session, llm: LlmClient, resource: Resource, text: str
) -> ResourceItem:
    item = ResourceItem(
        resource_id=resource.id,
        path="",
        title=resource.title or resource.locator,
        content_text=text,
        content_hash=extract.content_hash(text),
        meta={},
    )
    carried = _replace_items(db, resource, [item])

    if "" not in carried:
        result = classifier.classify_item(
            db,
            llm,
            kind=resource.kind,
            locator=resource.locator,
            path="",
            content=text,
            context=_discovery_context(resource),
            resource_id=resource.id,
        )
        item.meta = _meta_from(result)
        _apply_tags(db, item, [t.value for t in result.tags])
    db.commit()
    return item


def _crawl_page(db: Session, llm: LlmClient, resource: Resource, html: str) -> None:
    candidates = crawler.extract_links(html, resource.locator)
    decisions = crawler.classify_links(
        db, llm, resource.locator, candidates, resource_id=resource.id
    )

    from app.core.queue import enqueue

    for candidate in candidates:
        tag = decisions.get(candidate.url, "skip")
        if tag == "skip":
            continue
        already = db.query(Resource).filter(Resource.locator == candidate.url).first()
        if already is not None:
            continue
        discovered = Resource(
            kind="url",
            locator=candidate.url,
            title=candidate.text or candidate.url,
            discovered_from_id=resource.id,
            ingest_status="pending",
        )
        db.add(discovered)
        db.flush()
        # Register the discovered resource with its link-derived tag right
        # away (a placeholder item so agents can resolve it), then queue a
        # full ingestion, which fetches its content and re-classifies.
        placeholder = ResourceItem(
            resource_id=discovered.id,
            path="",
            title=discovered.title,
            content_text=f"(discovered link, not yet fetched) {candidate.url}",
            content_hash="",
            meta={"discovered_label": candidate.label},
        )
        db.add(placeholder)
        db.flush()
        _apply_tags(db, placeholder, [tag])

        from app import worker

        enqueue("ingestion", worker.ingest_resource_job, discovered.id)
    db.commit()


def _ingest_directory(db: Session, llm: LlmClient, resource: Resource) -> None:
    """A local directory expands into one taggable item per readable file,
    exactly like a repo does — notes folders shouldn't need to be added
    file by file."""
    root = Path(resource.locator)
    items: list[ResourceItem] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(
            part in repo_fetch.SKIP_DIRS or part.startswith(".") for part in path.parts
        ):
            continue
        if path.suffix.lower() not in repo_fetch.DOC_SUFFIXES:
            continue
        try:
            text = extract.extract_file(str(path))
        except Exception:
            continue
        items.append(
            ResourceItem(
                resource_id=resource.id,
                path=str(path.relative_to(root)),
                title=path.name,
                content_text=text,
                content_hash=extract.content_hash(text),
                meta={},
            )
        )
    if not items:
        raise ValueError(
            f"no readable text/PDF files found under directory {resource.locator}"
        )
    carried = _replace_items(db, resource, items)

    for item in items:
        if item.path in carried:
            continue
        result = classifier.classify_item(
            db,
            llm,
            kind="file",
            locator=resource.locator,
            path=item.path,
            content=item.content_text,
            resource_id=resource.id,
        )
        item.meta = _meta_from(result)
        _apply_tags(db, item, [t.value for t in result.tags])
    db.commit()


def _ingest_repo(db: Session, llm: LlmClient, resource: Resource) -> None:
    # Public repos (no credential_ref) clone anonymously and work in the
    # starter. Private repos need a PAT from SecretStore; with none stored it
    # raises MissingCredential, and that message is surfaced on the resource
    # in the UI.
    repo_path = repo_fetch.clone_or_pull(db, resource.locator, resource.credential_ref)
    evidence = repo_fetch.repo_git_evidence(repo_path)

    items: list[ResourceItem] = []
    for entry in repo_fetch.walk_repo(repo_path):
        items.append(
            ResourceItem(
                resource_id=resource.id,
                path=entry["path"],
                title=entry["title"],
                content_text=entry["content"],
                content_hash=extract.content_hash(entry["content"] or entry["path"]),
                meta={"git": evidence} if not entry["content"] else {},
            )
        )
    carried = _replace_items(db, resource, items)

    for item in items:
        if item.path in carried:
            continue
        if item.content_text:
            result = classifier.classify_item(
                db,
                llm,
                kind="repo",
                locator=resource.locator,
                path=item.path,
                content=item.content_text,
                resource_id=resource.id,
            )
            item.meta = {**item.meta, **_meta_from(result)}
            _apply_tags(db, item, [t.value for t in result.tags])
        else:
            # Non-doc files in a repo default to project-code (spec §4.3) —
            # still user-editable like every other tag.
            _apply_tags(db, item, ["project-code"])
    db.commit()


def _meta_from(result: classifier.Classification) -> dict:
    meta: dict = {}
    if result.lecture_number is not None:
        meta["lecture_number"] = result.lecture_number
    if result.date:
        meta["date"] = result.date
    return meta


def _discovery_context(resource: Resource) -> str:
    if resource.discovered_from_id is None:
        return ""
    return "discovered by crawling another course page"
