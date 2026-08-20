"""Every database read and write the app performs.

Kept in one module so the routes and the jobs cannot each invent their own version
of "save the pages". The source had three drifted copies of the same pipeline; this
is the cheapest structural guard against that happening again.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from urllib.parse import urlparse

from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import GenerationResult, PageEntry
from app.db.models import Page, Run, RunEvent, RunStatus, SectionRow, SiteConfig


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

    by_url = {page.url: page for page in await get_pages(session, run.id)}
    for position, section in enumerate(result.sections):
        for order, entry in enumerate(section.pages):
            if (row := by_url.get(entry.url)) is not None:
                row.section_name = section.name
                row.is_optional = False
                row.position = position * 1_000 + order
    for entry in result.optional:
        if (row := by_url.get(entry.url)) is not None:
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
