"""Plan construction and plan-to-URL selection. Offline: no client, no network."""

from __future__ import annotations

from app.llm.prompts.plan import CrawlPlan, TemplateRule
from app.llm.stages import SAMPLE_SIZE, heuristic_plan, select_urls
from app.scrape.recon import RobotsInfo, SiteRecon, cluster_urls


def recon_for(urls: list[str], site_url: str = "https://example.com") -> SiteRecon:
    return SiteRecon(
        site_url=site_url,
        robots=RobotsInfo(fetched=True),
        urls=urls,
        templates=cluster_urls(urls),
    )


def flat_site(n: int = 220) -> SiteRecon:
    """One template holding nearly everything -- the WordPress shape."""
    return recon_for([f"https://example.com/post-{i}-about-things" for i in range(n)])


# -- heuristic plan ---------------------------------------------------------


def test_heuristic_plan_excludes_pagination_and_archives():
    urls = [f"https://example.com/blog/page/{i}" for i in range(10)]
    urls += [f"https://example.com/tag/topic-{i}-here" for i in range(10)]
    urls += [f"https://example.com/docs/guide-{i}-setup" for i in range(6)]

    plan = heuristic_plan(recon_for(urls), page_cap=100)
    excluded = {rule.template for rule in plan.rules if not rule.includes}

    assert any("/page/" in template for template in excluded)
    assert any("/tag/" in template for template in excluded)
    assert not any("/docs/" in template for template in excluded)


def test_heuristic_plan_samples_very_repetitive_templates():
    plan = heuristic_plan(flat_site(500), page_cap=100)
    assert any(rule.sample_only for rule in plan.rules)


def test_heuristic_plan_covers_every_template():
    recon = recon_for(
        [
            "https://example.com/",
            "https://example.com/about",
            *[f"https://example.com/blog/post-{i}-here" for i in range(8)],
        ]
    )
    plan = heuristic_plan(recon, page_cap=50)
    assert {rule.template for rule in plan.rules} == {t.template for t in recon.templates}


# -- selection --------------------------------------------------------------


def test_excluded_templates_are_not_crawled():
    recon = recon_for(
        [
            "https://example.com/docs/one-two-three",
            "https://example.com/docs/four-five-six",
            "https://example.com/tag/alpha-beta-gamma",
        ]
    )
    plan = CrawlPlan(
        rules=[
            TemplateRule(
                template=t.template, action="exclude" if "/tag/" in t.template else "include"
            )
            for t in recon.templates
        ]
    )
    urls = select_urls(recon, plan, page_cap=100)
    assert not any("/tag/" in url for url in urls)
    assert any("/docs/" in url for url in urls)


def test_homepage_is_crawled_even_when_no_rule_includes_it():
    """It is the single most useful page in the file; an over-eager rule must not lose it."""
    recon = flat_site(30)
    plan = CrawlPlan(
        rules=[TemplateRule(template=t.template, action="exclude") for t in recon.templates]
    )
    assert select_urls(recon, plan, page_cap=10) == ["https://example.com/"]


def test_a_sample_fills_the_budget_rather_than_stopping_at_the_floor():
    """The flat-site case: one template, a 400-page cap, and 220 pages available."""
    recon = flat_site(220)
    plan = CrawlPlan(
        rules=[TemplateRule(template=t.template, action="sample") for t in recon.templates]
    )

    urls = select_urls(recon, plan, page_cap=400)

    # Everything it has, plus the homepage -- not a flat 25 with 94% of the budget unspent.
    assert len(urls) == 221
    assert len(set(urls)) == len(urls)


def test_a_sample_still_respects_a_tight_budget():
    recon = flat_site(220)
    plan = CrawlPlan(
        rules=[TemplateRule(template=t.template, action="sample") for t in recon.templates]
    )
    assert len(select_urls(recon, plan, page_cap=40)) == 40


def test_sample_floor_applies_when_there_is_no_budget_headroom():
    """With the cap already met by other templates, a sample stays at its floor."""
    recon = flat_site(1_000)
    plan = CrawlPlan(
        rules=[TemplateRule(template=t.template, action="sample") for t in recon.templates]
    )
    urls = select_urls(recon, plan, page_cap=SAMPLE_SIZE)
    assert len(urls) == SAMPLE_SIZE


def test_priority_orders_the_crawl_list():
    recon = recon_for(
        [
            "https://example.com/deep/nested/thing-one-here",
            "https://example.com/about",
        ]
    )
    plan = CrawlPlan(
        rules=[
            TemplateRule(template="/about", action="include", priority=1),
            TemplateRule(template="/deep/nested/thing-one-here", action="include", priority=5),
        ]
    )
    urls = select_urls(recon, plan, page_cap=100)
    assert urls.index("https://example.com/about") < urls.index(
        "https://example.com/deep/nested/thing-one-here"
    )
