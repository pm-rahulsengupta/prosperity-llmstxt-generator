"""Everything that happens before a single page is crawled.

One call: read robots.txt, walk the sitemaps, cluster the URLs into shapes, ask
Google how many pages it indexes, and turn all of that into a size estimate and a
default page budget. It is cheap -- a handful of HTTP requests and at most one SERP
call -- and it is the last point at which a run can be reconsidered for free.

The output is what the human reviews and what the LLM planner is given. Neither of
them should ever see 40,000 raw URLs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.config import Settings
from app.scrape.discover import discover
from app.scrape.recon import SiteRecon, summarise_for_plan
from app.scrape.sizing import SizeEstimate, assess, indexed_page_count

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Preflight:
    recon: SiteRecon
    size: SizeEstimate
    # Populated only when the size check ran and cost something, so the run record
    # can show what it spent.
    serp_calls: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def site_url(self) -> str:
        return self.recon.site_url

    def planning_brief(self) -> str:
        """The text handed to the LLM planner, and shown to the human beside it."""
        return "\n\n".join([self.size.summary(), summarise_for_plan(self.recon)])


async def run_preflight(site_url: str, settings: Settings) -> Preflight:
    """Recon plus size check. Never raises for a missing signal; records it instead."""
    recon = await discover(site_url, user_agent=settings.crawl_user_agent)

    indexed: int | None = None
    serp_calls = 0
    if settings.size_check_enabled:
        indexed = await indexed_page_count(
            recon.site_url,
            login=settings.dataforseo_login,
            password=settings.dataforseo_password,
            location_code=settings.size_check_location_code,
            language_code=settings.size_check_language_code,
        )
        # Charged whether or not a usable number came back.
        serp_calls = 1
        if indexed is None:
            logger.info("size check returned no count for %s", recon.site_url)

    size = assess(
        site_url=recon.site_url,
        sitemap_urls=recon.urls,
        indexed_estimate=indexed,
        indexed_source="dataforseo",
    )

    return Preflight(recon=recon, size=size, serp_calls=serp_calls, notes=list(recon.notes))


def effective_page_cap(size: SizeEstimate, requested: int | None, configured_default: int) -> int:
    """Reconcile the three page caps that can be in play.

    An explicit request from the human always wins -- the size check advises, it does
    not overrule. Otherwise take the tighter of the size-derived suggestion and the
    configured default, and treat "no cap" on a small site as exactly that rather
    than as an unbounded run.
    """
    if requested and requested > 0:
        return requested
    if size.recommended_max_pages == 0:
        # Small site: everything it has, but never more than the sitemap holds.
        return size.sitemap_html or configured_default
    return min(size.recommended_max_pages, configured_default)
