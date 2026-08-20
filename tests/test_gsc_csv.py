"""The upload path: a Search Console export becomes usable metrics.

Every fixture here is shaped like a real export rather than an idealised CSV,
because the failures worth catching are the ones a real file causes: a zip
instead of a CSV, a column named for the UI tab, thousands separators, and the
wrong tab exported entirely.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import date

import pytest

from app.core.metrics import DateRange
from app.metrics.gsc_csv import parse_gsc_export

PAGES_CSV = (
    "Top pages,Clicks,Impressions,CTR,Position\n"
    'https://x.com/,"1,200","45,000",2.67%,4.2\n'
    "https://x.com/seo-agency/,58,2100,2.76%,11.4\n"
    "https://x.com/about/,3,900,0.33%,28.1\n"
)


def test_a_pages_export_parses():
    result = parse_gsc_export(PAGES_CSV)

    assert result.url_count == 3
    home = result.metrics["https://x.com/"]
    assert home.clicks == 1_200
    assert home.impressions == 45_000
    assert home.position == pytest.approx(4.2)
    assert home.ctr == pytest.approx(0.0267)


def test_the_zip_google_actually_gives_you_works_unopened():
    """Nobody should have to extract a file to use this."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("Pages.csv", PAGES_CSV)
        bundle.writestr("Queries.csv", "Query,Clicks\nseo,10\n")

    result = parse_gsc_export(buffer.getvalue())

    assert result.url_count == 3
    assert any("ignored" in note for note in result.notes)


def test_the_queries_export_is_refused_rather_than_misread():
    """It has clicks and impressions, so it would parse and mean nothing.

    Scoring pages on query rows produces numbers that look entirely plausible,
    which is exactly why this has to fail loudly instead of succeeding quietly.
    """
    with pytest.raises(ValueError, match="queries export"):
        parse_gsc_export("Query,Clicks,Impressions,CTR,Position\nseo agency,120,4000,3%,2.1\n")


def test_a_zip_with_no_pages_file_says_what_it_did_contain():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("Queries.csv", "Query,Clicks\nseo,10\n")

    with pytest.raises(ValueError, match=re.escape("Queries.csv")):
        parse_gsc_export(buffer.getvalue())


def test_tracking_variants_are_merged_on_import():
    csv = (
        "Top pages,Clicks,Impressions\n"
        "https://x.com/,900,10000\n"
        "https://x.com/?utm_source=maps&utm_medium=organic,100,2000\n"
    )
    result = parse_gsc_export(csv)

    assert result.url_count == 1
    assert result.metrics["https://x.com/"].clicks == 1_000
    assert result.merged_count == 1
    assert any("tracking-tagged" in note for note in result.notes)


def test_the_window_is_carried_onto_every_row():
    window = DateRange(date(2026, 5, 19), date(2026, 8, 17))
    result = parse_gsc_export(PAGES_CSV, window=window)

    assert all(m.window == window for m in result.metrics.values())


def test_a_file_with_no_metric_columns_is_refused():
    with pytest.raises(ValueError, match="nothing to rank on"):
        parse_gsc_export("Top pages,Something\nhttps://x.com/,4\n")


def test_rows_that_are_not_urls_are_skipped_and_counted():
    """Exports carry totals rows and stray text; they are not pages."""
    csv = PAGES_CSV + "Total,1261,48000,,\n"
    result = parse_gsc_export(csv)

    assert result.url_count == 3
    assert any("Skipped 1" in note for note in result.notes)


def test_an_empty_file_fails_with_a_readable_message():
    with pytest.raises(ValueError, match="no rows"):
        parse_gsc_export("")
