"""Network side of recon: fetch robots.txt and walk the sitemap tree.

Kept apart from `recon.py` so the parsing and clustering logic stays testable with
no network. This module is thin on purpose -- it fetches, hands text to the pure
functions, and records what it could not do rather than raising.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urljoin, urlparse

import httpx

from app.scrape.recon import (
    BLOCKED_STATUSES,
    BlockedFetch,
    RobotsInfo,
    SiteRecon,
    cluster_urls,
    parse_robots,
    parse_sitemap,
    sitemap_candidates,
)

logger = logging.getLogger(__name__)

# A sitemap index can fan out a long way. Bound both the breadth and the depth so a
# misconfigured site cannot turn recon into an unbounded crawl.
MAX_SITEMAPS = 50
MAX_SITEMAP_DEPTH = 3
DEFAULT_TIMEOUT = 20.0


def normalise_site_url(site_url: str) -> str:
    """Accept what a person would type and return a usable origin."""
    candidate = site_url.strip()
    if not candidate.startswith(("http://", "https://")):
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    if not parsed.netloc:
        raise ValueError(f"Could not read a hostname from {site_url!r}")
    return f"{parsed.scheme}://{parsed.netloc}"


async def fetch_robots(
    client: httpx.AsyncClient, site_url: str, blocked: list[BlockedFetch] | None = None
) -> RobotsInfo:
    url = urljoin(site_url, "/robots.txt")
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        logger.info("robots.txt unreachable at %s: %s", url, exc)
        return RobotsInfo()

    # Recorded before the 200 check, because a refusal and a missing file are the
    # same `RobotsInfo()` to every caller and must not be the same finding. Without
    # this, a WAF denying us reads as "this site publishes no robots.txt".
    if response.status_code in BLOCKED_STATUSES:
        block = BlockedFetch(url, response.status_code, response.headers.get("server", ""))
        logger.info("robots.txt refused: %s", block.describe())
        if blocked is not None:
            blocked.append(block)

    if response.status_code != 200 or "html" in response.headers.get("content-type", ""):
        # A soft-404 returning the homepage is common; parsing it yields nonsense.
        return RobotsInfo()
    return parse_robots(response.text)


async def collect_sitemap_urls(
    client: httpx.AsyncClient, seeds: list[str], blocked: list[BlockedFetch] | None = None
) -> tuple[dict[str, str], int, list[str], dict[str, int]]:
    """Walk the sitemap tree breadth-first.

    Returns ({url: sitemap that listed it}, sitemaps read, notes). The mapping
    preserves discovery order and records provenance, which is the only useful
    grouping axis on a site whose URLs are all one level deep.
    """
    seen_sitemaps: set[str] = set()
    seen_urls: dict[str, str] = {}
    # url -> every sitemap that listed it, resolved to one below.
    memberships: dict[str, list[str]] = {}
    notes: list[str] = []
    queue = [(url, 0) for url in seeds]
    # Everything ever put on the queue, not just everything already fetched. A site
    # commonly answers /sitemap.xml, /sitemaps.xml and /sitemap_index.xml with the
    # same index, so each child sitemap gets queued once per parent. Checking only
    # `seen_sitemaps` cannot catch that -- the duplicates are all queued before any
    # of them is fetched. Measured on prosperitymedia.com.au: three fetches of every
    # child sitemap, for three copies of the same 223 URLs.
    queued: set[str] = {url for url in seeds}

    while queue and len(seen_sitemaps) < MAX_SITEMAPS:
        batch = [(u, d) for u, d in queue[:10] if u not in seen_sitemaps]
        queue = queue[10:]
        if not batch:
            continue

        seen_sitemaps.update(u for u, _ in batch)
        responses = await asyncio.gather(
            *(client.get(url) for url, _ in batch), return_exceptions=True
        )

        for (url, depth), response in zip(batch, responses, strict=True):
            if isinstance(response, BaseException):
                logger.debug("sitemap fetch failed for %s: %s", url, response)
                continue
            if response.status_code in BLOCKED_STATUSES and blocked is not None:
                # Same reasoning as `fetch_robots`: a sitemap we were refused and a
                # sitemap that does not exist both end this iteration, and only one
                # of them is a fact about the client's site.
                blocked.append(
                    BlockedFetch(url, response.status_code, response.headers.get("server", ""))
                )
            if response.status_code != 200:
                continue

            pages, nested = parse_sitemap(response.text)
            for page in pages:
                # First-wins would make the group key depend on fetch order, which
                # is concurrent and therefore not stable between runs. Every
                # membership is recorded and the most specific one chosen after the
                # walk, so a URL listed in both `sitemap_1.xml` and
                # `AllNew_BodyType.xml` lands in the group that means something.
                memberships.setdefault(page, []).append(url)
                seen_urls.setdefault(page, url)

            if nested and depth >= MAX_SITEMAP_DEPTH:
                notes.append(f"Stopped at sitemap depth {MAX_SITEMAP_DEPTH}: {url}")
                continue
            fresh = [child for child in nested if child not in queued]
            queued.update(fresh)
            queue.extend((child, depth + 1) for child in fresh)

    if len(seen_sitemaps) >= MAX_SITEMAPS:
        notes.append(f"Stopped after reading {MAX_SITEMAPS} sitemaps; there may be more.")

    shared = 0
    for page, listed_in in memberships.items():
        if len(listed_in) > 1:
            shared += 1
            seen_urls[page] = most_specific_sitemap(listed_in)
    if shared:
        notes.append(
            f"{shared:,} URL(s) appear in more than one sitemap; each was assigned to the "
            "most specific one."
        )

    return seen_urls, len(seen_sitemaps), notes, {u: len(v) for u, v in memberships.items()}


def most_specific_sitemap(candidates: list[str]) -> str:
    """Pick the sitemap that says the most about a URL.

    A generic `sitemap_1.xml` and a named `AllNew_BodyType.xml` can both list the
    same URL, and only the second is a useful group key. Preference order: deepest
    path, then longest filename, then alphabetical so the result is stable rather
    than dependent on fetch order.
    """

    def rank(url: str) -> tuple[int, int, str]:
        path = urlparse(url).path
        name = path.rsplit("/", 1)[-1]
        stem = name.rsplit(".", 1)[0]
        # A purely numeric name carries no meaning -- rank it below any real name.
        meaningful = 0 if stem.isdigit() else 1
        return (meaningful, len([p for p in path.split("/") if p]) + len(stem), url)

    return max(sorted(candidates), key=rank)


async def discover(site_url: str, user_agent: str, timeout: float = DEFAULT_TIMEOUT) -> SiteRecon:
    """Fetch robots.txt and sitemaps, then cluster whatever URLs turned up."""
    origin = normalise_site_url(site_url)

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": user_agent},
    ) as client:
        blocked: list[BlockedFetch] = []
        robots = await fetch_robots(client, origin, blocked)
        url_sources, sitemap_count, notes, memberships = await collect_sitemap_urls(
            client, sitemap_candidates(origin, robots), blocked
        )
    urls = list(url_sources)

    # Order matters: the block is stated first and the absences are then attributed
    # to it. Reporting "no robots.txt found" above a 403 invites the reader to
    # conclude the client has published nothing, which is the misreading that sent
    # a week of nrma.com.au runs to the wrong explanation.
    if blocked:
        notes.append(
            f"Blocked by the site: {'; '.join(block.describe() for block in blocked[:5])}"
            + (f" (+{len(blocked) - 5} more)" if len(blocked) > 5 else "")
        )
    if not robots.fetched:
        notes.append(
            "robots.txt could not be read because the request was refused."
            if blocked
            else "No robots.txt found. Falling back to the conventional sitemap paths."
        )
    if not urls:
        notes.append(
            "No URLs were discovered because the site refused every request. This is not "
            "a site without a sitemap -- it is a site we cannot reach from this server. "
            "Crawl it from an address the site accepts and upload the result through "
            "Imports → Screaming Frog, which skips the fetch entirely."
            if blocked
            else "No sitemap URLs found. Discovery will have to fall back to link crawling "
            "from the homepage."
        )

    return SiteRecon(
        site_url=origin,
        robots=robots,
        urls=urls,
        templates=cluster_urls(urls),
        sitemap_count=sitemap_count,
        notes=notes,
        url_sources=url_sources,
        url_memberships=memberships,
        blocked=blocked,
    )
