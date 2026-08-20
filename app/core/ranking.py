"""Page prioritisation, Optional classification and section templates.

The scoring weights, thresholds and section templates are ported unchanged from the
source tool — they are the most valuable thing in it and they are well tuned. What
changed is the handling of *missing* signals, which is where the source quietly
fell over:

- `crawl_depth` defaulted to 0, so every crawled page looked like the homepage.
  `is_optional_page` requires depth >= 4, so `## Optional` was always empty outside
  CSV mode, and `importance_score` returned a constant 25 for every crawled page —
  ranking was inert. Here, unknown depth falls back to the URL's own path depth.
- `link_score` and `word_count` were dropped between stages. `PageEntry` carries
  them; `score_breakdown` exposes each term so a zeroed signal is visible instead
  of silently costing 40% of the weighting.
"""

from __future__ import annotations

from urllib.parse import urlparse

from app.core.models import PageEntry

# --- Optional-section thresholds -------------------------------------------

# Pages this deep in the crawl are candidates for "## Optional".
OPTIONAL_DEPTH_THRESHOLD = 4
# Unique inlinks at or below this signal low internal importance.
OPTIONAL_INLINKS_THRESHOLD = 1
# Link Score at or below this, on Screaming Frog's 0-100 PageRank-like scale.
OPTIONAL_LINK_SCORE_THRESHOLD = 5

# --- Importance weights (must sum to 1.0) ----------------------------------

WEIGHT_LINK_SCORE = 0.4
WEIGHT_INLINKS = 0.3
WEIGHT_DEPTH = 0.2
WEIGHT_CONTENT = 0.1

# --- Output patterns --------------------------------------------------------

PATTERN_CATALOG = "catalog"
PATTERN_WORKFLOW = "workflow"
PATTERN_INDEX_EXPORT = "index_export"
PATTERN_ECOMMERCE = "ecommerce"

CATALOG_SECTIONS = [
    "Getting Started",
    "Core Concepts",
    "Guides",
    "API Reference",
    "Integrations",
    "Resources",
    "Contact",
    "Optional",
]
WORKFLOW_SECTIONS = [
    "Quickstart",
    "Setup & Configuration",
    "Features",
    "Workflows",
    "Troubleshooting",
    "Reference",
    "Contact",
    "Optional",
]
INDEX_EXPORT_SECTIONS = [
    "Overview",
    "Documentation",
    "Tutorials",
    "API",
    "Examples",
    "Contact",
    "Optional",
]
ECOMMERCE_SECTIONS = [
    "Brand Overview",
    "Product Categories",
    "Brand Portfolio",
    "Shopping Help",
    "Customer Service",
    "Store Locator",
    "Important Pages",
    "Contact",
    "Optional",
]


# --- Site-shaped profiles -------------------------------------------------
# The four above are all documentation shapes. Emitting "Overview / Documentation /
# Tutorials / Examples" for a site that is not documentation is the most common
# failure in generated llms.txt files, and we made it: the model chose
# `index_export` for an SEO agency and filed its service pages under "Examples".
# These six describe what a site *is*, not what a docs site looks like.

AGENCY_SERVICES_SECTIONS = [
    "Services",
    "Locations",
    "Industries",
    "Guides and Research",
    "Company",
    "Contact",
    "Optional",
]
SAAS_PRODUCT_SECTIONS = [
    "Product",
    "Documentation",
    "API Reference",
    "Guides",
    "Pricing",
    "Company",
    "Contact",
    "Optional",
]
ECOMMERCE_RETAIL_SECTIONS = [
    "Categories",
    "Buying Guides",
    "Shipping and Returns",
    "Brand",
    "Contact",
    "Optional",
]
PUBLISHER_MEDIA_SECTIONS = [
    "Sections",
    "Flagship Reporting",
    "About and Standards",
    "Contact",
    "Optional",
]
LOCAL_MULTI_LOCATION_SECTIONS = [
    "Services",
    "Locations",
    "Company",
    "Contact",
    "Optional",
]
PROFESSIONAL_SERVICES_SECTIONS = [
    "Practice Areas",
    "People",
    "Insights",
    "Company",
    "Contact",
    "Optional",
]

PATTERN_AGENCY = "agency_services"
PATTERN_SAAS = "saas_product"
PATTERN_ECOMMERCE_RETAIL = "ecommerce_retail"
PATTERN_PUBLISHER = "publisher_media"
PATTERN_LOCAL = "local_multi_location"
PATTERN_PROFESSIONAL = "professional_services"

PATTERN_TEMPLATES = {
    # Site-shaped profiles first: these fit most client work, and the order here is
    # the order the planning prompt shows them in.
    PATTERN_AGENCY: AGENCY_SERVICES_SECTIONS,
    PATTERN_PROFESSIONAL: PROFESSIONAL_SERVICES_SECTIONS,
    PATTERN_LOCAL: LOCAL_MULTI_LOCATION_SECTIONS,
    PATTERN_ECOMMERCE_RETAIL: ECOMMERCE_RETAIL_SECTIONS,
    PATTERN_PUBLISHER: PUBLISHER_MEDIA_SECTIONS,
    PATTERN_SAAS: SAAS_PRODUCT_SECTIONS,
    # Documentation shapes. Correct for a docs site and wrong for everything else.
    PATTERN_CATALOG: CATALOG_SECTIONS,
    PATTERN_WORKFLOW: WORKFLOW_SECTIONS,
    PATTERN_INDEX_EXPORT: INDEX_EXPORT_SECTIONS,
    PATTERN_ECOMMERCE: ECOMMERCE_SECTIONS,
}

PATTERN_LABELS = {
    PATTERN_AGENCY: "Agency / services business (SEO, marketing, consulting)",
    PATTERN_PROFESSIONAL: "Professional services (law, accounting, advisory)",
    PATTERN_LOCAL: "Local or multi-location business (trades, clinics, franchises)",
    PATTERN_ECOMMERCE_RETAIL: "E-commerce / retail (product catalogue)",
    PATTERN_PUBLISHER: "Publisher / media (news, magazine, editorial)",
    PATTERN_SAAS: "SaaS product site (product, pricing, docs)",
    PATTERN_CATALOG: "SaaS / API Platform (Stripe, Cloudflare)",
    PATTERN_WORKFLOW: "Developer Tool / IDE (Cursor, Windsurf)",
    PATTERN_INDEX_EXPORT: "Documentation / AI-Native (Anthropic, LangGraph)",
    PATTERN_ECOMMERCE: "E-Commerce / Retail (Strandbags, Nike)",
}

CONTACT_URL_KEYWORDS = frozenset(
    {
        "contact",
        "customer-service",
        "customer-support",
        "store-locator",
        "find-a-store",
        "locations",
        "help-centre",
        "help-center",
        "support",
    }
)
CONTACT_TITLE_KEYWORDS = ("contact", "store locator", "customer service")

# Sections positioned explicitly rather than by template order.
# "Main" is the bucket the deterministic fallback puts the homepage in. Sorting it
# alphabetically buries the single most important page halfway down the file.
PINNED_FIRST = ("Main",)
PINNED_LAST = ("Contact", "Optional")


def template_order(pattern: str) -> list[str]:
    return PATTERN_TEMPLATES.get(pattern, CATALOG_SECTIONS)


def effective_depth(page: PageEntry) -> int:
    """Crawl depth, falling back to URL path depth when it is unknown.

    A crawler that records its own traversal depth sets this directly. A CSV
    without a Crawl Depth column, or a page reached out of band, leaves it at -1 —
    and the URL's own path depth is a serviceable proxy. Returning 0 here (as the
    source did) claims every such page is the homepage.
    """
    if page.crawl_depth >= 0:
        return page.crawl_depth
    path = urlparse(page.url).path.strip("/")
    return len([p for p in path.split("/") if p])


def score_breakdown(page: PageEntry) -> dict[str, float]:
    """Per-term contributions, so a zeroed signal is visible rather than silent.

    Link Score and Unique Inlinks come from a Screaming Frog export. On a crawl-only
    run both are zero, and weighting them anyway spends 70% of the composite on two
    constants -- every page then scores on depth and word count alone, and the
    ranking is close to meaningless. So the weights are renormalised across the
    signals a run actually has. A page ranked against its real peers on 30% of the
    intended evidence is better than one ranked on 100% of nothing.
    """
    depth = effective_depth(page)
    has_link_graph = page.link_score > 0 or page.unique_inlinks > 0

    terms = {
        "depth": (max(0, 100 - depth * 20), WEIGHT_DEPTH),
        "content": (
            min(page.word_count / 10, 100) if page.word_count > 0 else 50,
            WEIGHT_CONTENT,
        ),
    }
    if has_link_graph:
        terms["link_score"] = (page.link_score, WEIGHT_LINK_SCORE)
        terms["inlinks"] = (min(page.unique_inlinks * 10, 100), WEIGHT_INLINKS)

    total_weight = sum(weight for _, weight in terms.values())
    return {name: value * (weight / total_weight) for name, (value, weight) in terms.items()}


def importance_score(page: PageEntry) -> float:
    """Composite importance. Higher is more important."""
    return sum(score_breakdown(page).values())


def is_optional_page(page: PageEntry) -> bool:
    """Deep *and* weakly linked. Both must hold, as in the source."""
    is_deep = effective_depth(page) >= OPTIONAL_DEPTH_THRESHOLD
    is_low_importance = page.unique_inlinks <= OPTIONAL_INLINKS_THRESHOLD or (
        0 < page.link_score <= OPTIONAL_LINK_SCORE_THRESHOLD
    )
    return is_deep and is_low_importance


def is_contact_page(page: PageEntry) -> bool:
    url_lower = page.url.lower()
    title_lower = page.display_title.lower()
    return any(kw in url_lower for kw in CONTACT_URL_KEYWORDS) or any(
        kw in title_lower for kw in CONTACT_TITLE_KEYWORDS
    )


def url_to_section(url: str) -> str:
    """Derive a section name from the first path segment. Deterministic fallback."""
    path = urlparse(url).path.strip("/")
    if not path:
        return "Main"
    return path.split("/")[0].replace("-", " ").replace("_", " ").title()


def sort_by_importance(pages: list[PageEntry]) -> list[PageEntry]:
    return sorted(pages, key=importance_score, reverse=True)


# Pages a file is not useful without. A model asked "who are these people and how do
# I contact them" has nowhere else to go, so these can never be dropped and can never
# be marked Optional -- Optional means "safe to ignore when context is tight".
IDENTITY_URL_PATTERNS = (
    "/about",
    "/contact",
    "/case-stud",
    "/our-work",
    "/portfolio",
    "/team",
    "/testimonial",
    "/clients",
    "/pricing",
    "/services",
)
IDENTITY_TITLE_KEYWORDS = ("about", "contact", "case stud", "our work", "pricing")


def is_identity_page(page: PageEntry) -> bool:
    """True for a page the file would be incomplete without.

    The homepage counts: it is the single most useful link in the file and, at depth
    zero, no heuristic here would ever have flagged it.
    """
    path = urlparse(page.url).path.strip("/")
    if not path:
        return True
    lowered = f"/{path.lower()}"
    if any(pattern in lowered for pattern in IDENTITY_URL_PATTERNS):
        return True
    title = page.display_title.lower()
    return any(keyword in title for keyword in IDENTITY_TITLE_KEYWORDS)
