"""What a crawler that does not run JavaScript actually gets.

From the LLM Access Checker's method, which loads every page twice -- once as raw
HTML and once with JavaScript executed -- and reports the gap. Its Robots & Crawl
pillar states the reason plainly: GPTBot, ClaudeBot and PerplexityBot do not
execute JavaScript, so content that needs JS to appear does not exist for most
AI.

This tool's escalation ladder was doing the exact opposite. `_should_escalate`
fires on a JS shell, Chromium returns the full page, and the raw-HTML result was
discarded -- so a page invisible to every AI crawler was recorded as a clean 200
with 2,000 words, and the generated llms.txt would describe content none of them
can reach. A confidently wrong deliverable, and worse than a failed crawl
because a failed crawl is visible.
"""

from __future__ import annotations

import pytest

from app.scrape.fetch import JS_VISIBLE_SHARE, FetchResult, Tier


def result(raw: int, rendered: int, tier: Tier = Tier.STEALTH) -> FetchResult:
    return FetchResult(url="https://x.example/p", tier=tier, raw_words=raw, rendered_words=rendered)


def test_a_shell_rescued_by_a_browser_is_a_finding():
    """The case this exists for: raw HTML has nothing, Chromium has an article."""
    assert result(raw=12, rendered=2000).needs_javascript


def test_a_page_that_only_gains_furniture_is_not():
    """Menus, cookie banners and footers hydrate under JS on most sites.

    Reporting every one of those would bury the pages where the article itself
    is missing.
    """
    assert not result(raw=1800, rendered=2000).needs_javascript


def test_the_threshold_is_a_ratio_not_a_word_count():
    """A 200-word page losing half its text matters as much as a 2,000-word one."""
    assert result(raw=90, rendered=200).needs_javascript
    assert not result(raw=190, rendered=200).needs_javascript


@pytest.mark.parametrize("rendered", [0])
def test_no_browser_ran_means_the_question_was_never_asked(rendered):
    """`False`, but for the honest reason.

    A page served fully by raw HTML never escalated, so nothing was hidden. That
    is not the same as "we checked and JS makes no difference", and the property
    must not claim it is -- which is why it keys on `rendered_words` being set
    rather than on the two counts being equal.
    """
    assert not result(raw=2000, rendered=rendered).needs_javascript


def test_the_share_is_stated_once():
    assert 0 < JS_VISIBLE_SHARE < 1


# -- the ladder records both sides ------------------------------------------------


def test_the_http_rung_is_recorded_even_when_a_browser_wins():
    """The whole fix. Before this, the cheap tier's result was thrown away."""
    import inspect

    from app.scrape.fetch import PageFetcher

    source = inspect.getsource(PageFetcher.fetch)

    assert "result.raw_words = words" in source
    assert "Tier.HTTP" in source
    assert "result.rendered_words" in source


def test_a_browser_rung_takes_the_larger_of_the_two():
    """DYNAMIC then STEALTH: the second must not overwrite a better first."""
    import inspect

    from app.scrape.fetch import PageFetcher

    assert "max(result.rendered_words, words)" in inspect.getsource(PageFetcher.fetch)


def test_firecrawl_is_not_counted_as_a_render():
    """It is a managed unblocker, not a statement about JavaScript.

    Counting its output as `rendered_words` would report "needs JavaScript" for
    a page that was merely behind a bot wall.
    """
    import inspect

    from app.scrape.fetch import PageFetcher

    source = inspect.getsource(PageFetcher.fetch)
    branch = source.split("elif tier in (", 1)[1].split(")", 1)[0]

    assert "FIRECRAWL" not in branch


def test_the_crawl_reports_the_count_rather_than_swallowing_it():
    import inspect

    from app.jobs import tasks

    source = inspect.getsource(tasks.generate_task)

    assert "needs_javascript" in source, "the ladder rescues these silently again"
    assert "js_only" in source
    assert '"js_only_urls"' in source, "a run's events scroll away; this must be stored"
