"""Every database read and write the app performs.

Kept in one module so the routes and the jobs cannot each invent their own version
of "save the pages". The source had three drifted copies of the same pipeline; this
is the cheapest structural guard against that happening again.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

from sqlalchemy import delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.metrics import DateRange, PageMetrics
from app.core.models import GenerationResult, PageEntry
from app.core.onboarding import SiteBrief, matches_any
from app.db.models import (
    ComponentMark,
    DocumentRevision,
    Page,
    Run,
    RunEvent,
    RunStatus,
    SectionRow,
    SiteConfig,
    SiteMetric,
)


def domain_of(site_url: str) -> str:
    host = urlparse(site_url if "//" in site_url else f"https://{site_url}").netloc
    return (host or site_url).removeprefix("www.").lower()


# -- runs -------------------------------------------------------------------


async def create_run(
    session: AsyncSession,
    site_url: str,
    created_by: str,
    source: str = "crawl",
) -> Run:
    run = Run(
        site_url=site_url,
        domain=domain_of(site_url),
        created_by=created_by,
        source=source,
        status=RunStatus.PENDING,
    )
    session.add(run)
    await session.flush()
    return run


async def get_run(session: AsyncSession, run_id: uuid.UUID) -> Run | None:
    return await session.get(Run, run_id)


async def list_runs(session: AsyncSession, limit: int = 50) -> list[Run]:
    result = await session.execute(select(Run).order_by(desc(Run.created_at)).limit(limit))
    return list(result.scalars())


async def set_status(session: AsyncSession, run: Run, status: RunStatus, error: str = "") -> None:
    run.status = status
    if error:
        run.error = error
    if status.is_terminal:
        run.finished_at = datetime.now(UTC)


async def record_event(
    session: AsyncSession,
    run_id: uuid.UUID,
    stage: str,
    message: str = "",
    done: int = 0,
    total: int = 0,
) -> None:
    session.add(RunEvent(run_id=run_id, stage=stage, message=message, done=done, total=total))


async def latest_event(session: AsyncSession, run_id: uuid.UUID) -> RunEvent | None:
    result = await session.execute(
        select(RunEvent)
        .where(RunEvent.run_id == run_id)
        .order_by(desc(RunEvent.at), desc(RunEvent.id))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def recent_events(
    session: AsyncSession, run_id: uuid.UUID, limit: int = 25
) -> list[RunEvent]:
    result = await session.execute(
        select(RunEvent)
        .where(RunEvent.run_id == run_id)
        .order_by(desc(RunEvent.at), desc(RunEvent.id))
        .limit(limit)
    )
    return list(reversed(list(result.scalars())))


# -- pages and sections -----------------------------------------------------


async def replace_pages(
    session: AsyncSession,
    run_id: uuid.UUID,
    entries: list[PageEntry],
    importances: dict[str, float] | None = None,
) -> None:
    """Write the page inventory, replacing whatever was there.

    Used by the crawl and CSV-import stages, which own the inventory. Later stages
    update rows in place -- they must never call this, or a user's include/exclude
    edits would be silently reset.
    """
    await session.execute(delete(Page).where(Page.run_id == run_id))
    scores = importances or {}
    session.add_all(
        Page.from_entry(run_id, entry, importance=scores.get(entry.url, 0.0)) for entry in entries
    )
    await session.flush()


async def get_pages(
    session: AsyncSession, run_id: uuid.UUID, included_only: bool = False
) -> list[Page]:
    query = select(Page).where(Page.run_id == run_id)
    if included_only:
        query = query.where(Page.included.is_(True))
    result = await session.execute(query.order_by(Page.position, Page.url))
    return list(result.scalars())


async def save_sections(
    session: AsyncSession, run_id: uuid.UUID, names: list[tuple[str, str]]
) -> None:
    """Persist the section list and its order.

    Sections are stored, not recomputed. `_rebuild_llmstxt` in the source re-derived
    them from URL paths every time, which threw away both the LLM's assignments and
    anything the user had renamed or reordered.
    """
    await session.execute(delete(SectionRow).where(SectionRow.run_id == run_id))
    session.add_all(
        SectionRow(run_id=run_id, name=name, description=description, position=position)
        for position, (name, description) in enumerate(names)
    )
    await session.flush()


async def get_sections(session: AsyncSession, run_id: uuid.UUID) -> list[SectionRow]:
    result = await session.execute(
        select(SectionRow).where(SectionRow.run_id == run_id).order_by(SectionRow.position)
    )
    return list(result.scalars())


async def store_result(session: AsyncSession, run: Run, result: GenerationResult) -> None:
    """Write an assembled result back onto the run, sections included."""
    run.site_name = result.site_name
    run.site_summary = result.site_summary
    run.pattern = result.pattern
    run.llmstxt = result.llmstxt
    run.llms_full = result.llms_full
    run.issues = [
        {"level": issue.level, "message": issue.message, "code": issue.code}
        for issue in result.issues
    ]
    await save_sections(
        session, run.id, [(section.name, section.description) for section in result.sections]
    )

    # Write the copy back too, not just the placement. `replace_pages` stores the
    # crawl's raw metadata *before* the summarise stage runs, so without this the
    # rendered file carries the model's titles and descriptions while the database
    # still carries the page's own meta description -- and the next re-render from
    # rows silently reverts every line. That is the source tool's `_rebuild_llmstxt`
    # defect exactly, one layer down.
    by_url = {page.url: page for page in await get_pages(session, run.id)}
    for position, section in enumerate(result.sections):
        for order, entry in enumerate(section.pages):
            if (row := by_url.get(entry.url)) is not None:
                row.title = entry.title or row.title
                row.description = entry.description or row.description
                row.section_name = section.name
                row.is_optional = False
                row.position = position * 1_000 + order
    for entry in result.optional:
        if (row := by_url.get(entry.url)) is not None:
            row.title = entry.title or row.title
            row.description = entry.description or row.description
            row.is_optional = True
            row.section_name = ""


# -- per-domain configuration ----------------------------------------------


async def load_site_config(session: AsyncSession, domain: str) -> SiteConfig | None:
    result = await session.execute(select(SiteConfig).where(SiteConfig.domain == domain))
    return result.scalar_one_or_none()


async def save_site_config(
    session: AsyncSession,
    domain: str,
    plan: dict,
    max_pages: int,
    updated_by: str,
    label: str = "",
) -> SiteConfig:
    config = await load_site_config(session, domain)
    if config is None:
        config = SiteConfig(domain=domain)
        session.add(config)
    config.plan = plan
    config.max_pages = max_pages
    config.updated_by = updated_by
    if label:
        config.label = label
    await session.flush()
    return config


async def load_brief(session: AsyncSession, domain: str) -> SiteBrief:
    """Never returns None. An unanswered brief is an empty one, not a missing one.

    Every caller would otherwise need the same `or SiteBrief()`, and the one that
    forgot would crash a run on a domain nobody has onboarded yet.
    """
    config = await load_site_config(session, domain)
    return SiteBrief.from_dict(config.brief if config else None)


async def save_brief(session: AsyncSession, domain: str, brief: SiteBrief) -> None:
    """Written separately from the plan, because the two have different lifetimes.

    A plan is approved per run; a brief is answered once and re-confirmed only
    when the site's shape moves.
    """
    config = await load_site_config(session, domain)
    if config is None:
        config = SiteConfig(domain=domain)
        session.add(config)
    config.brief = brief.to_dict()
    await session.flush()


async def replace_site_metrics(
    session: AsyncSession,
    domain: str,
    metrics: dict[str, PageMetrics],
    source: str,
    uploaded_by: str = "",
) -> int:
    """Replace this domain's metrics from one source, leaving other sources alone.

    Replace rather than merge: an operator re-uploading has almost always fixed a
    bad export, and merging would leave the rows they were trying to correct in
    place with no way to see them. Scoped to `source` so an upload does not delete
    what the API pulled, and the reverse.
    """
    await session.execute(
        delete(SiteMetric).where(SiteMetric.domain == domain, SiteMetric.source == source)
    )
    session.add_all(
        [
            SiteMetric(
                domain=domain,
                url=url,
                clicks=m.clicks,
                impressions=m.impressions,
                ctr=m.ctr,
                position=m.position,
                source=source,
                window_start=m.window.start if m.window else None,
                window_end=m.window.end if m.window else None,
                uploaded_by=uploaded_by,
            )
            for url, m in metrics.items()
        ]
    )
    await session.flush()
    return len(metrics)


async def load_site_metrics(session: AsyncSession, domain: str) -> dict[str, PageMetrics]:
    """Everything known about a domain's URLs, best source per URL.

    When two sources cover the same URL the one with more clicks wins, which in
    practice means the longer window. Averaging them would invent a number that
    no source reported and that no window explains.
    """
    result = await session.execute(select(SiteMetric).where(SiteMetric.domain == domain))
    best: dict[str, PageMetrics] = {}
    for row in result.scalars():
        candidate = PageMetrics(
            url=row.url,
            clicks=row.clicks,
            impressions=row.impressions,
            ctr=row.ctr,
            position=row.position,
            source=row.source,
            window=(
                DateRange(row.window_start, row.window_end)
                if row.window_start and row.window_end
                else None
            ),
        )
        incumbent = best.get(row.url)
        if incumbent is None or (candidate.clicks or 0) > (incumbent.clicks or 0):
            best[row.url] = candidate
    return best


async def metrics_summary(session: AsyncSession, domain: str) -> dict:
    """What the UI shows about a domain's metrics without loading them all."""
    result = await session.execute(
        select(
            SiteMetric.source,
            func.count(SiteMetric.id),
            func.sum(SiteMetric.clicks),
            func.min(SiteMetric.window_start),
            func.max(SiteMetric.window_end),
            func.max(SiteMetric.created_at),
        )
        .where(SiteMetric.domain == domain)
        .group_by(SiteMetric.source)
    )
    return {
        source: {
            "urls": urls,
            "clicks": clicks or 0,
            "window_start": start,
            "window_end": end,
            "at": at,
        }
        for source, urls, clicks, start, end, at in result.all()
    }


@dataclass(frozen=True, slots=True)
class PurgeReport:
    """Exactly what an embargo removed, so it can be checked and explained."""

    pages: int = 0
    metric_rows: int = 0
    index_lines: int = 0
    full_documents: int = 0
    revisions: int = 0
    urls: tuple[str, ...] = ()

    @property
    def anything(self) -> bool:
        return bool(
            self.pages
            or self.metric_rows
            or self.index_lines
            or self.full_documents
            or self.revisions
        )

    def summary(self) -> str:
        if not self.anything:
            return "Nothing matched the embargo; nothing was removed."
        return (
            f"Embargo removed {self.pages} stored page(s), {self.metric_rows} metric row(s), "
            f"{self.index_lines} line(s) from rendered index files, and blanked "
            f"{self.full_documents} full-text document(s) across {self.revisions} revision(s)."
        )


async def purge_embargoed(
    session: AsyncSession, domain: str, patterns: tuple[str, ...]
) -> PurgeReport:
    """Remove everything an embargoed URL left behind, not just the page row.

    Declaring an embargo has to be retroactive -- an operator adding one is
    normally reacting to something already crawled -- and it has to reach the
    derived artifacts, which is where the first version fell short. A page body
    deleted from `pages` while the same content sits in a rendered
    `llms-full.txt` that is still downloadable is not an embargo, it is a change
    of filing.

    Four places hold it:

    * `pages` -- the crawled body and metadata.
    * `site_metrics` -- rows keyed by the URL. Less sensitive, but an embargoed
      path should not be inferable from a metrics table either.
    * `runs.llmstxt` and every `document_revisions.llmstxt` -- link lines are
      removed individually, which is safe because the format is one line per page.
    * `runs.llms_full` and its revisions -- **blanked entirely**, not edited.
      That file is concatenated page bodies with no reliable per-page boundary to
      cut on, and a partial removal that cannot be verified is worse than an
      empty field. The run is left needing a re-render, which is recoverable.

    Recovery from a mistyped pattern is by re-running: remove the pattern from
    the brief and re-run the site. Nothing here is unrecoverable *from the
    source*, because the source is the client's own live website -- which is
    exactly why deleting our copy is safe to do eagerly.
    """
    if not patterns:
        return PurgeReport()

    result = await session.execute(
        select(Page.id, Page.url).join(Run, Page.run_id == Run.id).where(Run.domain == domain)
    )
    doomed = [(pid, url) for pid, url in result.all() if matches_any(url, patterns) is not None]
    doomed_urls = sorted({url for _, url in doomed})
    if doomed:
        await session.execute(delete(Page).where(Page.id.in_([pid for pid, _ in doomed])))

    metrics_result = await session.execute(
        select(SiteMetric.id, SiteMetric.url).where(SiteMetric.domain == domain)
    )
    doomed_metrics = [
        mid for mid, url in metrics_result.all() if matches_any(url, patterns) is not None
    ]
    if doomed_metrics:
        await session.execute(delete(SiteMetric).where(SiteMetric.id.in_(doomed_metrics)))

    index_lines = 0
    full_docs = 0
    revisions_touched = 0

    runs = (await session.execute(select(Run).where(Run.domain == domain))).scalars().all()
    for run in runs:
        cleaned, removed = _strip_embargoed_lines(run.llmstxt, patterns)
        if removed:
            run.llmstxt = cleaned
            index_lines += removed
        if run.llms_full and _mentions_embargoed(run.llms_full, patterns):
            run.llms_full = ""
            full_docs += 1

    run_ids = [run.id for run in runs]
    if run_ids:
        revs = (
            (
                await session.execute(
                    select(DocumentRevision).where(DocumentRevision.run_id.in_(run_ids))
                )
            )
            .scalars()
            .all()
        )
        for rev in revs:
            touched = False
            cleaned, removed = _strip_embargoed_lines(rev.llmstxt, patterns)
            if removed:
                rev.llmstxt = cleaned
                index_lines += removed
                touched = True
            if rev.llms_full and _mentions_embargoed(rev.llms_full, patterns):
                rev.llms_full = ""
                full_docs += 1
                touched = True
            if touched:
                revisions_touched += 1

    await session.flush()
    return PurgeReport(
        pages=len(doomed),
        metric_rows=len(doomed_metrics),
        index_lines=index_lines,
        full_documents=full_docs,
        revisions=revisions_touched,
        urls=tuple(doomed_urls[:50]),
    )


def _strip_embargoed_lines(text: str, patterns: tuple[str, ...]) -> tuple[str, int]:
    """Drop link lines naming an embargoed URL. Line-oriented and therefore safe."""
    if not text:
        return text, 0
    kept, removed = [], 0
    for line in text.splitlines():
        if _mentions_embargoed(line, patterns):
            removed += 1
            continue
        kept.append(line)
    if not removed:
        return text, 0
    return "\n".join(kept) + ("\n" if text.endswith("\n") else ""), removed


_URL_IN_TEXT = re.compile(r"https?://[^\s<>\")\]]+")


def _mentions_embargoed(text: str, patterns: tuple[str, ...]) -> bool:
    return any(matches_any(url, patterns) is not None for url in _URL_IN_TEXT.findall(text))


async def is_cancelled(session: AsyncSession, run_id: uuid.UUID) -> bool:
    """Has someone asked this run to stop?

    Read as a bare column rather than through `get_run`, because it is checked at
    every stage boundary and pulling the pages relationship each time to answer a
    boolean would be the most expensive question in the pipeline.
    """
    result = await session.execute(select(Run.status).where(Run.id == run_id))
    return result.scalar_one_or_none() == RunStatus.CANCELLED


async def delete_run(session: AsyncSession, run_id: uuid.UUID) -> bool:
    """Remove a run and everything hanging off it. Irreversible.

    Deliberately a plain delete with no soft-delete flag. A half-deleted run that
    still appears in cost totals and group rollups is worse than either outcome,
    and the thing an operator wants gone is usually the stored page bodies rather
    than the row.

    Cascades are declared on the foreign keys, so pages, sections, events and
    document revisions go with it.
    """
    run = await session.get(Run, run_id)
    if run is None:
        return False
    await session.delete(run)
    await session.flush()
    return True


async def clone_run(session: AsyncSession, run_id: uuid.UUID, created_by: str) -> Run | None:
    """Start a fresh run against the same site.

    A new row rather than a reset of the old one. Re-running is nearly always a
    comparison -- did the fix change the output -- and resetting in place destroys
    the artefact being compared against. It also keeps a failed run's events
    readable while its replacement runs, which is exactly when they are wanted.
    """
    original = await session.get(Run, run_id)
    if original is None:
        return None
    fresh = Run(
        site_url=original.site_url,
        domain=original.domain,
        created_by=created_by,
        source=original.source,
        # The cap is carried because it is usually a deliberate choice; the plan
        # is not, because re-running exists to get a new one.
        max_pages=original.max_pages,
    )
    session.add(fresh)
    await session.flush()
    return fresh


async def latest_complete_run(session: AsyncSession, domain: str) -> Run | None:
    """The most recent finished run for a domain, if there is one.

    agents.md and llms.txt describe the same site, so the pages one already found
    are the pages the other should link to. Re-crawling to answer "does this site
    have a privacy policy" when a completed crawl already knows would be paying
    twice for the same fact.
    """
    result = await session.execute(
        select(Run)
        .where(Run.domain == domain, Run.status == RunStatus.COMPLETE)
        .order_by(desc(Run.created_at))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def load_marks(session: AsyncSession, domain: str) -> dict[str, str]:
    """Component key -> who marked it done."""
    result = await session.execute(
        select(ComponentMark.component_key, ComponentMark.noted_by).where(
            ComponentMark.domain == domain
        )
    )
    return {key: (who or "someone") for key, who in result.all()}


async def set_mark(
    session: AsyncSession, domain: str, component_key: str, noted_by: str, note: str = ""
) -> None:
    existing = await session.execute(
        select(ComponentMark).where(
            ComponentMark.domain == domain, ComponentMark.component_key == component_key
        )
    )
    mark = existing.scalar_one_or_none()
    if mark is None:
        session.add(
            ComponentMark(domain=domain, component_key=component_key, noted_by=noted_by, note=note)
        )
    else:
        # Re-marking updates who and when. An assertion about an accessibility
        # item goes stale with the next theme change, so the date is the useful
        # part and overwriting it is the point.
        mark.noted_by = noted_by
        mark.note = note
        mark.noted_at = datetime.now(UTC)
    await session.flush()


async def clear_mark(session: AsyncSession, domain: str, component_key: str) -> None:
    await session.execute(
        delete(ComponentMark).where(
            ComponentMark.domain == domain, ComponentMark.component_key == component_key
        )
    )
    await session.flush()


async def record_observed_shape(session: AsyncSession, domain: str, shape: dict[str, int]) -> None:
    """Write what preflight just measured. Machine-owned; never the brief's copy."""
    config = await load_site_config(session, domain)
    if config is None:
        config = SiteConfig(domain=domain)
        session.add(config)
    config.observed_shape = dict(shape)
    await session.flush()


async def load_observed_shape(session: AsyncSession, domain: str) -> dict[str, int]:
    config = await load_site_config(session, domain)
    return dict(config.observed_shape) if config else {}


async def cache_indexed_estimate(session: AsyncSession, domain: str, estimate: int | None) -> None:
    if estimate is None:
        return
    config = await load_site_config(session, domain)
    if config is None:
        config = SiteConfig(domain=domain)
        session.add(config)
    config.indexed_estimate = estimate
    config.indexed_checked_at = datetime.now(UTC)
    await session.flush()


def indexed_estimate_is_fresh(config: SiteConfig | None, max_age_days: int = 30) -> bool:
    """A `site:` count costs a SERP call and moves slowly. A month old is fine."""
    if config is None or config.indexed_estimate is None or config.indexed_checked_at is None:
        return False
    age = datetime.now(UTC) - config.indexed_checked_at
    return age.days < max_age_days


async def recent_chat(session: AsyncSession, run_id: uuid.UUID, limit: int = 40) -> list:
    """The editing conversation, oldest first."""
    from app.db.models import ChatMessage

    result = await session.execute(
        select(ChatMessage)
        .where(ChatMessage.run_id == run_id)
        .order_by(desc(ChatMessage.at), desc(ChatMessage.id))
        .limit(limit)
    )
    return list(reversed(list(result.scalars())))


async def runs_since(session: AsyncSession, days: int = 30) -> list[Run]:
    """Runs created in the last `days`, newest first. For the admin cost view."""
    from datetime import timedelta

    cutoff = datetime.now(UTC) - timedelta(days=days)
    result = await session.execute(
        select(Run).where(Run.created_at >= cutoff).order_by(desc(Run.created_at))
    )
    return list(result.scalars())
