"""What a site is built on, from evidence the site itself publishes.

A BuiltWith-shaped question, answered without BuiltWith. The alternatives were
surveyed before writing this and every one of them was rejected for the same
reason:

    enthec/webappanalyzer     GPL-3.0   active   the maintained Wappalyzer dataset
    s0md3v/wappalyzer-next    GPL-3.0   active   Python, same dataset
    dochne/wappalyzer         GPL-3.0   stale    JavaScript
    projectdiscovery/wappalyzergo  MIT   active  Go, bundles the same fingerprints
    urbanadventurer/WhatWeb   GPL-2.0   active   Ruby

Wappalyzer went private in 2023 and every surviving fork descends from its last
GPL fingerprint set, so the *data* is GPL even where the code is not. Vendoring
it into a public repository would put this tool's licence question in someone
else's hands, and BuiltWith itself is a paid API with per-lookup billing.

The deciding argument is not licensing though — it is fit. Those tools answer
"which of seven thousand technologies is here", and an agents.md needs about six
facts: can this site transact, does it expose machine-readable listings, does it
have a search endpoint, where do its feeds live, and can we publish a static file
on it. Knowing the analytics vendor changes nothing in the output. So this asks
the smaller question directly, from three sources the site publishes about itself:

* **response headers** — platforms announce themselves, and a header is set by
  the server rather than by whoever wrote the theme;
* **the generator meta tag** — the site's own statement of what built it;
* **well-known paths** — `/wp-json/`, `/products.json`, `/.well-known/ucp`. The
  strongest evidence of all, because a path that answers is a capability that
  works rather than a badge that might be stale.

Everything returned carries the evidence that produced it, and nothing is
inferred from a platform's usual behaviour. `Detection.evidence` is what goes in
front of the operator, and it is the same rule the generator runs on: a
capability is real when something answered, not when a header hinted.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum

import httpx

from app.scrape.fingerprints import detect

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15.0

GENERATOR_META = re.compile(
    r"""<meta[^>]+name=["']generator["'][^>]+content=["']([^"']+)["']""", re.I
)


class Platform(StrEnum):
    SHOPIFY = "shopify"
    WORDPRESS = "wordpress"
    WOOCOMMERCE = "woocommerce"
    WIX = "wix"
    SQUARESPACE = "squarespace"
    WEBFLOW = "webflow"
    BIGCOMMERCE = "bigcommerce"
    MAGENTO = "magento"
    DRUPAL = "drupal"
    HUBSPOT = "hubspot"
    NEXTJS = "nextjs"
    UNKNOWN = "unknown"


# Platforms that sell. Decides which agents.md profile is even permitted to
# describe a transaction, so it is a list of facts rather than a guess.
COMMERCE_PLATFORMS = frozenset(
    {
        Platform.SHOPIFY,
        Platform.WOOCOMMERCE,
        Platform.BIGCOMMERCE,
        Platform.MAGENTO,
    }
)

# Header name -> substring -> platform. Headers beat HTML: a theme can copy a
# meta tag, but the server sets these.
# Measured against allbirds.com, which sets none of the `x-shopify-*` headers the
# first version looked for. What it does send is `_shopify_y` cookies and a
# `link:` preconnect to cdn.shopify.com -- so those are the signs that actually
# fire on a real store, and guessing at plausible header names instead is how a
# detector reports "unknown" for the most identifiable platform on the web.
HEADER_SIGNS: tuple[tuple[str, str, Platform], ...] = (
    ("set-cookie", "_shopify_", Platform.SHOPIFY),
    ("link", "cdn.shopify.com", Platform.SHOPIFY),
    ("x-shopid", "", Platform.SHOPIFY),
    ("x-shopify-stage", "", Platform.SHOPIFY),
    ("server", "shopify", Platform.SHOPIFY),
    ("x-powered-by", "wordpress", Platform.WORDPRESS),
    ("x-generator", "drupal", Platform.DRUPAL),
    ("x-powered-by", "next.js", Platform.NEXTJS),
    ("x-wix-request-id", "", Platform.WIX),
    ("x-hs-hub-id", "", Platform.HUBSPOT),
    ("x-served-by", "squarespace", Platform.SQUARESPACE),
    ("server", "webflow", Platform.WEBFLOW),
)

GENERATOR_SIGNS: tuple[tuple[str, Platform], ...] = (
    ("woocommerce", Platform.WOOCOMMERCE),
    ("wordpress", Platform.WORDPRESS),
    ("drupal", Platform.DRUPAL),
    ("wix", Platform.WIX),
    ("squarespace", Platform.SQUARESPACE),
    ("magento", Platform.MAGENTO),
    ("hubspot", Platform.HUBSPOT),
)


@dataclass(frozen=True, slots=True)
class Detection:
    """One fact about the site, and how it was established."""

    name: str
    evidence: str
    url: str = ""


@dataclass(slots=True)
class TechProfile:
    """What the site is built on and what it exposes."""

    site_url: str
    platform: Platform = Platform.UNKNOWN
    platform_evidence: str = ""
    # Machine-readable surfaces that answered. Each is citable in an agents.md.
    endpoints: list[Detection] = field(default_factory=list)
    signals: list[Detection] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Everything the fingerprint set matched, whether or not it changed a decision.
    # Shown to the operator as context; only the ecommerce category decides anything.
    technologies: list[str] = field(default_factory=list)
    ecommerce_tech: list[str] = field(default_factory=list)

    @property
    def sells(self) -> bool:
        """Whether the platform is one that transacts.

        Platform, not capability: a WooCommerce site is a shop even before we find
        its cart. `agents_doc` still requires a verified endpoint before writing
        anything about how to buy, so this only widens what may be described, never
        what is claimed.
        """
        # Either source is sufficient. The built-in signs know eleven platforms
        # precisely; the fingerprint set knows thousands and is the reason a
        # BigCartel or PrestaShop store is recognised as a shop at all.
        return self.platform in COMMERCE_PLATFORMS or bool(self.ecommerce_tech)

    @property
    def endpoint_urls(self) -> list[str]:
        return [d.url for d in self.endpoints if d.url]

    def summary(self) -> str:
        parts = [f"Platform: {self.platform.value}"]
        if self.platform_evidence:
            parts.append(f"({self.platform_evidence})")
        if self.endpoints:
            parts.append(f"— {len(self.endpoints)} machine-readable endpoint(s)")
        return " ".join(parts)


# Paths worth asking about, with the label an agents.md would use and the content
# type that makes the answer meaningful. A path answering HTML is a page, not an
# endpoint, which is the same soft-404 rule the agents probe applies.
CANDIDATE_ENDPOINTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("/products.json", "Products (JSON)", ("application/json",)),
    ("/collections.json", "Collections (JSON)", ("application/json",)),
    ("/wp-json/", "WordPress REST API", ("application/json",)),
    ("/wp-json/wp/v2/pages", "Pages (REST)", ("application/json",)),
    ("/sitemap.xml", "Sitemap", ("application/xml", "text/xml")),
    ("/sitemap_index.xml", "Sitemap index", ("application/xml", "text/xml")),
    ("/feed/", "RSS feed", ("application/rss+xml", "application/xml", "text/xml")),
    ("/rss.xml", "RSS feed", ("application/rss+xml", "application/xml", "text/xml")),
    ("/openapi.json", "OpenAPI description", ("application/json",)),
    ("/.well-known/ai-plugin.json", "AI plugin manifest", ("application/json",)),
)


# Fingerprint names that map onto a platform this tool models. Anything else the
# dataset finds is recorded but does not steer the profile: knowing a site runs
# Cloudflare or jQuery changes nothing an agent would do.
FINGERPRINT_PLATFORMS: dict[str, Platform] = {
    "Shopify": Platform.SHOPIFY,
    "WooCommerce": Platform.WOOCOMMERCE,
    "WordPress": Platform.WORDPRESS,
    "Wix": Platform.WIX,
    "Squarespace": Platform.SQUARESPACE,
    "Webflow": Platform.WEBFLOW,
    "BigCommerce": Platform.BIGCOMMERCE,
    "Magento": Platform.MAGENTO,
    "Drupal": Platform.DRUPAL,
    "HubSpot": Platform.HUBSPOT,
    "Next.js": Platform.NEXTJS,
}


def _platform_from_matches(matches) -> tuple[Platform, str] | None:
    """Pick the platform from fingerprint matches, commerce winning ties.

    A WooCommerce site matches WordPress too, and the commerce answer is the one
    that decides whether a transaction may be described -- so it is preferred
    rather than left to dictionary order.
    """
    named = [(m, FINGERPRINT_PLATFORMS[m.name]) for m in matches if m.name in FINGERPRINT_PLATFORMS]
    if not named:
        return None
    named.sort(key=lambda pair: pair[1] not in COMMERCE_PLATFORMS)
    match, platform = named[0]
    return platform, f"fingerprint {match.name} ({match.evidence})"


def platform_from_headers(headers: dict[str, str]) -> tuple[Platform, str]:
    lowered = {k.lower(): (v or "").lower() for k, v in headers.items()}
    for header, needle, platform in HEADER_SIGNS:
        value = lowered.get(header)
        if value is None:
            continue
        if not needle or needle in value:
            # Quote the part that matched, not the first 60 characters. httpx joins
            # repeated headers, so a store matching on `_shopify_` was being
            # evidenced with an unrelated `localization=us` cookie -- evidence an
            # operator cannot verify is worse than none, because it looks checked.
            if needle:
                at = value.find(needle)
                excerpt = value[max(0, at - 15) : at + len(needle) + 30]
                shown = f"{header}: ...{excerpt}..."
            else:
                shown = header
            return platform, f"response header {shown}"
    return Platform.UNKNOWN, ""


def platform_from_html(html: str) -> tuple[Platform, str]:
    match = GENERATOR_META.search(html or "")
    if not match:
        return Platform.UNKNOWN, ""
    content = match.group(1)
    lowered = content.lower()
    for needle, platform in GENERATOR_SIGNS:
        if needle in lowered:
            return platform, f'generator meta tag "{content[:60]}"'
    return Platform.UNKNOWN, ""


async def _check(
    client: httpx.AsyncClient, origin: str, path: str, label: str, expected: tuple[str, ...]
) -> Detection | None:
    url = origin.rstrip("/") + path
    try:
        response = await client.get(url)
    except httpx.HTTPError:
        return None
    if response.status_code >= 400:
        return None

    content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    # An HTML answer is the catch-all route, not the endpoint. Without this every
    # SPA would appear to expose every API in the list.
    if "html" in content_type or (expected and content_type not in expected):
        return None
    return Detection(name=label, evidence=f"{response.status_code} {content_type}", url=url)


async def probe_tech(
    site_url: str, user_agent: str, timeout: float = DEFAULT_TIMEOUT
) -> TechProfile:
    """Identify the platform and the machine-readable surfaces it exposes."""
    origin = site_url.rstrip("/")
    profile = TechProfile(site_url=origin)

    async with httpx.AsyncClient(
        follow_redirects=True, timeout=timeout, headers={"User-Agent": user_agent}
    ) as client:
        try:
            home = await client.get(origin + "/")
        except httpx.HTTPError as exc:
            profile.notes.append(f"Could not load the homepage: {type(exc).__name__}")
            return profile

        # The community fingerprint set first, since it covers thousands of
        # technologies against eleven here. The built-in signs stay as the
        # fallback for when the dataset cannot be fetched -- an audit that runs
        # with less is better than one that does not run.
        try:
            matches = detect(origin, dict(home.headers), home.text)
        except Exception as exc:
            matches = []
            profile.notes.append(f"Technology fingerprints unavailable: {type(exc).__name__}")

        profile.technologies = [m.name for m in matches]
        profile.ecommerce_tech = [m.name for m in matches if m.is_ecommerce]
        for match in matches:
            profile.signals.append(Detection(name=match.name, evidence=match.evidence))

        platform, evidence = platform_from_headers(dict(home.headers))
        if platform is Platform.UNKNOWN and matches:
            named = _platform_from_matches(matches)
            if named is not None:
                platform, evidence = named
        if platform is Platform.UNKNOWN:
            # The whole document, not a window. Measured: the generator tag on
            # prosperitymedia.com.au sits at byte 301,922, behind ~300KB of
            # inlined critical CSS, and a 200KB cap reported the site as unknown.
            platform, evidence = platform_from_html(home.text)
        profile.platform = platform
        profile.platform_evidence = evidence

        found = await asyncio.gather(
            *(
                _check(client, origin, path, label, expected)
                for path, label, expected in CANDIDATE_ENDPOINTS
            )
        )

    seen: set[str] = set()
    for detection in found:
        if detection is None or detection.name in seen:
            continue
        seen.add(detection.name)
        profile.endpoints.append(detection)

    # WooCommerce hides behind WordPress: the generator tag says WordPress and the
    # store is a plugin. Products answering is the evidence that separates them,
    # and it upgrades the platform because it decides whether a transaction may be
    # described at all.
    if profile.platform is Platform.WORDPRESS and any(
        "products" in d.name.lower() for d in profile.endpoints
    ):
        profile.platform = Platform.WOOCOMMERCE
        profile.platform_evidence += "; /products.json answers, so the store is live"

    if profile.platform is Platform.UNKNOWN:
        profile.notes.append(
            "No platform identified from headers or the generator tag. This is common on "
            "custom and headless sites and is not a fault; it only means the profile has "
            "to be chosen rather than detected."
        )

    return profile
