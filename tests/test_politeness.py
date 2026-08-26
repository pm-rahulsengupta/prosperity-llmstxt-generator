"""Obeying the crawl rules a site publishes about itself.

`recon.py` parsed `Crawl-delay` and `Disallow` from the beginning and used
neither: both were read, printed into one summary line for the operator, and then
ignored while the crawler ran at full speed.

Measured on nrma.com.au, which is where this came from. Its robots.txt is a stock
Drupal 7 file with one line added -- `Crawl-delay: 10` -- and the site was
reported to us as having "crawl restrictions". It has no such thing: plain HTTP
returns 200 with the full page. It asked to be crawled once every ten seconds and
got eight concurrent fetchers, which is 80x its stated rate. The throttling that
produces arrives as 403/429, which the ladder in `fetch.py` reads as "try harder"
and escalates to a browser -- so ignoring the delay made the load worse and the
result worse at the same time.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from pathlib import Path

import pytest

from app.scrape.politeness import (
    MAX_DELAY_SECONDS,
    Politeness,
    path_is_allowed,
    split_disallowed,
)

FIXTURES = Path(__file__).parent / "fixtures"

# Trimmed from the live file, keeping one of each shape it actually uses.
NRMA_DISALLOWED = [
    "/includes/",
    "/admin/",
    "/get-quote",
    "/search",
    "/taxonomy",
    "/taxonomy/*",
    "/chinese?*",
    "/sites/nrma/files/nrma/policy_booklets/*",
]


# -- the delay ------------------------------------------------------------------


def test_no_published_delay_means_no_pacing():
    """Every site without a `Crawl-delay` must be unaffected by this.

    The gate sits in the fetch path unconditionally, so "not published" has to be
    genuinely free rather than a small tax on every page of every crawl.
    """
    polite = Politeness.from_robots(None)

    assert not polite.applies
    assert polite.estimate_seconds(400) == 0


@pytest.mark.parametrize("published", [0, 0.0, None, -5])
def test_absent_or_nonsense_values_do_not_pace(published):
    assert not Politeness.from_robots(published).applies


def test_a_published_delay_is_honoured():
    assert Politeness.from_robots(10).delay == 10


def test_an_absurd_delay_is_capped_and_says_so():
    """Some sites ship `Crawl-delay: 3600` from a template.

    Honouring it literally is one page an hour and a crawl that never finishes.
    Capped rather than ignored, and the cap is recorded so the operator is told
    we are not doing exactly what the file asked.
    """
    polite = Politeness.from_robots(3600)

    assert polite.delay == MAX_DELAY_SECONDS
    assert polite.capped_from == 3600


async def test_requests_are_spaced_by_the_delay():
    polite = Politeness(delay=0.05)
    started = time.monotonic()

    await asyncio.gather(*(polite.wait() for _ in range(4)))

    # Four slots at 0.05s: the first is immediate, so three gaps.
    assert time.monotonic() - started >= 0.15 - 0.01


async def test_concurrency_does_not_defeat_the_delay():
    """The whole point. Eight fetchers must not mean eight simultaneous hits.

    `Crawl-delay` is a statement about how often a *host* is willing to be hit,
    so the gate is a lock and a timestamp rather than a semaphore.
    """
    # 50ms, not 5. Windows' monotonic clock has ~15.6ms granularity, so a delay
    # near it produces stamps that collapse onto the same tick and gaps of
    # exactly 0.0 -- a measurement artefact that reads as a broken gate.
    polite = Politeness(delay=0.05)
    stamps: list[float] = []

    async def hit():
        await polite.wait()
        stamps.append(time.monotonic())

    await asyncio.gather(*(hit() for _ in range(6)))
    stamps.sort()
    gaps = [b - a for a, b in itertools.pairwise(stamps)]

    assert all(gap >= 0.04 for gap in gaps), gaps


async def test_the_lock_is_not_held_across_the_sleep():
    """Holding it would serialise every waiter behind the whole queue.

    Ten waiters at 0.05s should finish in about 0.5s. If each waited for the lock
    *and then* slept while holding it, the last would wait for the sum of every
    sleep before it and the total would be far worse.
    """
    polite = Politeness(delay=0.05)
    started = time.monotonic()

    await asyncio.gather(*(polite.wait() for _ in range(10)))

    assert time.monotonic() - started < 1.2


def test_the_estimate_is_what_the_operator_is_shown():
    """400 pages at 10s is 66 minutes, which changes what you decide to do."""
    polite = Politeness.from_robots(10)

    assert polite.estimate_seconds(400) == pytest.approx(3990)


# -- the disallow list -----------------------------------------------------------


def test_a_plain_prefix_blocks_its_subtree():
    assert not path_is_allowed("/admin/config", NRMA_DISALLOWED)
    assert not path_is_allowed("/get-quote", NRMA_DISALLOWED)


def test_an_unrelated_path_is_untouched():
    assert path_is_allowed("/car-insurance/comprehensive", NRMA_DISALLOWED)
    assert path_is_allowed("/blog", NRMA_DISALLOWED)


def test_a_wildcard_rule_matches_below_it():
    assert not path_is_allowed("/taxonomy/term/17", NRMA_DISALLOWED)
    assert not path_is_allowed("/sites/nrma/files/nrma/policy_booklets/x.pdf", NRMA_DISALLOWED)


def test_an_empty_disallow_allows_everything():
    """`Disallow:` with no value is the documented way to say "allow all".

    Treating it as a prefix match on the empty string would block every URL on
    the site and the crawl would return nothing, with the cause invisible.
    """
    assert path_is_allowed("/anything", ["", "   "])


def test_a_dollar_anchored_rule_matches_only_the_exact_end():
    assert not path_is_allowed("/x.css", ["/*.css$"])
    assert path_is_allowed("/x.css?v=2", ["/*.css$"])


def test_no_rules_means_no_filtering():
    urls = ["https://x.example/a", "https://x.example/b"]

    assert split_disallowed(urls, []) == (urls, {})


def test_what_is_dropped_is_counted_per_rule():
    """A URL dropped silently is indistinguishable from one the crawler failed on.

    Shaped after `split_embargoed`, which exists for the same reason: an operator
    has to be able to answer "why is this page missing".
    """
    urls = [
        "https://www.nrma.com.au/car-insurance",
        "https://www.nrma.com.au/get-quote",
        "https://www.nrma.com.au/admin/config",
        "https://www.nrma.com.au/taxonomy/term/9",
        "https://www.nrma.com.au/blog",
    ]

    kept, counts = split_disallowed(urls, NRMA_DISALLOWED)

    assert kept == ["https://www.nrma.com.au/car-insurance", "https://www.nrma.com.au/blog"]
    # `/taxonomy/*`, not `/taxonomy`: both match a term page and the longest is
    # the one that decided, so it is the one an operator is sent to.
    assert counts == {"/get-quote": 1, "/admin/": 1, "/taxonomy/*": 1}


def test_a_url_with_no_path_is_treated_as_the_root():
    assert split_disallowed(["https://x.example"], ["/admin/"]) == (["https://x.example"], {})


# -- wiring ----------------------------------------------------------------------


def test_the_fetcher_waits_before_the_first_attempt_not_on_each_tier():
    """A page that escalates HTTP -> browser -> stealth is one page.

    Charging it three delays would triple the crawl for exactly the pages that
    are already slowest, and the rate limit is on pages -- which is what the site
    sees.
    """
    import inspect

    from app.scrape.fetch import PageFetcher

    source = inspect.getsource(PageFetcher.fetch)
    before_loop = source.split("for tier in self._ladder():", 1)[0]

    assert "await self.politeness.wait()" in before_loop
    assert "politeness" not in inspect.getsource(PageFetcher._attempt)


def test_the_crawl_stage_applies_both_rules():
    import inspect

    from app.jobs import tasks

    source = inspect.getsource(tasks.generate_task)

    assert "split_disallowed" in source, "the Disallow list is parsed and ignored again"
    assert "Politeness.from_robots" in source, "the Crawl-delay is parsed and ignored again"


def test_the_operator_is_told_what_the_delay_will_cost():
    """66 minutes for 400 pages changes what you decide to do, so it is said."""
    import inspect

    from app.jobs import tasks

    source = inspect.getsource(tasks.generate_task)

    assert "estimate_seconds" in source
    assert "record_event" in source


# -- against the real file ---------------------------------------------------------
#
# `tests/fixtures/nrma_robots.txt` is nrma.com.au's actual robots.txt, fetched
# 2026-08-26 and trimmed to keep one of every rule shape it uses. The hand-written
# lists above prove the matcher; this proves the parser and the matcher agree with
# what a real site publishes, which is the join where a fix like this goes inert.


def _nrma_robots():
    from app.scrape.recon import parse_robots

    return parse_robots((FIXTURES / "nrma_robots.txt").read_text(encoding="utf-8"))


def test_the_parser_finds_the_delay_that_started_all_this():
    """If this returns None the whole feature is inert and nothing else fails."""
    assert _nrma_robots().crawl_delay == 10.0


def test_the_delay_reaches_the_gate():
    polite = Politeness.from_robots(_nrma_robots().crawl_delay)

    assert polite.applies
    assert polite.estimate_seconds(400) / 60 == pytest.approx(66.5)


def test_the_parser_finds_both_rule_kinds():
    robots = _nrma_robots()

    assert "/get-quote" in robots.disallowed
    assert "/misc/*.css$" in robots.allowed, "Allow lines were dropped on the floor"
    assert robots.sitemaps == ["https://www.nrma.com.au/sitemap.xml"]


def test_an_allow_inside_a_blocked_directory_survives():
    """`Disallow: /misc/` with `Allow: /misc/*.css$` above it.

    The first version of `path_is_allowed` read `Disallow` only, so the explicitly
    permitted path was silently skipped -- dropping pages a client allowed on
    purpose.
    """
    robots = _nrma_robots()

    assert path_is_allowed("/misc/style.css", robots.disallowed, robots.allowed)
    assert not path_is_allowed("/misc/secret.txt", robots.disallowed, robots.allowed)


def test_the_pages_worth_crawling_are_untouched():
    """The check that this does not quietly gut a real crawl."""
    robots = _nrma_robots()
    urls = [
        "https://www.nrma.com.au/",
        "https://www.nrma.com.au/car-insurance",
        "https://www.nrma.com.au/car-insurance/comprehensive",
        "https://www.nrma.com.au/home-insurance",
        "https://www.nrma.com.au/blog",
        "https://www.nrma.com.au/llm-info",
        "https://www.nrma.com.au/get-quote",
        "https://www.nrma.com.au/taxonomy/term/12",
        "https://www.nrma.com.au/cron.php",
    ]

    kept, counts = split_disallowed(urls, robots.disallowed, robots.allowed)

    assert "https://www.nrma.com.au/llm-info" in kept
    assert len(kept) == 6
    assert counts == {"/get-quote": 1, "/taxonomy/*": 1, "/cron.php": 1}


def test_the_rule_named_in_the_report_is_the_one_that_decided():
    """Longest match, not first in file order.

    `/taxonomy` and `/taxonomy/*` both match a term page; naming the weaker one
    would send an operator to the wrong line of the file.
    """
    kept, counts = split_disallowed(
        ["https://www.nrma.com.au/taxonomy/term/12"], ["/taxonomy", "/taxonomy/*"]
    )

    assert kept == []
    assert counts == {"/taxonomy/*": 1}
