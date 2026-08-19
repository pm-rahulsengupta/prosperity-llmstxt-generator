"""HTML to main-content markdown, plus the metadata the ranking needs.

This replaces Firecrawl's `onlyMainContent: true`. Trafilatura does the extraction:
running a whole page through a plain HTML-to-markdown converter drags the nav, the
cookie banner and the footer into every entry of llms-full.txt, which is precisely
the noise the file exists to avoid.

Pure functions over HTML strings, so the interesting behaviour is testable without
touching the network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import trafilatura
from lxml import html as lxml_html

# Below this many characters of extracted body text, treat the fetch as having
# failed rather than succeeded thinly: it is the signature of a JS shell that
# rendered nothing, and it is what triggers escalation to a browser fetcher.
MIN_CONTENT_CHARS = 200

# Markers of a client-rendered page that returned only a mount point.
_JS_SHELL_HINTS = (
    'id="root"',
    'id="app"',
    'id="__next"',
    "data-reactroot",
    "ng-app",
)

_WHITESPACE = re.compile(r"\s+")


@dataclass(slots=True)
class ExtractedPage:
    url: str
    title: str = ""
    description: str = ""
    h1: str = ""
    markdown: str = ""
    word_count: int = 0
    canonical: str = ""
    robots_meta: str = ""

    @property
    def is_thin(self) -> bool:
        return len(self.markdown) < MIN_CONTENT_CHARS

    @property
    def is_noindex(self) -> bool:
        return "noindex" in self.robots_meta.lower()


def looks_like_js_shell(html_text: str) -> bool:
    """A near-empty body with a framework mount point: fetching it again as HTML
    will not help, but a browser fetcher will."""
    if not html_text:
        return True
    body_text = _visible_text(html_text)
    if len(body_text) >= MIN_CONTENT_CHARS:
        return False
    return any(hint in html_text for hint in _JS_SHELL_HINTS)


def extract(html_text: str, url: str) -> ExtractedPage:
    """Pull main-content markdown and head metadata out of a page."""
    page = ExtractedPage(url=url)
    if not html_text or not html_text.strip():
        return page

    markdown = (
        trafilatura.extract(
            html_text,
            output_format="markdown",
            include_links=True,
            include_tables=True,
            favor_precision=True,
            url=url,
        )
        or ""
    )
    page.markdown = markdown.strip()
    page.word_count = len(page.markdown.split())

    try:
        tree = lxml_html.fromstring(html_text)
    except (ValueError, lxml_html.etree.ParserError):
        return page

    page.title = _first_text(tree, "//title")
    page.h1 = _first_text(tree, "//h1")
    page.description = _meta(tree, "description")
    page.robots_meta = _meta(tree, "robots")

    canonical = tree.xpath("//link[@rel='canonical']/@href")
    if canonical:
        page.canonical = canonical[0].strip()

    # Open Graph is the usual fallback when a page has no meta description.
    if not page.description:
        page.description = _property_meta(tree, "og:description")
    if not page.title:
        page.title = _property_meta(tree, "og:title")

    return page


def _first_text(tree, xpath: str) -> str:
    nodes = tree.xpath(xpath)
    if not nodes:
        return ""
    return _clean(nodes[0].text_content())


def _meta(tree, name: str) -> str:
    values = tree.xpath(
        f"//meta[translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='{name}']/@content"
    )
    return _clean(values[0]) if values else ""


def _property_meta(tree, prop: str) -> str:
    values = tree.xpath(f"//meta[@property='{prop}']/@content")
    return _clean(values[0]) if values else ""


def _visible_text(html_text: str) -> str:
    try:
        tree = lxml_html.fromstring(html_text)
    except (ValueError, lxml_html.etree.ParserError):
        return ""
    for bad in tree.xpath("//script | //style | //noscript"):
        bad.getparent().remove(bad)
    return _clean(tree.text_content())


def _clean(value: str) -> str:
    return _WHITESPACE.sub(" ", value or "").strip()
