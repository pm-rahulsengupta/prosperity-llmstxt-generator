"""The HTML primitives, tested as a boundary.

This is the first artifact in the tool that is HTML, and the first place where
getting escaping wrong publishes something dangerous on a client's domain rather
than merely printing an odd character. Every test here is a property of the
choke point, not of a particular page.
"""

from __future__ import annotations

import inspect
import re

import pytest
from lxml import html as lxml_html

from app.core import info_render
from app.core.info_render import Html, attr, el, safe_url

# -- escaping ------------------------------------------------------------------


def test_a_crawled_title_containing_a_script_tag_is_inert():
    """Parsed, not string-matched. What matters is that no element exists."""
    rendered = el("h2", "<script>alert(1)</script>")

    doc = lxml_html.fragment_fromstring(str(rendered))

    assert doc.xpath("//script") == []
    assert "alert(1)" in doc.text_content(), "the text survives; only the markup dies"


def test_escaping_happens_exactly_once():
    """The likeliest visible defect on the first page shipped.

    Crawled titles routinely arrive already escaped by the source CMS. Escaping
    again puts `&amp;amp;` on a client's page, which reads as a data problem and
    gets blamed on the crawler.
    """
    already = str(el("p", "Ben &amp; Jerry's"))
    raw = str(el("p", "Ben & Jerry's"))

    assert already == raw
    assert "&amp;amp;" not in already


def test_an_html_value_is_not_escaped_again():
    inner = el("em", "emphasis")

    assert str(el("p", inner)) == "<p><em>emphasis</em></p>"


def test_a_plain_string_that_looks_like_html_is_escaped():
    """`Html` is the only exemption, and it has to be constructed deliberately."""
    assert str(el("p", "<em>not markup</em>")) == "<p>&lt;em&gt;not markup&lt;/em&gt;</p>"


# -- attributes ----------------------------------------------------------------


def test_an_attribute_value_cannot_break_out():
    rendered = str(el("div", "body", title='" onmouseover="alert(1)'))

    doc = lxml_html.fragment_fromstring(rendered)

    assert doc.get("onmouseover") is None
    assert doc.get("title") == '" onmouseover="alert(1)'


def test_attributes_are_always_double_quoted():
    """Quoting is not cosmetic. An unquoted value ends at a space, and
    `html.escape` does not escape spaces."""
    assert str(attr("data-x", "a b")) == 'data-x="a b"'


def test_an_attribute_name_that_is_not_a_name_is_refused():
    with pytest.raises(ValueError, match="attribute name"):
        el("div", "x", **{"onclick=alert(1) data": "y"})


def test_a_tag_name_that_is_not_a_name_is_refused():
    with pytest.raises(ValueError, match="tag name"):
        el("div onload=alert(1)", "x")


def test_an_empty_attribute_is_dropped_rather_than_rendered_bare():
    assert str(el("p", "x", title="")) == "<p>x</p>"


# -- URLs, which escaping does not make safe -----------------------------------


def test_a_javascript_url_is_refused_not_escaped():
    """The distinction this module exists to make.

    `href="javascript:alert(1)"` is already perfectly escaped and still runs.
    Escaping is the wrong tool; refusing is the right one.
    """
    assert safe_url("javascript:alert(1)") == ""
    assert safe_url("JavaScript:alert(1)") == "", "scheme matching is case-insensitive"


def test_a_data_url_is_refused():
    assert safe_url("data:text/html;base64,PHNjcmlwdD4=") == ""


def test_an_ordinary_link_survives():
    assert safe_url("https://example.com/about") == "https://example.com/about"


def test_a_url_outside_the_verified_set_is_refused():
    """This module's half of the rule AGT-004 enforces for markdown.

    An `href` is a URL an agent follows just as much as a bare one, so a page we
    generate may not link somewhere no probe saw.
    """
    seen = frozenset({"https://example.com/about"})

    assert safe_url("https://example.com/about", allowed=seen) == "https://example.com/about"
    assert safe_url("https://elsewhere.example/x", allowed=seen) == ""


def test_the_verified_set_ignores_a_trailing_slash():
    """A crawl records `/about` and a link writes `/about/`; they are one page."""
    assert safe_url("https://example.com/about/", allowed=frozenset({"https://example.com/about"}))


def test_no_allowed_set_means_the_scheme_check_alone():
    """`None` is "we were not given a set", not "the set is empty".

    Same sentinel discipline as `evidence.verified_urls`: an empty set must
    refuse everything, and `None` must not.
    """
    assert safe_url("https://example.com/x", allowed=None)
    assert safe_url("https://example.com/x", allowed=frozenset()) == ""


# -- the structural guarantee ---------------------------------------------------


def test_no_markup_is_built_by_string_interpolation_outside_el():
    """The guarantee that makes the rest of the tests worth having.

    Every test above checks that `el` escapes. None of them would notice a
    renderer that stopped calling `el` and wrote `f"<p>{title}</p>"` instead. This
    reads the module's own source and fails if any f-string outside the two
    primitives contains a tag.
    """
    offenders = [
        line.strip()
        for line in inspect.getsource(info_render).splitlines()
        if re.search(r'f"[^"]*<[a-z]', line) or re.search(r"f'[^']*<[a-z]", line)
    ]

    assert offenders == [], f"markup built by interpolation: {offenders}"


def test_the_structural_guard_would_catch_a_real_regression():
    """A guard that cannot fail is worse than none: it reads as protection.

    `el` builds its own tags from `f"<{opening}>"`, which the pattern must not
    flag, while `f"<p>{title}</p>"` -- the thing somebody will write in a hurry --
    must be.
    """

    def flags(line: str) -> bool:
        return bool(re.search(r'f"[^"]*<[a-z]', line) or re.search(r"f'[^']*<[a-z]", line))

    assert flags('    return f"<p>{title}</p>"')
    assert not flags('    return Html(f"<{opening}>{inner}</{tag}>")')


def test_html_is_a_str_so_it_composes():
    """Needed so a fragment can be passed around and concatenated without
    a wrapper type leaking into every signature."""
    assert isinstance(el("p", "x"), str)
    assert isinstance(el("p", "x"), Html)


def test_a_void_element_has_no_closing_tag():
    assert str(el("meta", name="robots", content="index,follow")) == (
        '<meta name="robots" content="index,follow">'
    )


def test_a_trailing_underscore_maps_to_a_keyword_attribute():
    assert str(el("p", "x", class_="lead")) == '<p class="lead">x</p>'


def test_an_underscore_becomes_a_dash():
    """`data_source_kind` is how a provenance mark is written in Python."""
    assert str(el("p", "x", data_source_kind="own_site")) == (
        '<p data-source-kind="own_site">x</p>'
    )
