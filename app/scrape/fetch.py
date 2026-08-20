"""Page fetching with a cheapest-first escalation ladder.

Scrapling gives three fetchers at very different costs. `Fetcher` is plain HTTP over
curl_cffi with TLS impersonation, ~40MB. `DynamicFetcher` and `StealthyFetcher` each
instantiate full Playwright, at 800MB+ per concurrent session. On a 2GB worker that
is a hard ceiling of two browsers, so the ladder exists to keep almost every page on
the cheap rung:

    Fetcher  ->  blocked or JS shell  ->  DynamicFetcher  ->  still blocked  ->  StealthyFetcher

Browser tiers are gated by their own semaphore, separate from the HTTP one, because
running 8 HTTP fetches alongside 2 browsers is fine and running 8 browsers is not.

A fourth rung, Firecrawl, is appended when a key is configured. It sits last because
it is the only rung that bills per page, and it exists because a managed unblocking
service fails differently from a local browser -- which is the whole point of having
a fallback at all. With no key the ladder is unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from app.scrape.extract import ExtractedPage, extract, looks_like_js_shell
from app.scrape.firecrawl import FirecrawlFetcher

logger = logging.getLogger(__name__)

# Statuses that mean "try harder", as opposed to "this page is genuinely not here".
ESCALATE_STATUSES = frozenset({401, 403, 405, 406, 429, 500, 502, 503, 520, 521, 522, 530})

# Chromium uses /dev/shm for rendering and Docker caps it at 64MB, so it crashes
# without this. Railway is Docker.
BROWSER_FLAGS = ("--disable-dev-shm-usage", "--no-sandbox")


class Tier(StrEnum):
    HTTP = "http"
    DYNAMIC = "dynamic"
    STEALTH = "stealth"
    FIRECRAWL = "firecrawl"


@dataclass(slots=True)
class FetchResult:
    url: str
    status: int = 0
    tier: Tier | None = None
    page: ExtractedPage | None = None
    error: str = ""
    attempts: list[str] = field(default_factory=list)
    # Set when the response settles the question: a 404 is not going to become a
    # 200 because we spent 800MB launching Chromium at it.
    terminal: bool = False

    @property
    def ok(self) -> bool:
        return self.page is not None and not self.page.is_thin


@dataclass(slots=True)
class FetchStats:
    """Per-tier counts, so the cost of a run is visible rather than inferred."""

    by_tier: dict[str, int] = field(default_factory=dict)
    failed: int = 0

    def record(self, result: FetchResult) -> None:
        if result.tier and result.ok:
            # `str(...)`, not the enum member: these counts are rendered into
            # progress messages and stored as JSON, and a StrEnum key formats as
            # "<Tier.HTTP: 'http'>" in both.
            tier = str(result.tier)
            self.by_tier[tier] = self.by_tier.get(tier, 0) + 1
        else:
            self.failed += 1


ProgressCallback = Callable[[int, int], None] | None


class PageFetcher:
    """Fetches pages, escalating only when the cheap tier demonstrably failed."""

    def __init__(
        self,
        user_agent: str,
        max_http_concurrency: int = 8,
        max_browser_concurrency: int = 2,
        timeout: float = 30.0,
        allow_browser: bool = True,
        firecrawl: FirecrawlFetcher | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.allow_browser = allow_browser
        self.firecrawl = firecrawl
        self._http = asyncio.Semaphore(max_http_concurrency)
        self._browser = asyncio.Semaphore(max_browser_concurrency)
        self.stats = FetchStats()
        # Set when Firecrawl reports an account-level failure -- a bad key or an
        # exhausted balance. Those are true of the next 500 pages too, so the rung
        # is dropped for the rest of the run instead of being paid for repeatedly.
        self.firecrawl_disabled_reason = ""

    async def fetch(self, url: str) -> FetchResult:
        result = FetchResult(url=url)

        for tier in self._ladder():
            attempt = await self._attempt(tier, url)
            result.attempts.append(f"{tier}:{attempt.status or attempt.error or '?'}")
            result.status = attempt.status or result.status
            result.tier = tier

            if attempt.page is not None:
                result.page = attempt.page
                if not self._should_escalate(attempt):
                    result.error = ""
                    self.stats.record(result)
                    return result

            result.error = attempt.error
            if attempt.terminal:
                break

        self.stats.record(result)
        return result

    async def fetch_many(
        self, urls: Iterable[str], on_progress: ProgressCallback = None
    ) -> list[FetchResult]:
        """Fetch concurrently. Progress reports completions, not submissions.

        The source batched in tens with a blocking `time.sleep(1)` between batches and
        a fresh thread pool each time, so one slow page stalled nine others. The
        semaphores do that job properly here.
        """
        urls = list(urls)
        total = len(urls)
        results: list[FetchResult] = []

        tasks = [asyncio.create_task(self.fetch(u)) for u in urls]
        for done, coro in enumerate(asyncio.as_completed(tasks), start=1):
            results.append(await coro)
            if on_progress:
                on_progress(done, total)

        order = {u: i for i, u in enumerate(urls)}
        results.sort(key=lambda r: order[r.url])
        return results

    # -- internals ---------------------------------------------------------

    def _ladder(self) -> list[Tier]:
        tiers = [Tier.HTTP, Tier.DYNAMIC, Tier.STEALTH] if self.allow_browser else [Tier.HTTP]
        if self._firecrawl_available():
            tiers.append(Tier.FIRECRAWL)
        return tiers

    def _firecrawl_available(self) -> bool:
        return bool(
            self.firecrawl and self.firecrawl.enabled and not self.firecrawl_disabled_reason
        )

    @staticmethod
    def _should_escalate(attempt: FetchResult) -> bool:
        if attempt.status in ESCALATE_STATUSES:
            return True
        return attempt.page is None or attempt.page.is_thin

    async def _attempt(self, tier: Tier, url: str) -> FetchResult:
        if tier is Tier.FIRECRAWL:
            return await self._attempt_firecrawl(url)

        semaphore = self._http if tier is Tier.HTTP else self._browser
        async with semaphore:
            try:
                response = await self._raw_fetch(tier, url)
            except Exception as exc:
                logger.debug("%s fetch failed for %s: %s", tier, url, exc)
                return FetchResult(url=url, tier=tier, error=f"{type(exc).__name__}: {exc}")

        status = getattr(response, "status", 0) or 0
        html_text = getattr(response, "html_content", "") or ""

        if status >= 400 and status not in ESCALATE_STATUSES:
            return FetchResult(
                url=url, status=status, tier=tier, error=f"HTTP {status}", terminal=True
            )

        # A JS shell is a successful response with nothing in it. Reporting it as a
        # page would end the ladder one rung too early.
        if tier is Tier.HTTP and looks_like_js_shell(html_text):
            return FetchResult(url=url, status=status, tier=tier, error="js-shell")

        return FetchResult(url=url, status=status, tier=tier, page=extract(html_text, url))

    async def _attempt_firecrawl(self, url: str) -> FetchResult:
        """The paid rung. Returns an already-extracted page, so no `extract` call."""
        assert self.firecrawl is not None  # guarded by `_firecrawl_available`

        # Remote API, no local browser: it belongs under the HTTP budget, not the
        # 2-slot browser one.
        async with self._http:
            response = await self.firecrawl.scrape(url)

        if response.status in {401, 402}:
            self.firecrawl_disabled_reason = response.error
            logger.warning("Firecrawl disabled for the rest of this run: %s", response.error)
            return FetchResult(url=url, tier=Tier.FIRECRAWL, error=response.error, terminal=True)

        if response.page is None:
            return FetchResult(
                url=url, status=response.status, tier=Tier.FIRECRAWL, error=response.error
            )

        return FetchResult(
            url=url, status=response.status or 200, tier=Tier.FIRECRAWL, page=response.page
        )

    async def _raw_fetch(self, tier: Tier, url: str):
        from scrapling import AsyncFetcher, DynamicFetcher, StealthyFetcher

        if tier is Tier.HTTP:
            return await AsyncFetcher.get(
                url,
                timeout=self.timeout,
                follow_redirects=True,
                stealthy_headers=True,
                headers={"User-Agent": self.user_agent},
                retries=1,
            )

        if tier is Tier.DYNAMIC:
            return await DynamicFetcher.async_fetch(
                url,
                headless=True,
                network_idle=True,
                disable_resources=True,
                timeout=self.timeout * 1000,
                useragent=self.user_agent,
                extra_flags=list(BROWSER_FLAGS),
            )

        return await StealthyFetcher.async_fetch(
            url,
            headless=True,
            network_idle=True,
            disable_resources=True,
            timeout=self.timeout * 1000,
            extra_flags=list(BROWSER_FLAGS),
        )
