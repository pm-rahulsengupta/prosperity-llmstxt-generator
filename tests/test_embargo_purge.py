"""Embargo has to reach the derived artifacts, not just the page store.

The first version deleted `pages` rows and stopped. A page body deleted from
`pages` while the same content sits in a rendered `llms-full.txt` that is still
downloadable is not an embargo, it is a change of filing. These test the parts
that can be tested without a database: what gets recognised as a mention, and
what a strip does to a rendered file.
"""

from __future__ import annotations

from app.db.repo import PurgeReport, _mentions_embargoed, _strip_embargoed_lines

INDEX = (
    "# Example\n"
    "\n"
    "> An example site.\n"
    "\n"
    "## Services\n"
    "\n"
    "- [SEO](https://x.com/services/seo/): Search work.\n"
    "- [NDA project](https://x.com/clients/nda-2026/brief/): Confidential.\n"
    "- [Digital PR](https://x.com/services/pr/): Link building.\n"
)


def test_an_embargoed_link_line_is_removed_from_a_rendered_index():
    cleaned, removed = _strip_embargoed_lines(INDEX, ("/clients/nda-2026/*",))

    assert removed == 1
    assert "nda-2026" not in cleaned
    # The rest of the file is untouched, including its structure.
    assert "## Services" in cleaned
    assert "https://x.com/services/seo/" in cleaned
    assert "https://x.com/services/pr/" in cleaned


def test_stripping_nothing_leaves_the_file_byte_identical():
    """A no-op purge must not rewrite a deliverable."""
    cleaned, removed = _strip_embargoed_lines(INDEX, ("/nothing-here/*",))

    assert removed == 0
    assert cleaned == INDEX


def test_the_trailing_newline_is_preserved():
    """The rendered file is byte-pinned by a golden test; a lost newline breaks it."""
    cleaned, _ = _strip_embargoed_lines(INDEX, ("/clients/nda-2026/*",))
    assert cleaned.endswith("\n")


def test_an_empty_document_is_handled():
    assert _strip_embargoed_lines("", ("/a/*",)) == ("", 0)


def test_a_mention_anywhere_in_body_text_counts():
    """`llms-full.txt` embeds whole page bodies, so a URL can appear in prose.

    This is why that file is blanked rather than edited: there is no reliable
    per-page boundary to cut on, and a partial removal nobody can verify is worse
    than an empty field.
    """
    body = "Some prose that links to https://x.com/clients/nda-2026/brief/ inline."

    assert _mentions_embargoed(body, ("/clients/nda-2026/*",))
    assert not _mentions_embargoed(body, ("/clients/other/*",))


def test_a_url_in_markdown_punctuation_is_still_found():
    """The regex has to stop at the bracket, or the pattern never matches."""
    assert _mentions_embargoed(
        "- [X](https://x.com/clients/nda-2026/brief/): note", ("/clients/nda-2026/*",)
    )


def test_an_unrelated_url_is_not_matched():
    assert not _mentions_embargoed(
        "- [X](https://x.com/services/seo/): note", ("/clients/nda-2026/*",)
    )


def test_a_near_miss_pattern_does_not_over_delete():
    """`/inventory*` versus `/inventory/private*`: the mistyped-pattern case.

    Over-deleting is recoverable by re-crawl -- the source is the client's live
    site -- but it should still not happen silently, so the report names what went.
    """
    text = (
        "- [Public](https://x.com/inventory/): fine\n"
        "- [Private](https://x.com/inventory/private/1): secret\n"
    )
    narrow, n_narrow = _strip_embargoed_lines(text, ("/inventory/private*",))
    broad, n_broad = _strip_embargoed_lines(text, ("/inventory*",))

    assert n_narrow == 1 and "https://x.com/inventory/" in narrow
    assert n_broad == 2, "a broad pattern takes both, which is why it is reported"


# -- the report ---------------------------------------------------------------


def test_an_empty_report_says_nothing_happened():
    report = PurgeReport()

    assert not report.anything
    assert "Nothing matched" in report.summary()


def test_the_report_counts_every_surface_separately():
    """Four places hold an embargoed URL, and a single total would hide which."""
    report = PurgeReport(pages=3, metric_rows=2, index_lines=4, full_documents=1, revisions=2)

    assert report.anything
    summary = report.summary()
    for number in ("3", "2", "4", "1"):
        assert number in summary


def test_the_report_carries_the_urls_not_only_a_count():
    """ "Three pages were removed" cannot confirm the right three went."""
    report = PurgeReport(pages=1, urls=("https://x.com/clients/nda-2026/brief/",))

    assert report.urls[0].endswith("/brief/")
