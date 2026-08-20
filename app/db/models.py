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
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    Date,
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
    # Prose block under the blockquote. The spec allows any markdown except
    # headings there; it is where disambiguation an agent would otherwise get
    # wrong belongs.
    notes: Mapped[str] = mapped_column(Text, default="")
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
    # The onboarding answers, in their own column rather than inside `plan`.
    # `save_site_config` replaces `plan` wholesale with `CrawlPlan.to_dict()`, and
    # `CrawlPlan.from_dict` keeps only the keys it knows, so a brief stored there
    # would be erased by the next plan approval without anything reporting it.
    brief: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    # URL count per sitemap group as of the last preflight. Machine-owned, and
    # kept apart from `brief["shape"]`, which records what the site looked like
    # when a person answered. Drift is the difference; one writer each is what
    # keeps that difference meaningful.
    observed_shape: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )
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


class User(Base):
    """An account that can sign in.

    Replicates geo-tracker's local-mode rule: exactly one self-service signup,
    after which the instance is closed and further accounts are created by an
    existing operator. That is what makes a public Railway domain safe without an
    identity provider -- the window in which a stranger could claim the instance
    closes the moment the first real user registers.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    # Null for an account that signs in through Google rather than a password.
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    # True for the bootstrap account and anyone it promotes. Only an admin can
    # create further accounts.
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[str] = mapped_column(String(320), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChatMessage(Base):
    """One turn of the editing conversation.

    Kept because a client deliverable that was edited by a model should be able to
    answer "who asked for what, and what did it do" months later. The operations
    are stored alongside the prose so the answer is specific.
    """

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | assistant
    body: Mapped[str] = mapped_column(Text, default="")
    # What the turn actually did, and what it refused to do.
    operations: Mapped[list] = mapped_column(JSONB, default=list)
    rejected: Mapped[list] = mapped_column(JSONB, default=list)
    author: Mapped[str] = mapped_column(String(320), default="")
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_chat_messages_run_at", "run_id", "at"),)


class DocumentRevision(Base):
    """The rendered file as it stood before an edit, so undo is real.

    Stored per edit rather than per run: the point of keeping them is to be able to
    go back one step after a chat turn did something unwanted, which is the failure
    mode a conversational editor actually has.
    """

    __tablename__ = "document_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    llmstxt: Mapped[str] = mapped_column(Text, default="")
    llms_full: Mapped[str] = mapped_column(Text, default="")
    # Snapshot of the per-page state, so a revert restores assignments and copy and
    # not merely the rendered text -- the text is downstream of the model.
    pages: Mapped[dict] = mapped_column(JSONB, default=dict)
    site_name: Mapped[str] = mapped_column(String(255), default="")
    site_summary: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(String(255), default="")
    author: Mapped[str] = mapped_column(String(320), default="")
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_document_revisions_run_at", "run_id", "at"),)


class ComponentMark(Base):
    """A person's assertion that a component is done.

    Only for the six the tool cannot check: layout shift, cursor styles, tap
    targets, ghost overlays, WebMCP and Web Bot Auth. Everything else is decided
    by the probe on every request and stored nowhere, because a remembered tick
    is a claim about the site as it was rather than as it is.

    `noted_by` is not decoration. This is a client-facing status, and "someone
    said this was done in August" needs a name attached to it when a client asks
    who, particularly for the accessibility items where the answer changes with
    every theme update.
    """

    __tablename__ = "component_marks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    component_key: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str] = mapped_column(Text, default="")
    noted_by: Mapped[str] = mapped_column(String(320), default="")
    noted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("domain", "component_key", name="uq_component_marks_domain_key"),
    )


class SiteMetric(Base):
    """Per-URL search metrics for a domain, from an upload or from the API.

    Its own table, not a JSONB blob hanging off `site_configs`. Two reasons, and
    the second is the one that bites: a domain can carry tens of thousands of
    rows, and human-supplied state must never live inside a column that a machine
    replaces wholesale -- `save_site_config` overwrites `plan` outright, so
    anything nested there is one plan approval away from being gone.

    Keyed on the canonical URL, so tracking-tagged variants have already been
    merged before anything is written. Storing them separately would recreate the
    split this layer exists to remove.
    """

    __tablename__ = "site_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    clicks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    impressions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ctr: Mapped[float | None] = mapped_column(Float, nullable=True)
    position: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Which adapter produced this, so a later API pull can be told apart from an
    # operator's upload and the two are never silently averaged.
    source: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    # The window the figures cover. Recorded rather than enforced: two runs are
    # only comparable over the same range, and without this nobody can tell.
    window_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    window_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    uploaded_by: Mapped[str] = mapped_column(String(320), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # One row per URL per source: re-uploading replaces rather than accumulates,
        # which is what an operator correcting a bad export expects.
        UniqueConstraint("domain", "url", "source", name="uq_site_metrics_domain_url_source"),
    )
