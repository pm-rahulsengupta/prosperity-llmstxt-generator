"""The Firecrawl fallback rung: response mapping, and where it sits in the ladder."""

from __future__ import annotations

import pytest

from app.scrape.fetch import FetchResult, PageFetcher, Tier
from app.scrape.firecrawl import FirecrawlFetcher, FirecrawlResponse, parse_scrape_response

SCRAPE_BODY = {
    "success": True,
    "data": {
        "markdown": "# Quick Start\n\nGet running in five minutes. " + ("word " * 60),
        "metadata": {
            "title": "Quick Start Guide | Example",
            "description": "Get started with Example in five minutes.",
            "sourceURL": "https://example.com/docs/quickstart",
            "statusCode": 200,
            "canonical": "https://example.com/docs/quickstart",
        },
    },
}


def test_parses_a_successful_scrape():
    result = parse_scrape_response(SCRAPE_BODY, "https://example.com/docs/quickstart")
    assert result.status == 200
    assert result.page is not None
    assert result.page.title == "Quick Start Guide | Example"
    assert result.page.h1 == "Quick Start"
    assert result.page.canonical.endswith("/docs/quickstart")
    assert result.page.word_count > 50
    assert not result.page.is_thin


def test_origin_status_is_read_from_metadata_not_the_api_call():
    """Firecrawl answers 200 even when the target site answered 403."""
    body = {
        "success": True,
        "data": {"markdown": "x" * 400, "metadata": {"statusCode": 403}},
    }
    assert parse_scrape_response(body, "https://example.com/").status == 403


def test_empty_markdown_is_a_failure_not_a_thin_page():
    body = {"success": True, "data": {"markdown": "", "metadata": {"statusCode": 200}}}
    result = parse_scrape_response(body, "https://example.com/")
    assert result.page is None
    assert "empty markdown" in result.error


def test_unsuccessful_payload_carries_the_reason():
    result = parse_scrape_response({"success": False, "error": "url is not allowed"}, "u")
    assert result.page is None
    assert "url is not allowed" in result.error


def test_no_key_means_disabled():
    assert FirecrawlFetcher(api_key="").enabled is False
    assert FirecrawlFetcher(api_key="fc-abc").enabled is True


# --- ladder placement ------------------------------------------------------


class StubFirecrawl(FirecrawlFetcher):
    def __init__(self, response: FirecrawlResponse) -> None:
        super().__init__(api_key="fc-test")
        self.response = response
        self.calls: list[str] = []

    async def scrape(self, url: str) -> FirecrawlResponse:
        self.calls.append(url)
        return self.response


def _blocked(tier: Tier, url: str) -> FetchResult:
    return FetchResult(url=url, status=403, tier=tier, error="HTTP 403")


@pytest.fixture
def blocked_everywhere(monkeypatch):
    """Every Scrapling tier returns 403, so the ladder runs to its end."""

    async def attempt(self, tier: Tier, url: str) -> FetchResult:
        if tier is Tier.FIRECRAWL:
            return await self._attempt_firecrawl(url)
        return _blocked(tier, url)

    monkeypatch.setattr(PageFetcher, "_attempt", attempt)


async def test_firecrawl_is_absent_from_the_ladder_without_a_key():
    fetcher = PageFetcher(user_agent="t")
    assert Tier.FIRECRAWL not in fetcher._ladder()

    fetcher = PageFetcher(user_agent="t", firecrawl=FirecrawlFetcher(api_key=""))
    assert Tier.FIRECRAWL not in fetcher._ladder()


async def test_firecrawl_runs_last_and_recovers_a_blocked_page(blocked_everywhere):
    page = parse_scrape_response(SCRAPE_BODY, "https://example.com/docs/quickstart").page
    stub = StubFirecrawl(FirecrawlResponse(status=200, page=page))
    fetcher = PageFetcher(user_agent="t", firecrawl=stub)

    result = await fetcher.fetch("https://example.com/docs/quickstart")

    assert result.ok
    assert result.tier is Tier.FIRECRAWL
    # Tried, in order, and only paid for the page the free tiers could not get.
    assert [a.split(":")[0] for a in result.attempts] == [
        "http",
        "dynamic",
        "stealth",
        "firecrawl",
    ]
    assert stub.calls == ["https://example.com/docs/quickstart"]


async def test_out_of_credits_disables_the_rung_for_the_rest_of_the_run(blocked_everywhere):
    stub = StubFirecrawl(
        FirecrawlResponse(status=402, page=None, error="firecrawl: out of credits")
    )
    fetcher = PageFetcher(user_agent="t", firecrawl=stub)

    await fetcher.fetch("https://example.com/a")
    await fetcher.fetch("https://example.com/b")
    await fetcher.fetch("https://example.com/c")

    # Charged once for the account-level failure, then stopped asking.
    assert stub.calls == ["https://example.com/a"]
    assert "out of credits" in fetcher.firecrawl_disabled_reason
    assert Tier.FIRECRAWL not in fetcher._ladder()


async def test_a_firecrawl_page_failure_does_not_disable_the_rung(blocked_everywhere):
    stub = StubFirecrawl(FirecrawlResponse(status=500, page=None, error="firecrawl: HTTP 500"))
    fetcher = PageFetcher(user_agent="t", firecrawl=stub)

    await fetcher.fetch("https://example.com/a")
    await fetcher.fetch("https://example.com/b")

    assert stub.calls == ["https://example.com/a", "https://example.com/b"]
    assert fetcher.firecrawl_disabled_reason == ""
