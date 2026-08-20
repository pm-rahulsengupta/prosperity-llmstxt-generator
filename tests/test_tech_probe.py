"""Platform and endpoint detection, without a GPL fingerprint set.

Every maintained Wappalyzer descendant carries the GPL dataset, and BuiltWith is a
paid API. This asks the smaller question an agents.md actually needs -- can the
site transact, what machine-readable surfaces answer -- from headers, the
generator tag, and paths that respond.

Two of the tests below record bugs found by running it against live sites: it
reported the most identifiable platform on the web as "unknown", and it missed a
generator tag sitting behind 300KB of inlined CSS.
"""

from __future__ import annotations

import pytest

from app.scrape.tech_probe import (
    COMMERCE_PLATFORMS,
    Detection,
    Platform,
    TechProfile,
    platform_from_headers,
    platform_from_html,
)

# -- headers ------------------------------------------------------------------


def test_shopify_is_detected_from_its_cookies():
    """The bug found on allbirds.com.

    The first version looked for `x-shopid` and `x-shopify-stage`, which a real
    store does not send. What it does send is `_shopify_y` cookies, so the
    detector reported "unknown" for the most identifiable platform on the web.
    """
    headers = {"set-cookie": "localization=us; path=/, _shopify_y=abc; domain=allbirds.com"}
    platform, evidence = platform_from_headers(headers)

    assert platform is Platform.SHOPIFY
    assert "_shopify_" in evidence


def test_shopify_is_detected_from_the_cdn_preconnect():
    headers = {"link": '<https://cdn.shopify.com>; rel="preconnect"'}
    assert platform_from_headers(headers)[0] is Platform.SHOPIFY


def test_the_evidence_quotes_the_part_that_matched():
    """httpx joins repeated headers, so a store matching on `_shopify_` was being
    evidenced with an unrelated `localization=us` cookie.

    Evidence an operator cannot verify is worse than none, because it looks
    checked.
    """
    headers = {"set-cookie": "localization=us; path=/, _shopify_y=abc; domain=x.com"}
    _, evidence = platform_from_headers(headers)

    assert "_shopify_y" in evidence


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"x-powered-by": "WordPress"}, Platform.WORDPRESS),
        ({"x-powered-by": "Next.js, Payload"}, Platform.NEXTJS),
        ({"x-wix-request-id": "abc"}, Platform.WIX),
        ({"x-generator": "Drupal 10"}, Platform.DRUPAL),
    ],
)
def test_other_platforms_from_headers(headers, expected):
    assert platform_from_headers(headers)[0] is expected


def test_unremarkable_headers_stay_unknown():
    """Guessing here changes which profile the file is written against."""
    assert platform_from_headers({"server": "nginx"})[0] is Platform.UNKNOWN
    assert platform_from_headers({})[0] is Platform.UNKNOWN


# -- the generator tag --------------------------------------------------------


def test_the_generator_tag_is_read():
    html = '<meta name="generator" content="WordPress 6.6.7" />'
    platform, evidence = platform_from_html(html)

    assert platform is Platform.WORDPRESS
    assert "6.6.7" in evidence


def test_woocommerce_beats_wordpress_in_the_generator_list():
    """A WooCommerce site is also a WordPress site; the more specific one decides
    whether a transaction may be described."""
    html = '<meta name="generator" content="WooCommerce 8.2" />'
    assert platform_from_html(html)[0] is Platform.WOOCOMMERCE


def test_a_generator_tag_far_down_the_document_is_still_found():
    """The bug found on prosperitymedia.com.au.

    WP Rocket inlines ~300KB of critical CSS ahead of the head tags, putting the
    generator tag at byte 301,922. A 200KB scan window reported the site as
    unknown -- so the whole document is searched.
    """
    html = (
        "<style>" + ("a{color:red}" * 30_000) + "</style>"
        '<meta name="generator" content="WordPress 6.6.7" />'
    )

    assert len(html) > 300_000
    assert platform_from_html(html)[0] is Platform.WORDPRESS


def test_no_generator_tag_is_unknown_not_a_guess():
    assert platform_from_html("<html><head></head></html>")[0] is Platform.UNKNOWN


def test_an_unrecognised_generator_is_unknown():
    """A generator we do not know is not a licence to guess."""
    html = '<meta name="generator" content="Bespoke CMS 1.0" />'
    assert platform_from_html(html)[0] is Platform.UNKNOWN


# -- what the platform licenses --------------------------------------------


def test_only_commerce_platforms_sell():
    for platform in (Platform.SHOPIFY, Platform.WOOCOMMERCE, Platform.MAGENTO):
        assert TechProfile(site_url="x", platform=platform).sells
    for platform in (Platform.WORDPRESS, Platform.WIX, Platform.NEXTJS, Platform.UNKNOWN):
        assert not TechProfile(site_url="x", platform=platform).sells


def test_selling_is_about_the_platform_not_the_verified_capability():
    """`sells` widens what may be described; it never claims anything.

    `build_agents_doc` still requires a verified UCP endpoint before writing how
    to buy, so a detected shop with no endpoint gets a file that says it cannot
    be transacted with.
    """
    assert Platform.SHOPIFY in COMMERCE_PLATFORMS
    assert TechProfile(site_url="x", platform=Platform.SHOPIFY).endpoint_urls == []


def test_the_summary_names_the_evidence():
    profile = TechProfile(
        site_url="https://x.com",
        platform=Platform.SHOPIFY,
        platform_evidence="response header set-cookie: ..._shopify_y...",
        endpoints=[
            Detection("Products (JSON)", "200 application/json", "https://x.com/products.json")
        ],
    )
    summary = profile.summary()

    assert "shopify" in summary
    assert "1 machine-readable endpoint" in summary
