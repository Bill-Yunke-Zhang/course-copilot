import json
import re
import shutil
import uuid
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import TAG_VOCABULARY, get_settings
from app.core.orchestrator import resource_change_event
from app.models import Resource, ResourceItem, ResourceItemTag
from app.schemas.api import (
    BulkTagsUpdate,
    ItemTagsUpdate,
    ResourceCreate,
    ResourceItemOut,
    ResourceOut,
    ResourceUpdate,
)
from app.database import get_db

router = APIRouter(prefix="/api", tags=["resources"])


def _resource_out(resource: Resource) -> ResourceOut:
    out = ResourceOut.model_validate(resource)
    out.items = [
        ResourceItemOut(
            id=item.id,
            path=item.path,
            title=item.title,
            tags=item.tags,
            meta=item.meta or {},
            content_preview=item.content_text[:300],
        )
        for item in resource.items
    ]
    return out


@router.get("/tags")
def list_tags() -> list[str]:
    return TAG_VOCABULARY


@router.get("/resources")
def list_resources(db: Session = Depends(get_db)) -> list[ResourceOut]:
    resources = db.scalars(
        select(Resource)
        .options(selectinload(Resource.items).selectinload(ResourceItem.tag_rows))
        .order_by(Resource.id)
    ).all()
    return [_resource_out(r) for r in resources]


@router.post("/resources", status_code=201)
def create_resource(body: ResourceCreate, db: Session = Depends(get_db)) -> ResourceOut:
    if body.kind not in ("file", "url", "repo"):
        raise HTTPException(422, "kind must be file, url, or repo")
    # A repo with a credential_ref is private (fetched via SecretStore);
    # without one it is treated as public and cloned anonymously.
    resource = Resource(
        kind=body.kind,
        locator=body.locator,
        title=body.title,
        credential_ref=body.credential_ref if body.kind == "repo" else None,
        ingest_status="pending",
    )
    db.add(resource)
    db.commit()

    from app import worker
    from app.core.queue import enqueue

    enqueue("ingestion", worker.ingest_resource_job, resource.id)
    return _resource_out(resource)


def _safe_relative(raw: str, fallback: str) -> PurePosixPath:
    rel = PurePosixPath((raw or fallback).replace("\\", "/"))
    if rel.is_absolute() or any(part in ("..", "") for part in rel.parts):
        raise HTTPException(422, f"unsafe relative path: {raw!r}")
    return rel


@router.post("/resources/upload", status_code=201)
def upload_resource(
    files: list[UploadFile] = File(...),
    # JSON array of each file's path relative to the picked folder (the
    # browser's webkitRelativePath), aligned with `files`. Empty entries
    # fall back to the plain filename.
    relative_paths: str = Form("[]"),
    title: str = Form(""),
    db: Session = Depends(get_db),
) -> ResourceOut:
    """File-picker path: the browser can't hand the backend a filesystem
    path, so picked files are uploaded into the shared uploads volume and
    registered as a `file` resource — a single file, or a whole directory
    (which ingestion expands into one taggable item per file)."""
    try:
        rels = json.loads(relative_paths)
    except json.JSONDecodeError:
        raise HTTPException(422, "relative_paths must be a JSON array") from None
    if len(rels) not in (0, len(files)):
        raise HTTPException(422, "relative_paths must align with files")

    upload_root = Path(get_settings().upload_dir)
    stamp = uuid.uuid4().hex[:8]

    def _safe_name(name: str) -> str:
        return re.sub(r"[^\w.-]+", "_", name).strip("_")

    if len(files) == 1 and "/" not in (rels[0] if rels else ""):
        rel = _safe_relative(files[0].filename or "upload", "upload")
        dest = upload_root / f"{stamp}_{rel.name}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as out:
            shutil.copyfileobj(files[0].file, out)
        locator = str(dest)
        default_title = rel.name
    else:
        # A picked folder's relative paths all share their top segment —
        # the folder's real name. Use it so the resource is recognizable
        # (".../a1b2c3d4_lecture-notes", title "lecture-notes"), not an
        # anonymous ".../a1b2c3d4_upload".
        first_segments = (
            {_safe_relative(r, f"file_{i}").parts[0] for i, r in enumerate(rels)}
            if rels
            else set()
        )
        folder_name = first_segments.pop() if len(first_segments) == 1 else ""
        display_name = title or folder_name or "upload"
        base = upload_root / f"{stamp}_{_safe_name(display_name) or 'upload'}"
        for i, upload in enumerate(files):
            rel = _safe_relative(
                rels[i] if rels else (upload.filename or f"file_{i}"), f"file_{i}"
            )
            dest = base / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as out:
                shutil.copyfileobj(upload.file, out)
        locator = str(base)
        default_title = display_name

    resource = Resource(
        kind="file",
        locator=locator,
        title=title or default_title,
        ingest_status="pending",
    )
    db.add(resource)
    db.commit()

    from app import worker
    from app.core.queue import enqueue

    enqueue("ingestion", worker.ingest_resource_job, resource.id)
    return _resource_out(resource)


@router.patch("/resources/{resource_id}")
def update_resource(
    resource_id: int, body: ResourceUpdate, db: Session = Depends(get_db)
) -> ResourceOut:
    resource = db.get(Resource, resource_id)
    if resource is None:
        raise HTTPException(404, "resource not found")
    if body.title is not None:
        resource.title = body.title
    relocated = body.locator is not None and body.locator != resource.locator
    if relocated:
        resource.locator = body.locator
        resource.ingest_status = "pending"
    db.commit()

    if relocated:
        from app import worker
        from app.core.queue import enqueue

        enqueue("ingestion", worker.ingest_resource_job, resource.id)
    return _resource_out(resource)


@router.post("/resources/{resource_id}/reingest")
def reingest_resource(resource_id: int, db: Session = Depends(get_db)) -> ResourceOut:
    resource = db.get(Resource, resource_id)
    if resource is None:
        raise HTTPException(404, "resource not found")
    resource.ingest_status = "pending"
    db.commit()

    from app import worker
    from app.core.queue import enqueue

    enqueue("ingestion", worker.ingest_resource_job, resource.id)
    return _resource_out(resource)


@router.delete("/resources/{resource_id}", status_code=204)
def delete_resource(resource_id: int, db: Session = Depends(get_db)) -> None:
    resource = db.get(Resource, resource_id)
    if resource is None:
        raise HTTPException(404, "resource not found")
    removed_tags = {t for item in resource.items for t in item.tags}
    db.delete(resource)
    db.commit()
    # Removal changes what the removed tags resolve to — dependent agents
    # get re-checked (cheap: fingerprint decides if anything actually runs).
    if removed_tags:
        resource_change_event(db, removed_tags)


@router.put("/resource-items/{item_id}/tags")
def update_item_tags(
    item_id: int, body: ItemTagsUpdate, db: Session = Depends(get_db)
) -> ResourceItemOut:
    item = db.get(ResourceItem, item_id)
    if item is None:
        raise HTTPException(404, "resource item not found")
    invalid = [t for t in body.tags if t not in TAG_VOCABULARY]
    if invalid:
        raise HTTPException(
            422, f"tags {invalid} are not in the fixed vocabulary {TAG_VOCABULARY}"
        )

    old_tags = set(item.tags)
    for row in list(item.tag_rows):
        db.delete(row)
    db.flush()
    for tag in dict.fromkeys(body.tags):  # dedupe, keep order
        db.add(ResourceItemTag(resource_item_id=item.id, tag=tag))
    if body.meta is not None:
        item.meta = body.meta
    db.commit()
    db.refresh(item)

    # Re-tagging is a resource change: agents needing any affected tag are
    # re-checked. User edits and classifier output are indistinguishable
    # downstream — by design.
    changed = old_tags | set(body.tags)
    if changed:
        resource_change_event(db, changed)

    return ResourceItemOut(
        id=item.id,
        path=item.path,
        title=item.title,
        tags=item.tags,
        meta=item.meta or {},
        content_preview=item.content_text[:300],
    )


@router.put("/resources/{resource_id}/bulk-tags")
def bulk_update_tags(
    resource_id: int, body: BulkTagsUpdate, db: Session = Depends(get_db)
) -> ResourceOut:
    """Tag a whole subtree of a multi-item resource (e.g. a repo's slides/
    folder) in one action. This is a bulk edit over the same per-item tags
    everything else uses — deliberately NOT a stored folder rule, so
    resolution semantics stay identical to individually edited tags.
    """
    resource = db.get(Resource, resource_id)
    if resource is None:
        raise HTTPException(404, "resource not found")
    invalid = [t for t in body.tags if t not in TAG_VOCABULARY]
    if invalid:
        raise HTTPException(
            422, f"tags {invalid} are not in the fixed vocabulary {TAG_VOCABULARY}"
        )

    prefix = body.path_prefix.strip("/")
    matching = [
        item
        for item in resource.items
        if not prefix or item.path == prefix or item.path.startswith(prefix + "/")
    ]
    if not matching:
        raise HTTPException(404, f"no items under path {prefix!r}")

    changed: set[str] = set()
    for item in matching:
        changed |= set(item.tags) | set(body.tags)
        for row in list(item.tag_rows):
            db.delete(row)
        db.flush()
        for tag in dict.fromkeys(body.tags):
            db.add(ResourceItemTag(resource_item_id=item.id, tag=tag))
        db.expire(item, ["tag_rows"])
    db.commit()

    if changed:
        resource_change_event(db, changed)
    db.expire(resource, ["items"])
    return _resource_out(resource)


@router.get("/resource-items/{item_id}")
def get_resource_item(item_id: int, db: Session = Depends(get_db)) -> dict:
    """Full item content — used by the UI to show a citation's source."""
    item = db.get(ResourceItem, item_id)
    if item is None:
        raise HTTPException(404, "resource item not found")
    return {
        "id": item.id,
        "resource_id": item.resource_id,
        "title": item.title,
        "path": item.path,
        "locator": item.resource.locator,
        "kind": item.resource.kind,
        "tags": item.tags,
        "meta": item.meta or {},
        "content": item.content_text,
    }
