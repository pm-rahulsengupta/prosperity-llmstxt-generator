"""Firecrawl as the last rung of the fetch ladder.

Scrapling handles the overwhelming majority of pages, and it is free and
self-hosted. But its two browser tiers can still lose to an aggressive WAF, and
when they do the run currently ends with a gap in the output. Firecrawl is a
managed service with its own proxy pool and unblocking stack, so it fails
differently -- which is the only property that makes a fallback worth having.

It is off unless `FIRECRAWL_API_KEY` is set, and even then it is only reached after
HTTP, Dynamic and Stealth have each been tried and failed. That ordering matters:
Firecrawl bills per page, the other three do not.

Note: as of this writing no Firecrawl key exists on this machine or in the
Prosperity Railway project. The only Firecrawl access in the building is the hosted
`prosperity_firecrawl_scrape` MCP tool, which is reachable from an agent session and
*not* from a deployed container. This adapter is therefore built and tested but
dormant until a key is provisioned; see docs/OPERATIONS.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.scrape.extract import ExtractedPage

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.firecrawl.dev/v2"


@dataclass(slots=True)
class FirecrawlResponse:
    status: int
    page: ExtractedPage | None
    error: str = ""


class FirecrawlFetcher:
    """Thin client for Firecrawl's scrape endpoint, shaped like the other tiers.

    Deliberately not the official SDK: this needs one endpoint, needs it async, and
    the SDK would add a dependency whose failure modes we would then have to learn.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 60.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def scrape(self, url: str) -> FirecrawlResponse:
        if not self.enabled:
            return FirecrawlResponse(status=0, page=None, error="firecrawl: no API key")

        payload = {
            "url": url,
            "formats": ["markdown"],
            # The same job trafilatura does locally. Without it the nav, cookie
            # banner and footer land in every entry of llms-full.txt.
            "onlyMainContent": True,
            "blockAds": True,
            "timeout": int(self.timeout * 1000),
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout + 15) as client:
                response = await client.post(
                    f"{self.base_url}/scrape",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            logger.warning("firecrawl transport failure for %s: %s", url, exc)
            return FirecrawlResponse(status=0, page=None, error=f"firecrawl: {exc}")

        if response.status_code == 402:
            # Out of credits is an account condition, not a page condition. Say so
            # plainly rather than letting it read as "this page is unfetchable".
            return FirecrawlResponse(status=402, page=None, error="firecrawl: out of credits")
        if response.status_code == 401:
            return FirecrawlResponse(status=401, page=None, error="firecrawl: bad API key")
        if response.status_code >= 400:
            return FirecrawlResponse(
                status=response.status_code,
                page=None,
                error=f"firecrawl: HTTP {response.status_code}",
            )

        try:
            body = response.json()
        except ValueError:
            return FirecrawlResponse(
                status=response.status_code, page=None, error="firecrawl: bad JSON"
            )

        return parse_scrape_response(body, url)


def parse_scrape_response(body: dict, url: str) -> FirecrawlResponse:
    """Map a Firecrawl scrape payload onto `ExtractedPage`. Pure, so it is testable.

    Firecrawl v1 and v2 both wrap the useful part in `data`, and both report the
    origin server's status inside `metadata.statusCode` rather than the HTTP status
    of the API call itself -- which is 200 even when the target returned 403.
    """
    if body.get("success") is False:
        return FirecrawlResponse(
            status=0, page=None, error=f"firecrawl: {body.get('error', 'unsuccessful')}"
        )

    data = body.get("data") or body
    metadata = data.get("metadata") or {}
    markdown = (data.get("markdown") or "").strip()
    origin_status = metadata.get("statusCode") or metadata.get("status_code") or 0

    if not markdown:
        return FirecrawlResponse(
            status=int(origin_status or 0), page=None, error="firecrawl: empty markdown"
        )

    page = ExtractedPage(
        url=metadata.get("sourceURL") or metadata.get("url") or url,
        title=(metadata.get("title") or "").strip(),
        description=(metadata.get("description") or "").strip(),
        # Firecrawl returns markdown, not HTML, so the first ATX H1 is the best
        # available stand-in for the <h1> the other tiers read directly.
        h1=_first_h1(markdown),
        markdown=markdown,
        word_count=len(markdown.split()),
        canonical=(metadata.get("canonical") or metadata.get("ogUrl") or "").strip(),
        robots_meta=(metadata.get("robots") or "").strip(),
    )
    return FirecrawlResponse(status=int(origin_status or 200), page=page)


def _first_h1(markdown: str) -> str:
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""
