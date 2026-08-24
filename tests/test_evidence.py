"""The evidence the rules are allowed to treat as fact.

Most of these guard the sentinel described in `app/core/evidence.py`: passing
`None` where `[]` was meant disables AGT-004 and nothing reports that it stopped
running. That is the failure this file exists to make loud.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.core.evidence import Evidence, evidence_for
from app.core.rules import audit_agents
from app.scrape.agents_probe import Surface, SurfaceState


def view(
    *,
    verified=(),
    endpoints=(),
    declared=(),
    sells=False,
    agents_md: Surface | None = None,
    site_url="https://x.example",
):
    return SimpleNamespace(
        site_url=site_url,
        probe=SimpleNamespace(verified_endpoints=tuple(verified), agents_md=agents_md),
        tech=SimpleNamespace(endpoint_urls=list(endpoints), sells=sells),
        bundle=SimpleNamespace(verified_endpoints=list(declared)),
    )


# -- the sentinel -------------------------------------------------------------


def test_a_probe_that_confirmed_nothing_still_counts_as_having_run():
    """`[]` and `None` are opposite claims, and AGT-004 branches on which.

    A site whose probe found no endpoints has evidence: it has been checked and
    there is nothing. Every URL in a file about it is therefore unverified. A
    site with no probe at all has no evidence either way, and condemning its
    file would be inventing a finding.
    """
    checked = evidence_for(view())

    assert checked.verified_urls is not None
    assert checked.as_list == ["https://x.example"], "its own origin, and nothing else"


def test_no_view_at_all_means_no_probe_ran():
    assert evidence_for(None).as_list is None


def test_the_sentinel_reaches_the_rule_in_both_directions():
    """The whole point. Wire this backwards and AGT-004 silently stops enforcing."""
    body = "# X\n\nSee https://not-verified.example/api for details.\n"

    ran = audit_agents(body, site_url="https://x.example", verified_urls=[])
    never = audit_agents(body, site_url="https://x.example", verified_urls=None)

    assert ran.failed("AGT-004"), "a probe that found nothing must condemn a stray URL"
    assert not never.failed("AGT-004"), "no probe means no verdict, not a pass"
    assert never.by_id("AGT-004").outcome.value == "skipped"


# -- what counts as verified --------------------------------------------------


def test_the_sites_own_origin_is_always_verified():
    assert "https://x.example" in evidence_for(view()).as_list


def test_ucp_endpoints_and_answering_surfaces_are_verified():
    ev = evidence_for(
        view(
            verified=("https://mcp.x.example/api",),
            endpoints=["https://x.example/wp-json"],
        )
    )

    assert "https://mcp.x.example/api" in ev.as_list
    assert "https://x.example/wp-json" in ev.as_list


def test_a_declared_endpoint_counts_only_once_verify_declared_confirmed_it():
    """`Bundle.verified_endpoints` already filters on `DeclaredEndpoint.verified`.

    An operator naming their own MCP server is how we learn of it, and also the
    easiest way a typo or a decommissioned host reaches a published file.
    """
    ev = evidence_for(view(declared=["https://x.example/mcp"]))

    assert "https://x.example/mcp" in ev.as_list


def test_a_trailing_slash_is_not_a_different_endpoint():
    ev = evidence_for(view(endpoints=["https://x.example/api/"]))

    assert (
        audit_agents(
            "# X\n\nCall https://x.example/api for data.\n",
            site_url="https://x.example",
            verified_urls=ev.as_list,
        ).failed("AGT-004")
        is False
    )


def test_evidence_cannot_be_widened_after_the_fact():
    """A rule that could add to its own evidence would be no check at all."""
    import dataclasses

    import pytest

    ev = evidence_for(view())
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.site_url = "https://other.example"


# -- the modifiers ------------------------------------------------------------


def test_whether_the_site_sells_comes_from_the_platform_probe():
    assert evidence_for(view(sells=True)).transactional is True
    assert evidence_for(view(sells=False)).transactional is False


def test_a_site_publishing_no_agents_md_has_no_content_type():
    """Distinct from one served with the wrong type, which is a different finding."""
    assert evidence_for(view()).content_type is None


def test_a_published_agents_md_carries_the_type_it_was_served_as():
    served = Surface(
        url="https://x.example/agents.md",
        state=SurfaceState.PRESENT,
        status=200,
        content_type="text/markdown",
    )

    assert evidence_for(view(agents_md=served)).content_type == "text/markdown"


def test_an_agents_md_served_with_no_type_is_none_not_empty_string():
    served = Surface(url="https://x.example/agents.md", state=SurfaceState.PRESENT, status=200)

    assert evidence_for(view(agents_md=served)).content_type is None


# -- what is deliberately excluded --------------------------------------------


def test_evidence_is_a_closed_set():
    """Five sources, each justified. A sitemap entry is not one of them.

    A sitemap is a claim a site makes about itself that nobody has checked, and
    admitting it would let a file assert a URL no request ever touched.
    """
    ev = evidence_for(
        view(
            verified=("https://a.example",),
            endpoints=["https://b.example"],
            declared=["https://c.example"],
        )
    )

    assert set(ev.as_list) == {
        "https://x.example",
        "https://a.example",
        "https://b.example",
        "https://c.example",
    }


def test_a_crawled_page_is_evidence_for_a_link():
    """The correction the rule engine forced, the first time it ran on real output.

    AGT-004 condemned twelve URLs in our own generated agents.md, all twelve of
    them pages a completed crawl had fetched. "A URL existed when we fetched it"
    is weaker than "this endpoint answers as advertised" -- and that distinction
    governs an endpoint an agent will *call*, not a page it will *read*.
    `_assemble` had already drawn the line correctly.
    """
    crawled = SimpleNamespace(
        site_url="https://x.example",
        probe=SimpleNamespace(verified_endpoints=(), agents_md=None),
        tech=SimpleNamespace(endpoint_urls=[], sells=False),
        bundle=SimpleNamespace(verified_endpoints=[]),
        crawled_urls=("https://x.example/a-blog-post/",),
    )

    ev = evidence_for(crawled)

    assert "https://x.example/a-blog-post" in ev.as_list
    assert not audit_agents(
        "# X\n\nRead https://x.example/a-blog-post/ for background.\n",
        site_url="https://x.example",
        verified_urls=ev.as_list,
    ).failed("AGT-004")


def test_empty_strings_never_reach_the_verified_set():
    ev = evidence_for(view(verified=("",), endpoints=[""], declared=[""]))

    assert ev.as_list == ["https://x.example"]


def test_evidence_is_the_shape_audit_agents_wants():
    ev = Evidence(site_url="https://x.example", verified_urls=("https://x.example",))

    report = audit_agents(
        "# X\n\nHome: https://x.example\n",
        site_url=ev.site_url,
        verified_urls=ev.as_list,
        transactional=ev.transactional,
        content_type=ev.content_type,
    )

    assert not report.failed("AGT-004")
