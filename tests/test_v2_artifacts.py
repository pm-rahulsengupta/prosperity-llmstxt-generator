"""Markdown page versions, the AI info page, and the OKF bundle.

Three artifacts added together because one document asked for all three, but
they fail in different ways and the tests are grouped by that rather than by
module. The recurring property under test is the one this codebase keeps
relearning: **nothing is advertised that was not produced.** A link to a
markdown twin that does not exist, an `ai-info` page listing an `llms.txt` the
client never publishes, an OKF concept nothing links to -- each is the same bug
wearing a different file extension.
"""

from __future__ import annotations

from datetime import date

import pytest
from lxml import html as lxml_html

from app.core.ai_info import render_ai_info
from app.core.bundle import render_headers
from app.core.md_pages import (
    REPLACE,
    SUFFIX,
    layout_for,
    md_path_for,
    render_md_pages,
    rewrite_links,
)
from app.core.models import PageEntry, Section
from app.core.okf import frontmatter, render_okf, slug
from app.core.onboarding import Fact, SiteBrief
from app.core.rules import audit_ai_info, audit_markdown_pages, audit_okf

#: Long enough to clear MD-003's floor. A shorter body is a legitimate finding,
#: not a fixture convenience, so the fixture carries a real page's worth of text.
_BODY = (
    "Red Spot rents vehicles from airport and city counters across Australia. "
    "Rates, insurance options and the terms that apply to each class are set out "
    "on this page, along with the documents a driver has to present at pickup."
)


def _page(url: str, title: str = "T", body: str = _BODY) -> PageEntry:
    return PageEntry(url=url, title=title, description="A description.", markdown=body)


# -- markdown page versions ----------------------------------------------------


@pytest.mark.parametrize(
    ("url", "layout", "expected"),
    [
        ("https://x.example/about/", REPLACE, "/about/index.md"),
        ("https://x.example/about/", SUFFIX, "/about/index.html.md"),
        ("https://x.example/a/b.html", REPLACE, "/a/b.md"),
        ("https://x.example/a/b.html", SUFFIX, "/a/b.html.md"),
        ("https://x.example/", REPLACE, "/index.md"),
        # No extension and no trailing slash is still a directory URL, because
        # that is what a router serving `/pricing` is doing.
        ("https://x.example/pricing", REPLACE, "/pricing/index.md"),
    ],
)
def test_the_markdown_twin_sits_at_a_path_an_agent_can_guess(url, layout, expected):
    assert md_path_for(url, layout) == expected


def test_the_naming_convention_follows_what_the_site_already_serves():
    """A site of `.html` URLs is being served file-for-file; suffix fits it."""
    explicit = [_page(f"https://x.example/{n}.html") for n in "abc"]
    routed = [_page(f"https://x.example/{n}/") for n in "abc"]

    assert layout_for(explicit) == SUFFIX
    assert layout_for(routed) == REPLACE
    assert layout_for([]) == REPLACE


def test_a_page_with_no_crawled_body_is_skipped_rather_than_published_empty():
    """An empty file and a page we failed to read look identical to an agent."""
    pages = [_page("https://x.example/a/"), PageEntry(url="https://x.example/b/", title="B")]

    out = render_md_pages(pages)

    assert out.count == 1
    assert out.skipped == ["https://x.example/b/"]


def test_two_urls_wanting_one_path_keep_the_first_and_record_the_second():
    """Overwriting would leave the count right and the contents wrong."""
    pages = [_page("https://x.example/a/"), _page("https://x.example/a")]

    out = render_md_pages(pages, REPLACE)

    assert out.count == 1
    assert out.skipped == ["https://x.example/a"]


def test_every_markdown_file_states_the_url_it_came_from():
    """Fetched on its own, it has nothing to attribute a quote to otherwise."""
    out = render_md_pages([_page("https://x.example/a/")])

    assert "Source: https://x.example/a/" in out.files["a/index.md"]


def test_a_link_is_rewritten_only_where_the_markdown_will_exist():
    """The rule the whole module turns on. A `.md` link on a site that never
    publishes the directory is worse than the HTML link it replaced."""
    pages = [_page("https://x.example/a/")]
    out = render_md_pages(pages, REPLACE)
    llms = (
        "# X\n\n> s\n\n## S\n\n- [A](https://x.example/a/): one\n- [B](https://x.example/b/): two\n"
    )

    rewritten = rewrite_links(llms, out.paths, "https://x.example")

    assert "https://x.example/a/index.md" in rewritten
    assert "https://x.example/b/" in rewritten, "no twin was generated, so it is left alone"


def test_a_link_to_another_domain_is_never_rewritten():
    pages = [_page("https://x.example/a/")]
    out = render_md_pages(pages, REPLACE)
    llms = "- [Other](https://other.example/a/): x\n"

    assert rewrite_links(llms, out.paths, "https://x.example") == llms


def test_mixing_the_two_naming_conventions_is_an_error():
    """No single host rule serves both, so half the links 404 after publishing."""
    report = audit_markdown_pages(
        {
            "a/index.md": "# A\n\nSource: u\n\n" + "x" * 100,
            "b.html.md": "# B\n\nSource: u\n\n" + "y" * 100,
        }
    )

    assert report.by_id("MD-004").outcome.value == "fail"


def test_the_generated_directory_passes_its_own_rules():
    pages = [_page(f"https://x.example/{n}/") for n in "abcde"]

    report = audit_markdown_pages(render_md_pages(pages).files)

    assert report.failures == [], [f.rule_id for f in report.failures]


# -- the AI info page ----------------------------------------------------------


def _brief(**kw) -> SiteBrief:
    facts = {
        "Founded": Fact("1998", "https://x.example/about/"),
        "Head office": Fact("Sydney", "https://x.example/about/"),
        "Fleet": Fact("400 vehicles", "https://x.example/about/"),
    }
    return SiteBrief(facts=facts, audience="Travellers", **kw)


def _render(**kw):
    defaults = dict(
        site_url="https://x.example",
        site_name="X Ltd",
        site_summary="A car rental company.",
        brief=_brief(),
        sections=[Section(name="S", pages=[_page("https://x.example/a/")])],
        verified_urls=frozenset({"https://x.example/about/", "https://x.example/a/"}),
        published=frozenset({"/llms.txt"}),
        generated_on=date(2026, 9, 9),
    )
    defaults.update(kw)
    return render_ai_info(**defaults)


def test_a_fact_carries_the_source_it_came_from():
    """A claim we cannot attribute cannot be defended when the client disputes it."""
    doc = lxml_html.fromstring(_render().html)

    sources = doc.xpath("//dd[@class='src']//a/@href")
    assert "https://x.example/about/" in sources


def test_a_source_the_probe_never_saw_is_stated_without_being_linked():
    """The claim is still the operator's; inventing a link to it is the failure."""
    brief = SiteBrief(facts={"Founded": Fact("1998", "https://elsewhere.example/x")})

    doc = lxml_html.fromstring(_render(brief=brief).html)

    assert doc.xpath("//dd[@class='src']//a") == []
    assert "https://elsewhere.example/x" in doc.text_content()


def test_only_the_machine_readable_files_the_bundle_produced_are_linked():
    """The same rule `render_headers` follows, on a page instead of a header."""
    doc = lxml_html.fromstring(_render(published=frozenset({"/llms.txt"})).html)

    hrefs = doc.xpath("//a/@href")
    assert "https://x.example/llms.txt" in hrefs
    assert "https://x.example/agents.md" not in hrefs


def test_a_title_carrying_markup_is_inert_on_the_published_page():
    page = _render(site_name="<script>alert(1)</script>X")

    doc = lxml_html.fromstring(page.html)

    assert doc.xpath("//script") == []


def test_a_page_of_two_claims_is_reported_as_too_thin_to_publish():
    """It spends the client's footer link and returns nothing a homepage lacks."""
    brief = SiteBrief(facts={"Founded": Fact("1998", "https://x.example/about/")})

    assert _render(brief=brief).thin is True
    assert _render().thin is False


def test_a_superlative_in_a_brand_fact_is_reported_not_published_silently():
    """This file goes on the client's domain under their name."""
    brief = SiteBrief(facts={"Claim": Fact("The best rates in Australia", "https://x.example/a/")})

    assert _render(brief=brief).copy_issues != []


def test_the_generated_page_passes_its_own_rules():
    page = _render()

    report = audit_ai_info(page.html, site_url="https://x.example", facts=page.facts)

    assert report.failures == [], [f.rule_id for f in report.failures]


def test_a_noindex_page_fails_because_it_can_never_be_cited():
    html = (
        "<!doctype html><html><head><title>t</title>"
        '<meta name="description" content="d">'
        '<link rel="canonical" href="https://x.example/ai-info">'
        '<meta name="robots" content="noindex"></head><body>2026-09-09</body></html>'
    )

    assert audit_ai_info(html, facts=5).by_id("INF-007").outcome.value == "fail"


# -- the OKF bundle ------------------------------------------------------------


def _bundle():
    sections = [
        Section(name="Company", description="About", pages=[_page("https://x.example/a/", "A")]),
        Section(name="Fleet", pages=[_page("https://x.example/b/", "B")]),
    ]
    return render_okf(
        "https://x.example", "X Ltd", "A summary.", sections, generated_on=date(2026, 9, 9)
    )


def test_a_title_with_a_colon_stays_one_yaml_field():
    """Unquoted it becomes a nested mapping and the file stops parsing."""
    block = frontmatter({"type": "Page", "title": "Hire: Fleet Options"})

    assert 'title: "Hire: Fleet Options"' in block


def test_a_concept_without_a_type_is_refused_rather_than_defaulted():
    """A bundle whose files have no type is not an OKF bundle."""
    with pytest.raises(ValueError):
        frontmatter({"title": "no type"})


def test_slugs_are_derived_from_the_title_so_they_survive_a_new_page():
    """The path is the concept's address; a counter reorders when one is added."""
    assert slug("Business Car Hire") == "business-car-hire"
    assert slug("") == "untitled"


def test_every_page_links_up_to_its_section_and_back_down():
    """An agent landing on a page file has a route to the rest of the bundle."""
    files = _bundle().files

    assert "../sections/company.md" in files["pages/a.md"]
    assert "../pages/a.md" in files["sections/company.md"]


def test_the_log_says_when_the_bundle_was_built_and_from_what():
    log = _bundle().files["log.md"]

    assert "2026-09-09" in log
    assert "x.example" in log


def test_the_generated_bundle_passes_its_own_rules():
    report = audit_okf(_bundle().files)

    assert report.failures == [], [f.rule_id for f in report.failures]


def test_a_concept_nothing_links_to_is_reported():
    files = dict(_bundle().files)
    files["pages/orphan.md"] = frontmatter({"type": "Page", "title": "Orphan"}) + "\n# Orphan\n"

    assert audit_okf(files).by_id("OKF-004").outcome.value == "fail"


def test_a_link_to_a_file_the_bundle_does_not_contain_is_reported():
    files = dict(_bundle().files)
    files["index.md"] += "\n- [Gone](sections/gone.md)\n"

    assert audit_okf(files).by_id("OKF-003").outcome.value == "fail"


# -- the v2 Link rels ----------------------------------------------------------


def test_markdown_alternates_are_advertised_only_where_the_directory_exists():
    with_md = render_headers("https://x.example", True, False, md_layout=REPLACE)
    without = render_headers("https://x.example", True, False)

    assert 'rel="alternate"; type="text/markdown"' in with_md
    assert "alternate" not in without


def test_the_describedby_rel_is_not_typed_as_the_pages_own_markdown():
    """v2 puts `type` on the alternate. Typing describedby invites an agent to
    treat llms.txt as this page's markdown form, which it is not."""
    headers = render_headers("https://x.example", True, False)

    assert 'Link: </llms.txt>; rel="describedby"' in headers
    assert 'rel="describedby"; type=' not in headers


# -- what a directory artifact does to the pages that render it ----------------


def test_a_directory_is_not_described_as_one_file_served_at_one_path():
    """`md/` reported `0 bytes` served at `/`, and the client guide then said to
    upload 419 files so they answered at the site root."""
    from app.core.client_report import _publish_steps, _size_label
    from app.core.components import by_key

    md = by_key("md-pages")
    okf = by_key("okf")

    assert md.is_directory and okf.is_directory
    assert by_key("llms-txt").is_directory is False
    # An OKF bundle has a root; the markdown twins are scattered beside the pages
    # they mirror, so claiming a root for them would be an invented fact.
    assert okf.path == "/okf/"
    assert md.path == ""

    steps = _publish_steps("md/", "/index.md", "https://x.example", "", None)
    assert "keeping the paths exactly as given" in " ".join(steps)
    assert "answers at https://x.example/." not in " ".join(steps)
    assert _size_label(0) == "0 bytes"


def test_a_directorys_size_is_the_total_and_not_the_length_of_its_empty_body():
    from app.core.bundle import Artifact
    from app.core.client_report import _artifact_serve_at, _size_label

    directory = Artifact("md/", "", "", "text/markdown", files={"a.md": "x" * 2048})

    assert directory.size == 2048
    assert _size_label(directory.size) == "2 KB"
    assert _artifact_serve_at(directory) == "/"


def test_a_size_in_characters_is_not_a_size_in_bytes():
    """An em-dash is one character and three bytes. The label said bytes."""
    from app.core.bundle import Artifact

    assert Artifact("a.md", "/a.md", "—" * 100, "text/markdown").size == 300


def test_an_empty_directory_in_a_handover_is_a_defect_not_a_quiet_pass():
    from app.core.delivery import Kind, check_delivery

    report = check_delivery(
        llms_txt="# X\n\n> s\n",
        expected_files={"llms.txt": "# X\n\n> s\n", "agents.md": "# a", "robots.txt": "# r"},
        directories={"md/": {}},
    )

    assert any(i.kind is Kind.DEFECT and "no files in it" in i.title for i in report.items)


def test_a_directorys_reported_size_is_its_contents_not_its_body():
    from app.core.delivery import check_delivery

    report = check_delivery(
        expected_files={"md/": "", "llms.txt": "# X"},
        directories={"md/": {"a.md": "abcd", "b.md": "efgh"}},
    )

    assert report.files["md/"] == 8, "the empty body would have reported 0"


def test_a_www_page_on_a_bare_site_is_still_the_same_site():
    """The defect that shipped: redspot is filed as `redspot.com.au` and every
    page it lists is `www.redspot.com.au`, so comparing hosts verbatim rejected
    all 419 links and rewrote none -- and the file was byte-identical, which is
    what "nothing needed changing" also looks like."""
    pages = [_page("https://www.redspot.com.au/vehicles/ute-hire/")]
    out = render_md_pages(pages, REPLACE)
    llms = "- [Ute Hire](https://www.redspot.com.au/vehicles/ute-hire/): utes\n"

    rewritten = rewrite_links(llms, out.paths, "https://redspot.com.au")

    assert "/vehicles/ute-hire/index.md" in rewritten


def test_generating_the_directory_without_pointing_the_index_at_it_is_an_error():
    """MD-006. 419 markdown files nothing links to is not a feature."""
    pages = [_page("https://x.example/a/"), _page("https://x.example/b/")]
    out = render_md_pages(pages, REPLACE)
    html_only = "- [A](https://x.example/a/): one\n- [B](https://x.example/b/): two\n"

    missed = audit_markdown_pages(out.files, index_text=html_only)
    assert missed.by_id("MD-006").outcome.value == "fail"

    pointed = audit_markdown_pages(
        out.files, index_text=rewrite_links(html_only, out.paths, "https://x.example")
    )
    assert pointed.by_id("MD-006").outcome.value == "pass"


def test_md_006_skips_rather_than_passes_when_it_was_given_no_index():
    """A rule that did not run is not a rule that passed."""
    out = render_md_pages([_page("https://x.example/a/")], REPLACE)

    assert audit_markdown_pages(out.files).by_id("MD-006").outcome.value == "skipped"


def test_a_pre_quoted_summary_does_not_reach_yaml_or_a_meta_description():
    """The stored summary arrives carrying its own `>` on some runs. llms.txt
    stripped it and shipped clean; okf wrote it into frontmatter and ai-info into
    a published meta description, both with a literal `>` on the client's domain."""
    quoted = "> Australian car rental company operating from airports."

    bundle = render_okf(
        "https://x.example", "X", quoted, [Section(name="S", pages=[_page("https://x.example/a/")])]
    )
    page = _render(site_summary=quoted)

    assert ">" not in bundle.files["index.md"].split("---")[1]
    assert 'content="&gt;' not in page.html
    assert "Australian car rental" in page.html
