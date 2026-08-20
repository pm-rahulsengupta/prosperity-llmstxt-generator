"""Tables.

The source tool kept results in `_results_store`, a module-level dict. It was lost
on every restart and wrong the moment a second worker existed, which on a platform
that restarts containers for a variable change is most of the time. Everything that
survives a run lives here instead.

Two shapes are worth pointing out:

* `Section` is a table, not a derived value. The source re-ran URL-path grouping on
  every rebuild, so any edit a user made -- a renamed section, a moved page -- was
  discarded the next time the file was assembled, and an LLM section assignment
  never survived at all.
* `RunEvent` is an append-only log rather than a mutable progress field, so the UI
  can show what a long crawl has been doing rather than only where it got to.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.models import PageEntry
from app.db.base import Base


class RunStatus(StrEnum):
    PENDING = "pending"
    PREFLIGHT = "preflight"
    AWAITING_REVIEW = "awaiting_review"  # the human gate, before any crawl spend
    CRAWLING = "crawling"
    TRIAGING = "triaging"
    SUMMARISING = "summarising"
    ASSEMBLING = "assembling"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {RunStatus.COMPLETE, RunStatus.FAILED, RunStatus.CANCELLED}


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    site_url: Mapped[str] = mapped_column(String(512), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=RunStatus.PENDING)
    created_by: Mapped[str] = mapped_column(String(320), nullable=False, default="")

    # Source of the page inventory: "crawl" or "screaming_frog".
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="crawl")

    # -- pre-flight ---------------------------------------------------------
    sitemap_total: Mapped[int] = mapped_column(Integer, default=0)
    sitemap_html: Mapped[int] = mapped_column(Integer, default=0)
    indexed_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    size_tier: Mapped[str] = mapped_column(String(16), default="unknown")
    size_warnings: Mapped[list] = mapped_column(JSONB, default=list)
    max_pages: Mapped[int] = mapped_column(Integer, default=0)

    # The crawl plan, as reviewed and possibly edited by a human. Stored whole so
    # the exact plan a run used is recoverable months later.
    plan: Mapped[dict] = mapped_column(JSONB, default=dict)
    plan_source: Mapped[str] = mapped_column(
        String(16), default="heuristic"
    )  # llm | heuristic | manual

    # -- output -------------------------------------------------------------
    site_name: Mapped[str] = mapped_column(String(255), default="")
    site_summary: Mapped[str] = mapped_column(Text, default="")
    pattern: Mapped[str] = mapped_column(String(32), default="")
    llmstxt: Mapped[str] = mapped_column(Text, default="")
    llms_full: Mapped[str] = mapped_column(Text, default="")
    issues: Mapped[list] = mapped_column(JSONB, default=list)

    # -- accounting ---------------------------------------------------------
    # What the run cost, per tier and per LLM stage. The source could not answer
    # "why was that slow" or "what did that cost" at all.
    stats: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    pages: Mapped[list[Page]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="selectin"
    )
    sections: Mapped[list[SectionRow]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="selectin"
    )
    events: Mapped[list[RunEvent]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="noload"
    )

    __table_args__ = (Index("ix_runs_domain_created", "domain", "created_at"),)


class SectionRow(Base):
    """A section of the generated file, as it stands after any human edits."""

    __tablename__ = "sections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(Integer, default=0)

    run: Mapped[Run] = relationship(back_populates="sections")

    __table_args__ = (UniqueConstraint("run_id", "name", name="uq_sections_run_name"),)


class Page(Base):
    """One candidate page. Mirrors `app.core.models.PageEntry` field for field.

    Deliberately wide rather than a JSON blob: `link_score` and `word_count` being
    quietly dropped between stages is the exact defect this port exists to fix, and
    a column that must be named cannot be dropped by accident.
    """

    __tablename__ = "pages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )

    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    title: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    h1: Mapped[str] = mapped_column(Text, default="")

    word_count: Mapped[int] = mapped_column(Integer, default=0)
    text_ratio: Mapped[float] = mapped_column(Float, default=0.0)

    crawl_depth: Mapped[int] = mapped_column(Integer, default=-1)
    folder_depth: Mapped[int] = mapped_column(Integer, default=0)
    inlinks: Mapped[int] = mapped_column(Integer, default=0)
    unique_inlinks: Mapped[int] = mapped_column(Integer, default=0)
    outlinks: Mapped[int] = mapped_column(Integer, default=0)
    external_outlinks: Mapped[int] = mapped_column(Integer, default=0)
    link_score: Mapped[int] = mapped_column(Integer, default=0)

    content_hash: Mapped[str] = mapped_column(String(64), default="")
    canonical: Mapped[str] = mapped_column(String(2048), default="")
    closest_similarity: Mapped[float] = mapped_column(Float, default=0.0)

    markdown: Mapped[str] = mapped_column(Text, default="")
    status_code: Mapped[int] = mapped_column(Integer, default=0)
    fetch_tier: Mapped[str] = mapped_column(String(16), default="")
    response_time: Mapped[float] = mapped_column(Float, default=0.0)

    importance: Mapped[float] = mapped_column(Float, default=0.0)
    section_name: Mapped[str] = mapped_column(String(255), default="")
    is_optional: Mapped[bool] = mapped_column(Boolean, default=False)
    # Set false by a user unticking a page. Excluded pages are kept, not deleted,
    # so a change of mind does not mean re-crawling.
    included: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0)

    run: Mapped[Run] = relationship(back_populates="pages")

    __table_args__ = (
        UniqueConstraint("run_id", "url", name="uq_pages_run_url"),
        Index("ix_pages_run_section", "run_id", "section_name"),
    )

    def to_entry(self) -> PageEntry:
        return PageEntry(
            url=self.url,
            title=self.title,
            description=self.description,
            h1=self.h1,
            word_count=self.word_count,
            text_ratio=self.text_ratio,
            crawl_depth=self.crawl_depth,
            folder_depth=self.folder_depth,
            inlinks=self.inlinks,
            unique_inlinks=self.unique_inlinks,
            outlinks=self.outlinks,
            external_outlinks=self.external_outlinks,
            link_score=self.link_score,
            content_hash=self.content_hash,
            canonical=self.canonical,
            closest_similarity=self.closest_similarity,
            markdown=self.markdown,
            status_code=self.status_code,
            fetch_tier=self.fetch_tier,
            response_time=self.response_time,
            section=self.section_name,
            is_optional=self.is_optional,
            index=self.position,
        )

    @classmethod
    def from_entry(cls, run_id: uuid.UUID, entry: PageEntry, importance: float = 0.0) -> Page:
        return cls(
            run_id=run_id,
            url=entry.url,
            title=entry.title,
            description=entry.description,
            h1=entry.h1,
            word_count=entry.word_count,
            text_ratio=entry.text_ratio,
            crawl_depth=entry.crawl_depth,
            folder_depth=entry.folder_depth,
            inlinks=entry.inlinks,
            unique_inlinks=entry.unique_inlinks,
            outlinks=entry.outlinks,
            external_outlinks=entry.external_outlinks,
            link_score=entry.link_score,
            content_hash=entry.content_hash,
            canonical=entry.canonical,
            closest_similarity=entry.closest_similarity,
            markdown=entry.markdown,
            status_code=entry.status_code,
            fetch_tier=entry.fetch_tier,
            response_time=entry.response_time,
            importance=importance,
            section_name=entry.section,
            is_optional=entry.is_optional,
            position=entry.index,
        )


class RunEvent(Base):
    """Append-only progress log. What the HTMX poller reads."""

    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, default="")
    done: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    run: Mapped[Run] = relationship(back_populates="events")

    __table_args__ = (Index("ix_run_events_run_at", "run_id", "at"),)


class SiteConfig(Base):
    """Per-domain saved settings, so a monthly regeneration is one click.

    The source had `save_crawl_config` writing to Snowflake and `load_crawl_config`
    dead on the Flask path, so the UI could save a configuration and never load one
    back. That is fixed by making the loader the only way a run gets its defaults.
    """

    __tablename__ = "site_configs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    label: Mapped[str] = mapped_column(String(255), default="")
    plan: Mapped[dict] = mapped_column(JSONB, default=dict)
    max_pages: Mapped[int] = mapped_column(Integer, default=0)
    # Cached `site:` count, with its date, so a monthly rerun does not buy the same
    # SERP call again for no new information.
    indexed_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    indexed_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_by: Mapped[str] = mapped_column(String(320), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
