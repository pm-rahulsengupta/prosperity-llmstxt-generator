"""Whether a sitemap actually leads anywhere.

The readiness check for `/sitemap.xml` asked two questions -- did it answer below
400, and was it served as XML -- and then passed. It never opened the file. The
component is titled "live and reachable" and the check verified neither.

`tests/fixtures/opencorp_sitemap.xml` is the real file from opencorp.com.au,
fetched 2026-08-27, and it is why this exists. It answers `200 text/xml`, it is
a valid `<sitemapindex>`, and all thirteen children point at
`ocnewstg.staging.tempurl.host` -- a staging host returning `401 Password
Protected` whose robots.txt is `Disallow: /`. A WordPress sitemap generated on
staging and never rewritten for production. Zero production URLs are reachable
through it, and the client's checklist said Published.

That is worse than an absent sitemap. An absent one is visible. This one passes
every surface test while a crawler following it collects thirteen 401s, and it
publishes an internal staging hostname to anyone who reads it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.scrape.sitemap_check import judge_sitemap, same_site

FIXTURES = Path(__file__).parent / "fixtures"
OPENCORP = (FIXTURES / "opencorp_sitemap.xml").read_text(encoding="utf-8")

URLSET = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://x.example/</loc></url>
  <url><loc>https://x.example/about</loc></url>
</urlset>"""


def index_of(*locs: str) -> str:
    entries = "".join(f"<sitemap><loc>{loc}</loc></sitemap>" for loc in locs)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{entries}</sitemapindex>"
    )


# -- the case this exists for ------------------------------------------------------


def test_the_real_opencorp_sitemap_fails():
    """The measured defect, against the bytes the site actually serves."""
    verdict = judge_sitemap(OPENCORP, "https://opencorp.com.au")

    assert verdict.ok is False
    assert verdict.url_count == 0


def test_the_failure_names_the_host_it_points_at():
    """ "Broken sitemap" sends someone hunting. The staging hostname is the fix."""
    verdict = judge_sitemap(OPENCORP, "https://opencorp.com.au")

    assert "ocnewstg.staging.tempurl.host" in verdict.detail
    assert "13" in verdict.detail


def test_www_does_not_change_the_verdict():
    """The client is on file as opencorp.com.au; the sitemap is served on both."""
    assert judge_sitemap(OPENCORP, "https://www.opencorp.com.au").ok is False


# -- what must still pass ------------------------------------------------------------


def test_a_normal_urlset_passes_and_counts():
    verdict = judge_sitemap(URLSET, "https://x.example")

    assert verdict.ok is True
    assert verdict.url_count == 2


def test_an_index_whose_children_are_on_this_site_passes():
    verdict = judge_sitemap(index_of("https://x.example/page-sitemap.xml"), "https://x.example")

    assert verdict.ok is True


def test_a_subdomain_counts_as_the_same_site():
    """`assets.example.com` serving a sitemap for `example.com` is ordinary.

    Only a wholly different registrable name is the finding -- and
    `ocnewstg.staging.tempurl.host` is not a subdomain of `opencorp.com.au` by
    any reading.
    """
    verdict = judge_sitemap(index_of("https://cdn.x.example/s.xml"), "https://x.example")

    assert verdict.ok is True


@pytest.mark.parametrize(
    "url,site",
    [
        ("https://x.example/a", "https://www.x.example"),
        ("https://www.x.example/a", "https://x.example"),
        ("https://X.Example/a", "https://x.example"),
        ("https://x.example:443/a", "https://x.example"),
    ],
)
def test_host_comparison_ignores_www_case_and_port(url, site):
    assert same_site(url, site)


def test_a_genuinely_different_host_is_not_the_same_site():
    assert not same_site("https://evil.example/a", "https://x.example")
    assert not same_site("https://x.example.evil.com/a", "https://x.example")


# -- the other ways a sitemap leads nowhere ------------------------------------------


def test_an_empty_sitemap_fails():
    empty = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'

    assert judge_sitemap(empty, "https://x.example").ok is False


def test_something_that_is_not_a_sitemap_fails():
    """Valid XML that is not a sitemap is the same finding for a crawler."""
    assert judge_sitemap("<rss><channel/></rss>", "https://x.example").ok is False


def test_unparseable_xml_fails():
    assert judge_sitemap("<broken", "https://x.example").ok is False
    assert judge_sitemap("", "https://x.example").ok is False


def test_a_urlset_pointing_entirely_offsite_fails():
    offsite = (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://other.example/a</loc></url></urlset>"
    )

    assert judge_sitemap(offsite, "https://x.example").ok is False


# -- following the children ------------------------------------------------------------


def test_children_that_are_all_empty_fail():
    """On-host but leading nowhere. Only following them can catch this."""
    index = index_of("https://x.example/a.xml")
    children = {
        "https://x.example/a.xml": '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>'
    }

    assert judge_sitemap(index, "https://x.example", children).ok is False


def test_children_that_list_pages_pass_with_a_count():
    index = index_of("https://x.example/a.xml")
    verdict = judge_sitemap(index, "https://x.example", {"https://x.example/a.xml": URLSET})

    assert verdict.ok is True
    assert verdict.url_count == 2


def test_unfetchable_children_are_reported_as_such_not_as_empty():
    """ "We could not open them" and "they are empty" are different findings."""
    index = index_of("https://x.example/a.xml")
    verdict = judge_sitemap(index, "https://x.example", {"https://x.example/a.xml": None})

    assert verdict.ok is False
    assert "could not be fetched" in verdict.detail


def test_not_following_the_children_says_so_rather_than_claiming_a_count():
    """The absent-vs-empty rule, applied to our own effort.

    `children=None` means they were never opened. Returning a count of zero
    would claim we looked and found nothing.
    """
    verdict = judge_sitemap(index_of("https://x.example/a.xml"), "https://x.example")

    assert verdict.url_count is None
    assert "not opened" in verdict.detail


# -- wiring ----------------------------------------------------------------------------


def test_the_readiness_check_opens_the_sitemap():
    """It used to decide on status and content type alone.

    A 200 served as XML told it nothing about whether the file leads anywhere,
    and that is exactly how opencorp.com.au was marked Published.
    """
    import inspect

    from app.scrape import readiness

    source = inspect.getsource(readiness.audit_readiness)

    assert "judge_sitemap(" in source, "the sitemap check no longer opens the file"
    assert 'item.key == "sitemap"' in source


def test_following_children_is_bounded():
    """An audit must not turn into a crawl of every sitemap a large site has."""
    from app.scrape.readiness import MAX_SITEMAP_CHILDREN

    assert 0 < MAX_SITEMAP_CHILDREN <= 5
