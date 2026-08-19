"""The fetch escalation ladder, driven by a stub transport.

No browser is launched here. What is under test is the decision logic: when to
escalate, when to stop, and that a failure on one page does not end the run.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.scrape.fetch import PageFetcher, Tier

REAL_PAGE = """<html><head><title>Real</title></head><body><main>
<h1>Heading</h1><p>This body is comfortably long enough to clear the thin-content
threshold, which is what tells the ladder it can stop climbing and return.</p>
<p>A second paragraph, for good measure, so the extractor keeps the block.</p>
</main></body></html>"""

JS_SHELL = '<html><head><title>App</title></head><body><div id="root"></div></body></html>'


@dataclass
class StubResponse:
    status: int
    html_content: str


class StubFetcher(PageFetcher):
    """Returns a scripted response per tier and records which tiers were tried."""

    def __init__(self, script: dict[Tier, StubResponse | Exception], **kwargs) -> None:
        super().__init__(user_agent="test-agent", **kwargs)
        self.script = script
        self.tried: list[Tier] = []

    async def _raw_fetch(self, tier: Tier, url: str):
        self.tried.append(tier)
        outcome = self.script.get(tier)
        if outcome is None:
            raise RuntimeError(f"no scripted response for {tier}")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def test_a_good_http_response_never_launches_a_browser() -> None:
    """The whole point of the ladder: browsers cost 20x the memory."""
    fetcher = StubFetcher({Tier.HTTP: StubResponse(200, REAL_PAGE)})

    result = await fetcher.fetch("https://e.com/a")

    assert fetcher.tried == [Tier.HTTP]
    assert result.ok
    assert result.tier is Tier.HTTP
    assert "Heading" in result.page.markdown


async def test_js_shell_escalates_to_the_browser_tier() -> None:
    fetcher = StubFetcher(
        {Tier.HTTP: StubResponse(200, JS_SHELL), Tier.DYNAMIC: StubResponse(200, REAL_PAGE)}
    )

    result = await fetcher.fetch("https://e.com/a")

    assert fetcher.tried == [Tier.HTTP, Tier.DYNAMIC]
    assert result.tier is Tier.DYNAMIC
    assert result.ok


async def test_403_escalates_all_the_way_to_stealth() -> None:
    fetcher = StubFetcher(
        {
            Tier.HTTP: StubResponse(403, ""),
            Tier.DYNAMIC: StubResponse(403, ""),
            Tier.STEALTH: StubResponse(200, REAL_PAGE),
        }
    )

    result = await fetcher.fetch("https://e.com/a")

    assert fetcher.tried == [Tier.HTTP, Tier.DYNAMIC, Tier.STEALTH]
    assert result.tier is Tier.STEALTH
    assert result.ok


async def test_404_does_not_escalate() -> None:
    """A page that is genuinely absent must not cost two browser launches."""
    fetcher = StubFetcher({Tier.HTTP: StubResponse(404, "")})

    result = await fetcher.fetch("https://e.com/missing")

    assert fetcher.tried == [Tier.HTTP]
    assert not result.ok
    assert result.error == "HTTP 404"


async def test_an_exception_in_one_tier_moves_to_the_next() -> None:
    fetcher = StubFetcher(
        {Tier.HTTP: TimeoutError("read timeout"), Tier.DYNAMIC: StubResponse(200, REAL_PAGE)}
    )

    result = await fetcher.fetch("https://e.com/a")

    assert result.ok
    assert result.tier is Tier.DYNAMIC


async def test_exhausting_the_ladder_reports_failure_rather_than_raising() -> None:
    fetcher = StubFetcher(
        {
            Tier.HTTP: StubResponse(503, ""),
            Tier.DYNAMIC: StubResponse(503, ""),
            Tier.STEALTH: StubResponse(503, ""),
        }
    )

    result = await fetcher.fetch("https://e.com/a")

    assert not result.ok
    assert len(result.attempts) == 3
    assert fetcher.stats.failed == 1


async def test_browsers_can_be_disabled_entirely() -> None:
    """For a scheduled refresh where a slow, expensive escalation is not wanted."""
    fetcher = StubFetcher({Tier.HTTP: StubResponse(200, JS_SHELL)}, allow_browser=False)

    result = await fetcher.fetch("https://e.com/a")

    assert fetcher.tried == [Tier.HTTP]
    assert not result.ok


async def test_fetch_many_preserves_input_order_and_reports_progress() -> None:
    fetcher = StubFetcher({Tier.HTTP: StubResponse(200, REAL_PAGE)})
    urls = [f"https://e.com/{i}" for i in range(12)]
    seen: list[tuple[int, int]] = []

    results = await fetcher.fetch_many(urls, on_progress=lambda d, t: seen.append((d, t)))

    assert [r.url for r in results] == urls, "results must come back in input order"
    assert seen[-1] == (12, 12)
    assert len(seen) == 12


async def test_stats_break_down_by_tier() -> None:
    """A run's real cost is how many pages needed a browser."""
    fetcher = StubFetcher(
        {Tier.HTTP: StubResponse(200, JS_SHELL), Tier.DYNAMIC: StubResponse(200, REAL_PAGE)}
    )

    await fetcher.fetch_many([f"https://e.com/{i}" for i in range(3)])

    assert fetcher.stats.by_tier == {Tier.DYNAMIC: 3}
    assert fetcher.stats.failed == 0


@pytest.mark.parametrize("status", [401, 403, 429, 503])
async def test_blocking_statuses_all_escalate(status: int) -> None:
    fetcher = StubFetcher(
        {Tier.HTTP: StubResponse(status, ""), Tier.DYNAMIC: StubResponse(200, REAL_PAGE)}
    )

    await fetcher.fetch("https://e.com/a")

    assert Tier.DYNAMIC in fetcher.tried
