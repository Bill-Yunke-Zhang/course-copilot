from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Resource(Base):
    """A thing the user pointed the app at: a local file, a URL, or a repo.

    A resource is the unit the user adds/removes in the Resource List. The
    unit agents actually consume is the ResourceItem — for `file`/`url`
    resources there is exactly one item; a `repo` resource expands into one
    item per file/subfolder at ingestion time.
    """

    __tablename__ = "resources"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # file | url | repo
    # File path, URL, or repo clone URL depending on kind.
    locator: Mapped[str] = mapped_column(String(1024))
    title: Mapped[str] = mapped_column(String(255), default="")
    # Set when this resource was auto-discovered by crawling another resource
    # (e.g. a "Read" link found on the schedule page). Shown in the UI so the
    # user can tell direct adds from crawled ones and prune the irrelevant.
    discovered_from_id: Mapped[int | None] = mapped_column(
        ForeignKey("resources.id", ondelete="SET NULL"), nullable=True
    )
    # Name of the SecretStore credential needed to fetch this resource
    # (repos only). A single "gitlab-pat" covers all course repos.
    credential_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # pending | ingesting | ready | error
    ingest_status: Mapped[str] = mapped_column(String(16), default="pending")
    ingest_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    items: Mapped[list["ResourceItem"]] = relationship(
        back_populates="resource", cascade="all, delete-orphan"
    )
    discovered_from: Mapped["Resource | None"] = relationship(remote_side=[id])


class ResourceItem(Base):
    """The taggable, citable unit of content that agents consume.

    `source_ref`s in agent output point at ResourceItem ids. Tags live in
    ResourceItemTag rows; `tags` is the convenience accessor.
    """

    __tablename__ = "resource_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    resource_id: Mapped[int] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE")
    )
    # Sub-path within a repo resource; empty string for file/url resources.
    path: Mapped[str] = mapped_column(String(512), default="")
    title: Mapped[str] = mapped_column(String(255), default="")
    # Extracted text content (markdown/plaintext). For `project-code` items
    # this stays empty — agents work from `meta` (git evidence), not prose.
    content_text: Mapped[str] = mapped_column(
        Text().with_variant(Text(2**24), "mysql"), default=""
    )
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    # Classifier-extracted, user-editable metadata (lecture number, date, …).
    # Same principle as tags: the classifier's output is a suggestion.
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    resource: Mapped[Resource] = relationship(back_populates="items")
    tag_rows: Mapped[list["ResourceItemTag"]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )

    @property
    def tags(self) -> list[str]:
        return sorted(t.tag for t in self.tag_rows)


class ResourceItemTag(Base):
    """One tag on one resource item, drawn from the fixed vocabulary.

    Whether a tag was assigned by the classifier or edited in by the user is
    deliberately NOT recorded — resolution must treat both identically.
    """

    __tablename__ = "resource_item_tags"
    __table_args__ = (UniqueConstraint("resource_item_id", "tag"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    resource_item_id: Mapped[int] = mapped_column(
        ForeignKey("resource_items.id", ondelete="CASCADE")
    )
    tag: Mapped[str] = mapped_column(String(32), index=True)

    item: Mapped[ResourceItem] = relationship(back_populates="tag_rows")
