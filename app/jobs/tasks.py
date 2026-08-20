"""The pipeline, as background jobs.

Two tasks, split at the human review gate:

    preflight  ->  [ a person reviews and edits the plan ]  ->  generate

Everything before the gate is cheap: robots, sitemaps, clustering, one SERP call and
one LLM planning call. Everything after it costs crawl time and per-page LLM spend.
Putting the gate between them is the whole reason stage 1 exists.

Each task records `RunEvent` rows as it goes, which is what the UI polls. A run that
dies mid-crawl leaves its events behind, so "what was it doing when it stopped" has
an answer -- the source lost the entire run on a container restart.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from app.config import get_settings
from app.core.models import PageEntry, Section
from app.core.pipeline import GenerateOptions, generate
from app.core.ranking import (
    importance_score,
    is_optional_page,
    sort_by_importance,
)
from app.db import repo
from app.db.base import session_scope
from app.db.models import RunStatus
from app.jobs.queue import QUEUE_CRAWL, app
from app.llm.client import LLMClient, LLMUsage
from app.llm.prompts.plan import CrawlPlan
from app.llm.stages import (
    plan_crawl,
    review_output,
    select_urls,
    summarise_pages,
    summarise_site,
    triage_pages,
)
from app.scrape.fetch import PageFetcher
from app.scrape.firecrawl import FirecrawlFetcher
from app.scrape.preflight import effective_page_cap, run_preflight

logger = logging.getLogger(__name__)


def build_fetcher(settings, allow_browser: bool = True) -> PageFetcher:
    firecrawl = (
        FirecrawlFetcher(api_key=settings.firecrawl_api_key, base_url=settings.firecrawl_base_url)
        if settings.firecrawl_enabled
        else None
    )
    return PageFetcher(
        user_agent=settings.crawl_user_agent,
        max_http_concurrency=settings.max_http_concurrency,
        max_browser_concurrency=settings.max_browser_concurrency,
        allow_browser=allow_browser,
        firecrawl=firecrawl,
    )


@app.task(name="preflight", queue=QUEUE_CRAWL, retry=2)
async def preflight_task(run_id: str, requested_max_pages: int = 0) -> None:
    """Recon, size check and crawl plan. Stops at the review gate, crawls nothing."""
    settings = get_settings()
    rid = uuid.UUID(run_id)

    async with session_scope() as session:
        run = await repo.get_run(session, rid)
        if run is None:
            logger.error("preflight for unknown run %s", run_id)
            return
        await repo.set_status(session, run, RunStatus.PREFLIGHT)
        await repo.record_event(session, rid, "preflight", "Reading robots.txt and sitemaps")
        site_url = run.site_url
        domain = run.domain

    try:
        pre = await run_preflight(site_url, settings)
    except Exception as exc:
        logger.exception("preflight failed for %s", site_url)
        async with session_scope() as session:
            run = await repo.get_run(session, rid)
            if run is not None:
                await repo.set_status(session, run, RunStatus.FAILED, error=str(exc))
                await repo.record_event(session, rid, "preflight", f"Failed: {exc}")
        return

    cap = effective_page_cap(pre.size, requested_max_pages, settings.crawl_default_max_pages)

    usage = LLMUsage()
    client = LLMClient(settings, usage)
    plan = await plan_crawl(client, pre.planning_brief(), pre.recon, cap)

    async with session_scope() as session:
        run = await repo.get_run(session, rid)
        if run is None:
            return
        run.sitemap_total = pre.size.sitemap_total
        run.sitemap_html = pre.size.sitemap_html
        run.indexed_estimate = pre.size.indexed_estimate
        run.size_tier = pre.size.tier
        run.size_warnings = list(pre.size.warnings)
        run.max_pages = cap
        run.plan = plan.to_dict()
        run.plan_source = plan.source
        run.pattern = plan.site_pattern
        if plan.site_name:
            run.site_name = plan.site_name
        run.stats = {**(run.stats or {}), "llm": usage.as_dict(), "serp_calls": pre.serp_calls}
        await repo.cache_indexed_estimate(session, domain, pre.size.indexed_estimate)
        await repo.set_status(session, run, RunStatus.AWAITING_REVIEW)
        await repo.record_event(
            session,
            rid,
            "plan",
            f"{pre.size.sitemap_html} crawlable URLs, {len(plan.rules)} templates, "
            f"cap {cap} ({plan.source} plan). Review before crawling.",
            total=pre.size.sitemap_html,
        )


@app.task(name="generate", queue=QUEUE_CRAWL, retry=1)
async def generate_task(run_id: str) -> None:
    """Crawl, triage, summarise, assemble, review. Runs only after plan approval."""
    settings = get_settings()
    rid = uuid.UUID(run_id)
    usage = LLMUsage()
    client = LLMClient(settings, usage)

    async with session_scope() as session:
        run = await repo.get_run(session, rid)
        if run is None:
            return
        site_url = run.site_url
        plan = CrawlPlan.from_dict(run.plan or {})
        cap = run.max_pages or settings.crawl_default_max_pages
        await repo.set_status(session, run, RunStatus.CRAWLING)
        await repo.record_event(session, rid, "crawl", "Re-reading sitemaps")

    try:
        pre = await run_preflight(site_url, settings)
        urls = select_urls(pre.recon, plan, cap)

        async with session_scope() as session:
            await repo.record_event(
                session, rid, "crawl", f"Fetching {len(urls)} pages", done=0, total=len(urls)
            )

        fetcher = build_fetcher(settings, allow_browser=True)
        depths = _depth_by_url(urls, pre.recon.site_url)
        results = await fetcher.fetch_many(urls)

        entries: list[PageEntry] = []
        for result in results:
            if not result.ok or result.page is None:
                continue
            page = result.page
            entries.append(
                PageEntry(
                    url=page.url or result.url,
                    title=page.title,
                    description=page.description,
                    h1=page.h1,
                    word_count=page.word_count,
                    markdown=page.markdown,
                    canonical=page.canonical,
                    status_code=result.status,
                    fetch_tier=str(result.tier or ""),
                    # Recorded from the crawl, not left at the default. The source
                    # never set this, which is why `## Optional` was always empty on
                    # every crawl-sourced file it ever produced.
                    crawl_depth=depths.get(result.url, 1),
                )
            )

        if not entries:
            raise RuntimeError("No pages could be fetched. Check robots rules and the plan.")

        scores = {entry.url: importance_score(entry) for entry in entries}
        for entry in entries:
            entry.is_optional = is_optional_page(entry)

        async with session_scope() as session:
            await repo.replace_pages(session, rid, entries, scores)
            await repo.record_event(
                session,
                rid,
                "crawl",
                f"Fetched {len(entries)} of {len(urls)}; tiers {fetcher.stats.by_tier}",
                done=len(entries),
                total=len(urls),
            )
            run = await repo.get_run(session, rid)
            if run is not None:
                run.stats = {
                    **(run.stats or {}),
                    "fetch": {
                        "by_tier": dict(fetcher.stats.by_tier),
                        "failed": fetcher.stats.failed,
                        "requested": len(urls),
                    },
                }
                await repo.set_status(session, run, RunStatus.TRIAGING)

        # -- triage ---------------------------------------------------------
        assignments = await triage_pages(client, entries, plan.site_pattern, scores)
        for entry in entries:
            if (assignment := assignments.get(entry.url)) is not None:
                entry.section = assignment.section
                entry.is_optional = assignment.is_optional

        async with session_scope() as session:
            await repo.record_event(
                session,
                rid,
                "triage",
                f"{len(assignments)} of {len(entries)} pages placed by model"
                if assignments
                else "No LLM triage; heuristic sections retained",
                done=len(assignments),
                total=len(entries),
            )
            run = await repo.get_run(session, rid)
            if run is not None:
                await repo.set_status(session, run, RunStatus.SUMMARISING)

        # -- summarise ------------------------------------------------------
        ranked = sort_by_importance(entries)
        blurb = await summarise_site(client, site_url, plan.site_name, ranked)
        copy = await summarise_pages(client, ranked)
        for entry in entries:
            if (written := copy.get(entry.url)) is not None:
                entry.title = written.title or entry.title
                entry.description = written.description or entry.description

        async with session_scope() as session:
            await repo.record_event(
                session,
                rid,
                "summarise",
                f"{len(copy)} of {len(entries)} link lines written by model"
                if copy
                else "No LLM copy; page metadata used",
                done=len(copy),
                total=len(entries),
            )
            run = await repo.get_run(session, rid)
            if run is not None:
                await repo.set_status(session, run, RunStatus.ASSEMBLING)

        # -- assemble -------------------------------------------------------
        sections, optional = _sections_from(entries)
        result, _reports = generate(
            site_url=site_url,
            entries=entries,
            options=GenerateOptions(
                pattern=plan.site_pattern,
                site_name=(blurb.site_name if blurb else "") or plan.site_name,
                site_summary=blurb.summary if blurb else "",
                generated_on=datetime.now(UTC).date(),
            ),
            sections=sections,
            optional=optional,
        )

        # -- QA -------------------------------------------------------------
        result.issues = list(result.issues) + await review_output(
            client, result.llmstxt, result.issues
        )

        async with session_scope() as session:
            run = await repo.get_run(session, rid)
            if run is None:
                return
            await repo.store_result(session, run, result)
            run.stats = {**(run.stats or {}), "llm": usage.as_dict()}
            await repo.set_status(session, run, RunStatus.COMPLETE)
            await repo.record_event(
                session,
                rid,
                "complete",
                f"{result.pages_included} pages in {len(result.sections)} sections; "
                f"{len(result.issues)} issue(s)",
                done=result.pages_included,
                total=len(entries),
            )

    except Exception as exc:
        logger.exception("generate failed for run %s", run_id)
        async with session_scope() as session:
            run = await repo.get_run(session, rid)
            if run is not None:
                run.stats = {**(run.stats or {}), "llm": usage.as_dict()}
                await repo.set_status(session, run, RunStatus.FAILED, error=str(exc))
                await repo.record_event(session, rid, "failed", str(exc))
        raise


def _depth_by_url(urls: list[str], site_url: str) -> dict[str, int]:
    """Path depth as a stand-in for crawl depth on sitemap-discovered URLs.

    A real link-graph depth needs a link crawl; for a sitemap-driven run the path
    depth is the honest approximation, and it is what `## Optional` needs to be
    anything other than permanently empty.
    """
    from urllib.parse import urlparse

    depths: dict[str, int] = {}
    for url in urls:
        path = urlparse(url).path.strip("/")
        depths[url] = 0 if not path else len([s for s in path.split("/") if s])
    return depths


def _sections_from(entries: list[PageEntry]) -> tuple[list[Section] | None, list[PageEntry] | None]:
    """Build sections from assignments, or return None to use URL grouping.

    Returning None is not a failure: it is the deterministic path, and it is what
    runs whenever triage did not assign anything.
    """
    assigned = [entry for entry in entries if entry.section and not entry.is_optional]
    if not assigned:
        return None, None

    optional = [entry for entry in entries if entry.is_optional]
    unassigned = [entry for entry in entries if not entry.section and not entry.is_optional]

    grouped: dict[str, list[PageEntry]] = {}
    for entry in assigned:
        grouped.setdefault(entry.section, []).append(entry)
    # Pages the model skipped are not dropped; they join the first section rather
    # than disappearing from the file.
    if unassigned:
        first = next(iter(grouped), "Pages")
        grouped.setdefault(first, []).extend(unassigned)

    sections = [
        Section(name=name, pages=sort_by_importance(pages), position=position)
        for position, (name, pages) in enumerate(grouped.items())
    ]
    return sections, optional
