"""Importing a Screaming Frog crawl instead of performing one.

The fallback for a site we cannot read. A WAF that blocks our fetcher blocks the
preflight too, so the import has to skip the crawl stage entirely rather than
degrade it -- which is why `generate_task` branches on `Run.source` rather than
just tolerating an empty fetch.
"""

from __future__ import annotations

from app.core.csv_source import parse_screaming_frog_csv
from app.db.models import SOURCE_CRAWL, SOURCE_IMPORT


def test_the_fixture_parses_into_pages(sf_csv):
    entries = parse_screaming_frog_csv(sf_csv)

    assert entries, "the checked-in Internal All export must parse"
    assert all(e.url.startswith("http") for e in entries)


def test_only_indexable_html_that_answered_200_is_kept(sf_csv):
    """Screaming Frog saw the response and we did not, so its verdict is trusted."""
    entries = parse_screaming_frog_csv(sf_csv)

    assert all(e.status_code in (0, 200) for e in entries)


def test_the_cap_is_a_cost_guard_not_a_ranking(sf_csv):
    """It truncates in row order. Filtering belongs in Screaming Frog."""
    everything = parse_screaming_frog_csv(sf_csv)
    if len(everything) < 2:
        return

    capped = parse_screaming_frog_csv(sf_csv, max_urls=1)

    assert len(capped) == 1
    assert capped[0].url == everything[0].url, "row order, not score order"


def test_the_two_sources_are_distinct_values():
    """`generate_task` branches on this, so a collision would silently crawl."""
    assert SOURCE_CRAWL != SOURCE_IMPORT


def test_an_imported_run_skips_the_crawl_stage():
    """Asserted against the source, because the failure mode is a future edit
    reintroducing a fetch on a path that exists precisely to avoid one."""
    import inspect

    from app.jobs import tasks

    source = inspect.getsource(tasks.generate_task)
    branch = source.split("if source == SOURCE_IMPORT:")[1].split("else:")[0]

    assert "fetch_many" not in branch, "an import must never fetch"
    assert "run_preflight" not in branch, "the preflight would fail for the same reason"
    assert "get_pages" in branch, "it reads the rows the upload already stored"


def test_a_stored_row_round_trips_into_a_page_entry():
    """Every field the pipeline scores on has to survive the database."""
    from types import SimpleNamespace

    from app.jobs.tasks import _entry_from_row

    row = SimpleNamespace(
        url="https://x.example/a",
        title="A",
        description="d",
        h1="H",
        word_count=400,
        text_ratio=0.5,
        crawl_depth=2,
        folder_depth=1,
        inlinks=9,
        unique_inlinks=7,
        outlinks=3,
        external_outlinks=1,
        link_score=55,
        content_hash="abc",
        canonical="https://x.example/a",
        markdown="# A",
        status_code=200,
    )

    entry = _entry_from_row(row)

    assert entry.url == "https://x.example/a"
    assert entry.word_count == 400
    assert entry.unique_inlinks == 7, "the ranking signal the source tool dropped"
    assert entry.link_score == 55


def test_a_missing_column_becomes_a_default_not_a_crash():
    """A field Screaming Frog did not export is the same shape a crawl produces
    for a page that answered without it."""
    from types import SimpleNamespace

    from app.jobs.tasks import _entry_from_row

    sparse = SimpleNamespace(
        url="https://x.example/a",
        title=None,
        description=None,
        h1=None,
        word_count=None,
        text_ratio=None,
        crawl_depth=None,
        folder_depth=None,
        inlinks=None,
        unique_inlinks=None,
        outlinks=None,
        external_outlinks=None,
        link_score=None,
        content_hash=None,
        canonical=None,
        markdown=None,
        status_code=None,
    )

    entry = _entry_from_row(sparse)

    assert entry.title == ""
    assert entry.word_count == 0
    assert entry.crawl_depth == -1, "unknown depth, not root"
