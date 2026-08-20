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
from enum import StrEnum
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


class CountFailure(StrEnum):
    """Why the indexed-page count is missing, in the caller's language."""

    OK = "ok"
    NO_CREDENTIALS = "no_credentials"
    NOT_WHITELISTED = "not_whitelisted"
    AUTH_FAILED = "auth_failed"
    OUT_OF_CREDITS = "out_of_credits"
    RATE_LIMITED = "rate_limited"
    NO_RESULTS = "no_results"
    API_ERROR = "api_error"
    TRANSPORT = "transport_error"


# DataForSEO reports task-level refusals in `tasks[0].status_code`, while the
# envelope stays HTTP 200 / 20000 Ok. Codes from their error reference.
TASK_STATUS_REASONS: dict[int, CountFailure] = {
    40100: CountFailure.AUTH_FAILED,
    40101: CountFailure.AUTH_FAILED,
    40200: CountFailure.OUT_OF_CREDITS,
    40201: CountFailure.OUT_OF_CREDITS,
    40202: CountFailure.OUT_OF_CREDITS,
    40207: CountFailure.NOT_WHITELISTED,
    40209: CountFailure.RATE_LIMITED,
    40501: CountFailure.API_ERROR,
}

# What to tell a human. These are read off the run page by someone deciding what to
# do next, so each one names the fix rather than the symptom.
FAILURE_MESSAGES: dict[CountFailure, str] = {
    CountFailure.NO_CREDENTIALS: (
        "No indexed-page count: DataForSEO credentials are not configured, so the "
        "sitemap is the only size signal."
    ),
    CountFailure.NOT_WHITELISTED: (
        "No indexed-page count: DataForSEO refused the request because this server's "
        "IP is not whitelisted. Add the worker's static outbound IPs at "
        "app.dataforseo.com/api-access. The credentials are fine."
    ),
    CountFailure.AUTH_FAILED: (
        "No indexed-page count: DataForSEO rejected the credentials. Check "
        "DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD."
    ),
    CountFailure.OUT_OF_CREDITS: (
        "No indexed-page count: the DataForSEO account is out of credit. This is an "
        "account condition, not a problem with this site."
    ),
    CountFailure.RATE_LIMITED: (
        "No indexed-page count: DataForSEO rate-limited the request. It will usually "
        "succeed on a later run."
    ),
    CountFailure.NO_RESULTS: (
        "No indexed-page count: Google returned no results for a `site:` query on "
        "this domain, which usually means it is not indexed at all."
    ),
    CountFailure.API_ERROR: ("No indexed-page count: DataForSEO returned an error for this query."),
    CountFailure.TRANSPORT: (
        "No indexed-page count: could not reach DataForSEO. The sitemap is the only "
        "size signal for this run."
    ),
}


@dataclass(slots=True)
class IndexedCount:
    """A count, or a stated reason there isn't one.

    The predecessor returned a bare `int | None`, so every cause collapsed into the
    same "credentials are not configured" warning -- which sent a real IP rejection
    on a long detour through the credentials. The reason travels with the result now.
    """

    count: int | None = None
    reason: CountFailure = CountFailure.OK
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.count is None

    def explain(self) -> str:
        message = FAILURE_MESSAGES.get(self.reason, FAILURE_MESSAGES[CountFailure.API_ERROR])
        return f"{message} ({self.detail})" if self.detail else message


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
    indexed: IndexedCount | None = None,
) -> SizeEstimate:
    """Combine the two counts into a budget and a set of warnings. Pure.

    `indexed` is the richer form of `indexed_estimate` and carries the reason a
    count is missing. Both are accepted so existing callers and tests keep working;
    when `indexed` is supplied it wins.
    """
    if indexed is not None and indexed_estimate is None:
        indexed_estimate = indexed.count
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
        # Say what actually happened. `indexed` carries the reason when the caller
        # has one; without it we can only report the count as missing.
        estimate.warnings.append(
            indexed.explain()
            if indexed is not None
            else FAILURE_MESSAGES[CountFailure.NO_CREDENTIALS]
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
) -> IndexedCount:
    """Ask Google, via DataForSEO, roughly how many pages of `domain` it indexes.

    Never raises: a size check is advisory and must not be the reason a run cannot
    start. But it now says *why* it came back empty. The figure Google reports for a
    `site:` query is a rounded estimate that drifts between requests -- it is an
    order-of-magnitude signal and is used as one.

    Costs one live SERP call per invocation. Results are cached per domain by the
    caller rather than re-requested on every run.
    """
    if not (login and password):
        return IndexedCount(reason=CountFailure.NO_CREDENTIALS)

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
        logger.warning("site: size check transport failure for %s: %s", host, exc)
        return IndexedCount(reason=CountFailure.TRANSPORT, detail=f"{type(exc).__name__}: {exc}")

    result = parse_results_count(body)
    if result.failed:
        logger.warning("site: size check for %s: %s", host, result.explain())
    return result


def parse_results_count(body: dict) -> IndexedCount:
    """Read a DataForSEO SERP response, including the reason it did not answer.

    The response is HTTP 200 and `status_code: 20000 Ok.` even when the *task* was
    refused -- the refusal is one level down, in `tasks[0].status_code`. Reading only
    the count is why an IP rejection presented as "credentials are not configured".
    """
    for task in body.get("tasks") or []:
        for result in task.get("result") or []:
            count = result.get("se_results_count")
            if isinstance(count, int) and count >= 0:
                return IndexedCount(count=count, reason=CountFailure.OK)

    for task in body.get("tasks") or []:
        status = task.get("status_code")
        message = (task.get("status_message") or "").strip()
        if isinstance(status, int) and status != 20000:
            return IndexedCount(
                reason=TASK_STATUS_REASONS.get(status, CountFailure.API_ERROR),
                detail=f"{status} {message}".strip(),
            )

    # A well-formed response that simply carried no count -- a `site:` query for a
    # domain Google indexes nothing for is a real, and informative, answer.
    return IndexedCount(reason=CountFailure.NO_RESULTS)
