"""Telling "the site refused us" apart from "the site has nothing".

Discovery reported both as the same thing: `fetch_robots` returned an empty
`RobotsInfo()` for a 403 exactly as it did for a 404, and the sitemap walk
`continue`d past both. The run then said "No robots.txt found" and "No sitemap
URLs found", which reads as a finding about the client's site.

Measured on nrma.com.au: 403 from the Railway worker and 200 from an Australian
residential address, on the same static `robots.txt`, `server: AkamaiGHost`. Four
runs parked at the review gate with no sitemap and produced a one-page file, and
nothing anywhere said the word "blocked".

The network is faked with `httpx.MockTransport` rather than mocked out entirely,
so these exercise the real request path and the real branch.
"""

from __future__ import annotations

import httpx
import pytest

from app.scrape.discover import collect_sitemap_urls, discover, fetch_robots
from app.scrape.recon import BlockedFetch, RobotsInfo, SiteRecon, name_blocker

ROBOTS_BODY = "User-agent: *\nDisallow: /admin\nSitemap: https://x.example/sitemap.xml\n"
SITEMAP_BODY = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>https://x.example/a</loc></url>"
    "<url><loc>https://x.example/b</loc></url>"
    "</urlset>"
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


# -- naming what refused us ----------------------------------------------------


@pytest.mark.parametrize(
    ("server", "expected"),
    [
        ("AkamaiGHost", "Akamai"),
        ("imperva", "Imperva"),
        ("Incapsula", "Imperva Incapsula"),
        ("cloudflare", "Cloudflare"),
        ("awselb/2.0", "AWS ELB"),
        ("", "unidentified"),
    ],
)
def test_the_blocker_is_named_from_the_server_header(server, expected):
    """A bare 403 does not tell an operator which remedy applies.

    An IP-reputation deny needs a different address; a bot challenge needs a
    different fetcher. The `Server` header is the cheapest signal that separates
    them, and it is the one that identified NRMA's edge as Akamai rather than the
    Imperva everyone assumed.
    """
    assert name_blocker(server) == expected


def test_an_unrecognised_server_is_reported_rather_than_swallowed():
    assert name_blocker("SomeVendorWAF/9") == "SomeVendorWAF/9"


# -- robots.txt ----------------------------------------------------------------


async def test_a_refused_robots_is_recorded_as_a_block():
    def handler(request):
        return httpx.Response(403, headers={"server": "AkamaiGHost"}, text="Access Denied")

    blocked: list[BlockedFetch] = []
    async with _client(handler) as client:
        info = await fetch_robots(client, "https://x.example", blocked)

    assert not info.fetched, "a refusal must not be read as a robots.txt"
    assert len(blocked) == 1
    assert blocked[0].status == 403
    assert blocked[0].blocker == "Akamai"
    assert "403" in blocked[0].describe()


async def test_a_missing_robots_is_not_a_block():
    """404 is the ordinary case and must stay ordinary.

    Flagging it would put a scary warning on every site that simply has no
    robots.txt, which is most small sites.
    """

    def handler(request):
        return httpx.Response(404, text="not found")

    blocked: list[BlockedFetch] = []
    async with _client(handler) as client:
        info = await fetch_robots(client, "https://x.example", blocked)

    assert not info.fetched
    assert blocked == []


async def test_a_served_robots_is_parsed_and_records_nothing():
    def handler(request):
        return httpx.Response(200, text=ROBOTS_BODY, headers={"content-type": "text/plain"})

    blocked: list[BlockedFetch] = []
    async with _client(handler) as client:
        info = await fetch_robots(client, "https://x.example", blocked)

    assert info.fetched
    assert info.sitemaps == ["https://x.example/sitemap.xml"]
    assert blocked == []


# -- the sitemap walk ----------------------------------------------------------


async def test_a_refused_sitemap_is_recorded():
    def handler(request):
        return httpx.Response(403, headers={"server": "cloudflare"})

    blocked: list[BlockedFetch] = []
    async with _client(handler) as client:
        urls, _count, _notes, _members = await collect_sitemap_urls(
            client, ["https://x.example/sitemap.xml"], blocked
        )

    assert urls == {}
    assert [b.blocker for b in blocked] == ["Cloudflare"]


async def test_a_served_sitemap_yields_its_urls_and_records_nothing():
    """The happy path, so the block branch cannot swallow the ordinary one."""

    def handler(request):
        return httpx.Response(200, text=SITEMAP_BODY, headers={"content-type": "application/xml"})

    blocked: list[BlockedFetch] = []
    async with _client(handler) as client:
        urls, count, _notes, _members = await collect_sitemap_urls(
            client, ["https://x.example/sitemap.xml"], blocked
        )

    assert sorted(urls) == ["https://x.example/a", "https://x.example/b"]
    assert count == 1
    assert blocked == []


async def test_a_sitemap_that_is_merely_absent_is_not_a_block():
    def handler(request):
        return httpx.Response(404)

    blocked: list[BlockedFetch] = []
    async with _client(handler) as client:
        await collect_sitemap_urls(client, ["https://x.example/sitemap.xml"], blocked)

    assert blocked == []


# -- what the run is told ------------------------------------------------------


def test_a_wholly_blocked_site_says_so_and_names_the_remedy():
    """The note has to contradict the reading it replaces, not merely soften it.

    Read from the source because `discover` builds its own client and there is no
    seam to inject a transport through. Weaker than driving it, and still worth
    pinning: the exact sentence is the deliverable here, and the old one -- "No
    sitemap URLs found" -- is what got misread for a week.
    """
    import inspect

    source = inspect.getsource(discover)

    assert "Blocked by the site:" in source
    assert "refused every request" in source
    assert "Screaming Frog" in source, "states the problem without the way round it"
    assert "No robots.txt found" in source, "the ordinary unblocked wording was lost"


def test_shut_out_is_blocked_and_empty_handed():
    """Blocked *and* nothing discovered. Either alone is not the same finding."""
    block = BlockedFetch("https://x.example/robots.txt", 403, "AkamaiGHost")

    assert SiteRecon(site_url="x", robots=RobotsInfo(), urls=[], blocked=[block]).shut_out
    assert not SiteRecon(site_url="x", robots=RobotsInfo(), urls=["https://x.example/a"]).shut_out
    # Refused one sitemap, read another: the run proceeds, so this is a warning
    # rather than a failure, and the page count it reports is a floor.
    assert not SiteRecon(
        site_url="x", robots=RobotsInfo(), urls=["https://x.example/a"], blocked=[block]
    ).shut_out


def test_the_summary_says_it_is_our_access_not_their_content():
    """The whole point of the flag: stop attributing our block to the client."""
    recon = SiteRecon(
        site_url="x",
        robots=RobotsInfo(),
        urls=[],
        blocked=[
            BlockedFetch("https://x.example/robots.txt", 403, "AkamaiGHost"),
            BlockedFetch("https://x.example/sitemap.xml", 403, "AkamaiGHost"),
        ],
    )

    summary = recon.blocked_summary()

    assert "Akamai" in summary
    assert "403" in summary
    assert "not a gap on the site" in summary
    assert recon.blockers() == ["Akamai"], "one blocker named once, not per request"


def test_nothing_is_claimed_when_nothing_was_refused():
    assert SiteRecon(site_url="x", robots=RobotsInfo(), urls=["a"]).blocked_summary() == ""


# -- preflight stops rather than planning a crawl of nothing --------------------


def test_preflight_fails_a_blocked_site_before_it_pays_for_a_plan():
    """It used to hand the planner an empty inventory and park at the review gate.

    That invites an operator to approve a crawl of nothing, which is what produced
    nrma.com.au's one-page output. The guard sits after the size check -- "Google
    indexes 924 pages we cannot see" is the evidence that this is a block and not
    an empty site -- and before the two LLM calls, which would be wasted.
    """
    import inspect

    from app.jobs.tasks import preflight_task

    source = inspect.getsource(preflight_task)
    guard = source.split("shut_out", 1)

    assert len(guard) == 2, "preflight no longer checks whether it was shut out"
    before, after = guard
    assert "run_preflight" in before, "the guard runs before discovery"
    assert "classify_groups" not in before, "the LLM spend happens before the guard"
    assert "RunStatus.FAILED" in after
    assert "Screaming Frog" in after, "fails without naming the way round it"
