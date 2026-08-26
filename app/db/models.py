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


#: How a run's pages arrived. `Run.source` has carried a value since the first
#: migration and until now only ever held "crawl".
#:
#: Defined here rather than in `app.jobs.tasks` so the web process can name it
#: without importing the job graph -- every other job reference in `main.py`
#: takes the same care, via a function-local import.
SOURCE_CRAWL = "crawl"
SOURCE_IMPORT = "screaming-frog"


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
    # Its own column, not a key in `plan`. It lived there first, and three code
    # paths do `run.plan = plan.to_dict()` against a `CrawlPlan` that has no such
    # field -- so approving the crawl plan silently dropped it and the run built
    # no llms-full.txt. A run option is not part of the crawl plan.
    generate_full: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
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


class ShareLink(Base):
    """A link a client can open. The token *is* the authorisation.

    There is no ownership model anywhere in this schema -- no `owner` or
    `user_id` column on any client-scoped table -- so `require_user` is the whole
    authorisation layer for staff. A client is not staff and has no session, so
    the row below has to carry the authority itself: which domain, which section,
    until when.

    That is why neither the domain nor the section appears in the share URL. If
    they did, "the handler must ignore what the request says" would be a property
    a reader has to verify rather than one the shape guarantees, and a client with
    one link could walk to another client's audit by editing the address bar.

    `token_hash` holds a SHA-256 digest, never the token. See `app.core.share`
    for why the digest is unsalted and why argon2 would be actively wrong here.
    """

    __tablename__ = "share_links"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    section: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The operator's own note -- "sent to jane@client" -- so the management list
    #: is readable. Never anything derived from the token.
    label: Mapped[str] = mapped_column(String(255), default="")
    created_by: Mapped[str] = mapped_column(String(320), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: Not nullable. There is no "never expires" option: a link with no expiry is
    #: the one that surfaces in a forwarded email in three years.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[str] = mapped_column(String(320), default="")

    #: Enough to answer "did they open it, and when did they last look". A
    #: per-view event table would answer "how many times on Tuesday", which
    #: nobody asks, and would accumulate personal data about someone who never
    #: agreed to anything. No IP, no User-Agent, no Referer -- see the route.
    first_viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    view_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (Index("ix_share_links_domain_created", "domain", "created_at"),)

    def state(self, now: datetime) -> str:
        """`live`, `revoked` or `expired`.

        Decided here rather than in the query on purpose: the handler needs to
        tell the three apart for the log while returning one identical response
        to the client. Folding it into a `WHERE` throws that away.
        """
        if self.revoked_at is not None:
            return "revoked"
        if self.expires_at <= now:
            return "expired"
        return "live"


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


class SiteSnapshot(Base):
    """The last probe of a domain's agent surfaces, so a page render costs nothing.

    Every site page used to re-probe the client's site on GET: fourteen to
    eighteen readiness requests plus roughly fourteen probe requests, blocking,
    against a thirty-second healthcheck. Clicking between the six family tabs
    re-probed six times, and a domain nobody had ever onboarded still got
    twenty-five live requests fired at it. The pages now read this row.

    One row per domain, replaced on refresh rather than appended, because the
    question these pages answer is "how is this site *now*" -- a history would be
    a different feature with a different shape, and keeping every probe forever to
    serve one of them is a cost with no reader.

    `fetched_at` is not optional and is never hidden. A cached figure presented as
    a live one is the failure this table could most easily introduce, so every
    page that renders a snapshot renders its age beside it, and a domain with no
    row says "not checked yet" rather than quietly probing.
    """

    __tablename__ = "site_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    # Serialised rather than modelled: these three are read back into the same
    # dataclasses they came from, and a column per field would have to be migrated
    # every time a probe learns to look for one more thing.
    probe: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    readiness: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )
    tech: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    fetched_by: Mapped[str] = mapped_column(String(320), default="")
    # How long the probe took. Recorded because it is the number that decides
    # whether this stays a background job or could ever go back inline.
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)


class SiteAudit(Base):
    """One audit from the LLM Access Checker, as it sent it.

    The Checker is the diagnosis and this tool is the remediation; until now they
    had never spoken, so an operator audited a client, got forty findings, and
    then opened a second tool that started from scratch and knew none of them.
    The Checker pushes each audit here as it saves it.

    **Appended, not replaced** -- the opposite of `SiteSnapshot` next door, and
    deliberately. That row answers "how is this site now" and a history would be
    a different feature. This one carries a score from a versioned rubric that
    its author refuses to trend across versions, so keeping the series is what
    lets us say "audited three days ago under v4" instead of implying the number
    has always meant the same thing.

    `payload` is the whole export, stored verbatim. The Checker builds it as a
    dict literal inside a Streamlit UI module rather than as a versioned
    contract, so a shape change there must degrade the join rather than lose the
    audit. Everything else on this row is a copy of something inside `payload`,
    lifted out only because it is queried or sorted on.

    `audit_id` is the Checker's own primary key, and unique here. A webhook that
    fires twice -- a retry, a redeploy mid-request -- must leave one row.
    """

    __tablename__ = "site_audits"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    audit_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    overall_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pillar_scores: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    # Which rubric produced `overall_score`. Stored because the Checker treats
    # scores from different versions as different measurements wearing the same
    # unit, and a number rendered without it invites exactly the comparison it
    # refuses to make.
    rubric_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # When the Checker ran it, not when we heard about it. Both, because a push
    # that arrives late is a different fact from an audit that ran late.
    audited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_site_audits_domain_audited", "domain", "audited_at"),)


class ArtifactEdit(Base):
    """Conversational refinements to a generated file, as operations.

    Operations rather than the resulting text, for the reason
    `app/llm/prompts/chat.py` gives about llms.txt and which applies harder here:
    stored text is a snapshot of a file that is otherwise a pure function of the
    evidence, so the moment a re-probe changes the evidence the stored copy is
    a claim about a site as it was. Stored operations replay onto whatever the
    generator produces next, so a refinement survives a refresh instead of
    silently going stale.

    One row per domain and component. A refine turn rewrites it whole -- the
    operations are cumulative within the row, and the row is the current state of
    "what this operator asked for", not a log. `ChatMessage` is the log.

    `facts` are operator-asserted prose the probe cannot check, kept beside the
    operations because they are the same kind of thing: something a person said,
    which has to render attributed and survive a regeneration.
    """

    __tablename__ = "artifact_edits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    component_key: Mapped[str] = mapped_column(String(64), nullable=False)
    operations: Mapped[list] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )
    facts: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    # The turns that produced this, newest last, capped. `ChatMessage` is
    # run-scoped and a refinement is domain-scoped, so it cannot be reused here.
    # Kept beside the operations rather than in its own table because a
    # conversation with no surviving edit is still worth showing -- a refused
    # turn is exactly what an operator needs to see when they wonder why nothing
    # changed.
    messages: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    edited_by: Mapped[str] = mapped_column(String(320), default="")
    edited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("domain", "component_key", name="uq_artifact_edits_domain_component"),
    )


class LlmSpend(Base):
    """One interactive LLM call, so it reaches the costs page.

    The pipeline's spend has always been recorded, onto `Run.stats["llm"]` by
    `app/jobs/tasks.py`. Interactive spend never was. `chat_edit` built an
    `LLMClient` with no usage object at all; `refine_turn` and `suggest_brief`
    built one, passed it, and then never read it -- so the `LLMUsage` filled up
    and was garbage-collected with the request.

    `cost_of` reads `Run.stats` and nothing else, which means /admin did not
    report these as unpriced. It reported them as **not having happened**, while
    they ran on `llm_model_chat`, defaulting to `gpt-4o` and the most expensive
    model in the rate table. The costs page's own rule -- "a model with no rate
    is not free, it is unknown" -- was being broken one layer above where it is
    enforced.

    A table rather than a JSONB column on some existing row, because interactive
    spend is not a property of a run or of an edit: `suggest_brief` happens before
    any run exists, and a refine turn belongs to a domain. One row per call keeps
    it auditable and lets the ceiling be a cheap COUNT.

    Tokens are stored and dollars are not. `pricing.py` converts at read time, so
    correcting a rate reprices history rather than leaving old rows wrong -- the
    same reason `Run.stats` stores tokens.
    """

    __tablename__ = "llm_spend"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Which client the spend was on behalf of. Nullable because `suggest_brief`
    # can run against a domain nobody has onboarded yet.
    domain: Mapped[str] = mapped_column(String(255), default="", index=True)
    # Set where the call belongs to a run, so pipeline and interactive spend can
    # be reconciled against `Run.stats` rather than double-counted.
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )
    stage: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    model: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    calls: Mapped[int] = mapped_column(Integer, default=0)
    # Why a call fell back, when it did. A refusal costs nothing and still needs
    # to be visible: a silent fallback looks the same as a success on a bill.
    fallbacks: Mapped[list] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )
    spent_by: Mapped[str] = mapped_column(String(320), default="")
    spent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
