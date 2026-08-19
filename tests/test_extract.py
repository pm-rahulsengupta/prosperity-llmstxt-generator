"""Main-content extraction and JS-shell detection. Offline."""

from __future__ import annotations

from app.scrape.extract import ExtractedPage, extract, looks_like_js_shell

FULL_PAGE = """<!doctype html>
<html><head>
  <title>Quick Start Guide | Example</title>
  <meta name="description" content="Get started with Example in five minutes.">
  <meta name="robots" content="index, follow">
  <link rel="canonical" href="https://example.com/docs/quickstart">
</head>
<body>
  <nav><a href="/">Home</a> <a href="/pricing">Pricing</a> <a href="/blog">Blog</a></nav>
  <main>
    <h1>Quick Start</h1>
    <p>Install the CLI and authenticate with your API key. This paragraph is long
       enough that trafilatura will treat it as real body content rather than
       boilerplate, which is what we are checking here.</p>
    <h2>Authenticate</h2>
    <p>Run the login command and paste the token from your dashboard. Again this is
       deliberately verbose so the extractor has something substantial to keep.</p>
  </main>
  <footer>Copyright 2026 Example Inc. All rights reserved. Privacy. Terms.</footer>
</body></html>"""

JS_SHELL = """<!doctype html>
<html><head><title>Example App</title></head>
<body><div id="root"></div><script src="/bundle.js"></script></body></html>"""


def test_extracts_head_metadata() -> None:
    page = extract(FULL_PAGE, "https://example.com/docs/quickstart")

    assert page.title == "Quick Start Guide | Example"
    assert page.description == "Get started with Example in five minutes."
    assert page.h1 == "Quick Start"
    assert page.canonical == "https://example.com/docs/quickstart"
    assert not page.is_noindex


def test_markdown_keeps_body_and_drops_chrome() -> None:
    """The whole reason for trafilatura over a plain HTML-to-markdown pass."""
    markdown = extract(FULL_PAGE, "https://example.com/docs/quickstart").markdown

    assert "Install the CLI" in markdown
    assert "Authenticate" in markdown
    assert "All rights reserved" not in markdown, "footer must not survive"
    assert "Pricing" not in markdown, "nav must not survive"


def test_word_count_is_derived_from_extracted_content() -> None:
    page = extract(FULL_PAGE, "https://example.com/docs/quickstart")

    assert page.word_count > 40
    assert not page.is_thin


def test_falls_back_to_open_graph() -> None:
    html = """<html><head>
      <meta property="og:title" content="OG Title">
      <meta property="og:description" content="OG description of the page.">
    </head><body><p>Body</p></body></html>"""
    page = extract(html, "https://e.com/x")

    assert page.title == "OG Title"
    assert page.description == "OG description of the page."


def test_meta_name_matching_is_case_insensitive() -> None:
    html = '<html><head><meta name="Description" content="Mixed case name."></head><body></body></html>'
    assert extract(html, "https://e.com/x").description == "Mixed case name."


def test_noindex_is_reported() -> None:
    html = '<html><head><meta name="robots" content="noindex, nofollow"></head><body></body></html>'
    assert extract(html, "https://e.com/x").is_noindex


def test_empty_and_malformed_html_do_not_raise() -> None:
    assert extract("", "https://e.com/x") == ExtractedPage(url="https://e.com/x")
    assert extract("   ", "https://e.com/x").markdown == ""
    extract("<html><body><p>unclosed", "https://e.com/x")  # must not raise


def test_js_shell_is_detected() -> None:
    """This is the signal that escalates a page from HTTP to a browser fetcher."""
    assert looks_like_js_shell(JS_SHELL)
    assert looks_like_js_shell("")


def test_a_real_page_is_not_mistaken_for_a_js_shell() -> None:
    assert not looks_like_js_shell(FULL_PAGE)


def test_thin_extraction_is_flagged() -> None:
    page = extract("<html><body><main><p>Two words.</p></main></body></html>", "https://e.com/x")
    assert page.is_thin
