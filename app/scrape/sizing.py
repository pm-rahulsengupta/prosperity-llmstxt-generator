"""Pre-flight size check: how big is this site, before we spend anything crawling it.

Two independent counts, because they answer different questions and the gap between
them is itself the finding:

* **Sitemap count** -- what the site says it has. Free, one request, already paid for
  by recon.
* **Indexed count** -- what Google says it has, via a `site:` query through
  DataForSEO. Costs one SERP call.

A site whose index is three times its sitemap has orphan pages the sitemap never
declared, and a sitemap-only crawl will miss them. A site whose sitemap is three
times its index is publishing pages Google has declined to keep, which is a strong
prior that most of them are thin, duplicated or paginated -- and a reason to cap the
crawl hard rather than pay to fetch all of them.

Both counts are advisory. Nothing here blocks a run; it sets a default budget and
puts a number in front of the human before the spend starts.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

DATAFORSEO_ENDPOINT = "https://api.dataforseo.com/v3/serp/google/organic/live/advanced"

# Sitemaps routinely list assets. They are pages for the crawler's purposes only if
# they render HTML, and none of these do.
NON_HTML_EXTENSIONS = frozenset(
    {
        "jpg",
        "jpeg",
        "png",
        "gif",
        "webp",
        "avif",
        "svg",
        "ico",
        "bmp",
        "tiff",
        "pdf",
        "doc",
        "docx",
        "xls",
        "xlsx",
        "ppt",
        "pptx",
        "csv",
        "zip",
        "gz",
        "mp3",
        "mp4",
        "mov",
        "avi",
        "webm",
        "wav",
        "woff",
        "woff2",
        "ttf",
        "eot",
        "css",
        "js",
        "json",
        "xml",
        "rss",
        "txt",
    }
)

# Buckets, by the larger of the two counts. The numbers are crawl-cost thresholds,
# not opinions about site quality.
TIERS: tuple[tuple[int, str, int], ...] = (
    (100, "small", 0),  # 0 == no cap, take everything
    (1_000, "medium", 400),
    (10_000, "large", 1_000),
    (10**9, "huge", 1_500),
)


@dataclass(slots=True)
class SizeEstimate:
    site_url: str
    sitemap_total: int = 0
    sitemap_html: int = 0
    indexed_estimate: int | None = None
    indexed_source: str = "unavailable"
    tier: str = "unknown"
    recommended_max_pages: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def best_count(self) -> int:
        """The number to plan against: the larger of what we know."""
        return max(self.sitemap_html, self.indexed_estimate or 0)

    @property
    def needs_review(self) -> bool:
        """True when a human should look at the plan before the crawl runs."""
        return self.tier in {"large", "huge"} or bool(self.warnings)

    def summary(self) -> str:
        indexed = (
            f"{self.indexed_estimate:,} indexed (Google, via {self.indexed_source})"
            if self.indexed_estimate is not None
            else "indexed count unavailable"
        )
        cap = "no cap" if not self.recommended_max_pages else f"{self.recommended_max_pages:,}"
        lines = [
            f"{self.site_url}: {self.sitemap_html:,} crawlable URLs in sitemaps "
            f"({self.sitemap_total:,} listed), {indexed}.",
            f"Size tier: {self.tier}. Suggested page cap: {cap}.",
        ]
        lines.extend(f"  ! {w}" for w in self.warnings)
        return "\n".join(lines)


def count_html_urls(urls: list[str]) -> tuple[int, int]:
    """(total listed, likely-HTML) for a set of sitemap URLs."""
    html = 0
    for url in urls:
        tail = urlparse(url).path.rsplit("/", 1)[-1]
        if "." in tail and tail.rsplit(".", 1)[-1].lower() in NON_HTML_EXTENSIONS:
            continue
        html += 1
    return len(urls), html


def classify(count: int) -> tuple[str, int]:
    """(tier name, recommended page cap) for a page count."""
    for threshold, name, cap in TIERS:
        if count < threshold:
            return name, cap
    return "huge", TIERS[-1][2]


def assess(
    site_url: str,
    sitemap_urls: list[str],
    indexed_estimate: int | None = None,
    indexed_source: str = "unavailable",
) -> SizeEstimate:
    """Combine the two counts into a budget and a set of warnings. Pure."""
    total, html = count_html_urls(sitemap_urls)
    estimate = SizeEstimate(
        site_url=site_url,
        sitemap_total=total,
        sitemap_html=html,
        indexed_estimate=indexed_estimate,
        indexed_source=indexed_source if indexed_estimate is not None else "unavailable",
    )
    estimate.tier, estimate.recommended_max_pages = classify(estimate.best_count)

    if total and html < total:
        estimate.warnings.append(
            f"{total - html:,} sitemap entries are assets, not pages, and are excluded."
        )

    if indexed_estimate is not None and html:
        if indexed_estimate > html * 3:
            estimate.warnings.append(
                f"Google indexes roughly {indexed_estimate:,} pages but the sitemaps list "
                f"only {html:,}. There are orphan pages no sitemap declares -- link "
                "crawling will find more than sitemap discovery alone."
            )
        elif html > max(indexed_estimate * 3, 50):
            estimate.warnings.append(
                f"The sitemaps list {html:,} pages but Google indexes roughly "
                f"{indexed_estimate:,}. Most of what is published is not being kept, "
                "which usually means thin, duplicated or paginated URLs. Cap the crawl "
                "and lean on the exclude rules."
            )
    elif indexed_estimate is None:
        estimate.warnings.append(
            "No indexed-page count: DataForSEO credentials are not configured, so the "
            "sitemap is the only size signal."
        )

    if not html:
        estimate.warnings.append(
            "No crawlable URLs in any sitemap. Discovery falls back to link crawling "
            "from the homepage, which is slower and less complete."
        )

    if estimate.tier == "huge":
        estimate.warnings.append(
            "This site is too large to crawl exhaustively. Review the crawl plan and "
            "its exclude rules before starting; an unbounded run here is real money."
        )

    return estimate


async def indexed_page_count(
    domain: str,
    login: str,
    password: str,
    location_code: int = 2036,
    language_code: str = "en",
    timeout: float = 45.0,
) -> int | None:
    """Ask Google, via DataForSEO, roughly how many pages of `domain` it indexes.

    Returns None rather than raising: a size check is advisory and must never be the
    reason a run cannot start. The figure Google reports for a `site:` query is a
    rounded estimate and drifts between requests -- it is an order-of-magnitude
    signal, and it is used as one.

    Costs one live SERP call per invocation. Results are cached per domain by the
    caller rather than re-requested on every run.
    """
    if not (login and password):
        return None

    host = urlparse(domain if "//" in domain else f"https://{domain}").netloc or domain
    host = host.removeprefix("www.")
    token = base64.b64encode(f"{login}:{password}".encode()).decode()
    payload = [
        {
            "keyword": f"site:{host}",
            "location_code": location_code,
            "language_code": language_code,
            "depth": 10,
            "device": "desktop",
        }
    ]

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                DATAFORSEO_ENDPOINT,
                json=payload,
                headers={
                    "Authorization": f"Basic {token}",
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("site: size check failed for %s: %s", host, exc)
        return None

    return parse_results_count(body)


def parse_results_count(body: dict) -> int | None:
    """Pull `se_results_count` out of a DataForSEO SERP response."""
    for task in body.get("tasks") or []:
        for result in task.get("result") or []:
            count = result.get("se_results_count")
            if isinstance(count, int) and count >= 0:
                return count
    return None
