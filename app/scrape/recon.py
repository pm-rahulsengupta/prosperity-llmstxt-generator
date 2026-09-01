"""Site reconnaissance: robots.txt, sitemaps, and URL path-template clustering.

This runs before anything is crawled and costs one or two HTTP requests. Its job is
to turn "here are 40,000 URLs" into "here are 23 shapes of page, with counts", which
is the only form a crawl plan can sensibly be written against -- by a human or by a
model. Sending 40,000 raw URLs to an LLM is both unaffordable and less useful than
sending the shapes.

Parsing and clustering are pure functions over text. Only `discover` touches the
network, so the interesting logic is testable offline.
"""

from __future__ import annotations

import re
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

# A path node with at least this many distinct children is a variable segment
# (/blog/post-a, /blog/post-b, ...) rather than a fixed one (/docs, /pricing).
COLLAPSE_MIN_CHILDREN = 5

_NUMERIC = re.compile(r"^\d+$")
_YEAR = re.compile(r"^(19|20)\d{2}$")
_DATE = re.compile(r"^(19|20)\d{2}-\d{2}(-\d{2})?$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_HAS_DIGITS_AND_WORDS = re.compile(r"^[a-z0-9]+(-[a-z0-9]+){2,}$", re.I)


#: Statuses that mean a server refused us, as opposed to not having the file.
#:
#: An edge WAF answers 403 to `robots.txt` exactly as it answers 403 to a page, so
#: without this distinction a blocked site is indistinguishable from a site that
#: publishes no sitemap -- and the run reports the client's site as empty when the
#: truth is that we were denied at the door. Measured on nrma.com.au: 403 from the
#: Railway worker and 200 from an Australian residential address, on the same
#: static text file, with `server: AkamaiGHost` on the refusal.
#:
#: 404 and 410 are deliberately absent. Those really are "no such file", which is
#: the ordinary case `fetch_robots` already handles correctly.
BLOCKED_STATUSES = frozenset({401, 403, 405, 406, 429, 451, 503})

#: `Server` values that name the thing doing the blocking. Worth reporting because
#: the remedy differs: an IP-reputation deny needs a different address, a bot
#: challenge needs a different fetcher, and an operator cannot tell which from a
#: bare 403.
_WAF_HINTS = (
    ("akamai", "Akamai"),
    ("imperva", "Imperva"),
    ("incapsula", "Imperva Incapsula"),
    ("cloudflare", "Cloudflare"),
    ("awselb", "AWS ELB"),
    ("cloudfront", "CloudFront"),
    ("sucuri", "Sucuri"),
    ("barracuda", "Barracuda"),
    ("f5", "F5 BIG-IP"),
    ("fastly", "Fastly"),
)


def name_blocker(server: str) -> str:
    """A recognisable name for whatever refused us, or the raw `Server` value."""
    lowered = (server or "").lower()
    for needle, label in _WAF_HINTS:
        if needle in lowered:
            return label
    return server or "unidentified"


@dataclass(slots=True, frozen=True)
class BlockedFetch:
    """One request a site refused, kept so the run can say so in words."""

    url: str
    status: int
    server: str = ""

    @property
    def blocker(self) -> str:
        return name_blocker(self.server)

    def describe(self) -> str:
        return f"{self.url} returned {self.status} ({self.blocker})"


@dataclass(slots=True)
class RobotsInfo:
    sitemaps: list[str] = field(default_factory=list)
    disallowed: list[str] = field(default_factory=list)
    # `Allow` overrides `Disallow` for the same path in every major crawler --
    # a rule `crawl_rules.py` already states and this file used to drop on the
    # floor. Without it a site that blocks a directory and re-permits one path
    # inside it has that path silently skipped.
    allowed: list[str] = field(default_factory=list)
    crawl_delay: float | None = None
    fetched: bool = False


@dataclass(slots=True)
class PathTemplate:
    """One shape of URL, with how many pages share it."""

    template: str
    count: int
    examples: list[str] = field(default_factory=list)
    max_depth: int = 0

    @property
    def is_variable(self) -> bool:
        return "{" in self.template


@dataclass(slots=True)
class SiteRecon:
    site_url: str
    robots: RobotsInfo
    urls: list[str] = field(default_factory=list)
    templates: list[PathTemplate] = field(default_factory=list)
    sitemap_count: int = 0
    notes: list[str] = field(default_factory=list)
    # url -> the sitemap that listed it. On a flat site (WordPress, most Shopify
    # themes) every URL clusters to a single /{slug} template and path shape carries
    # no signal at all, but the sitemap split does: post-sitemap, page-sitemap,
    # category-sitemap. Provenance is often the better planning axis of the two.
    url_sources: dict[str, str] = field(default_factory=dict)
    # url -> how many sitemaps listed it. Kept alongside the resolved source
    # rather than discarded once the collision is settled: a URL that appears in
    # five named sitemaps is usually a genuine hub, and that is a promotion
    # signal in its own right rather than only a tiebreak to be resolved away.
    url_memberships: dict[str, int] = field(default_factory=dict)
    #: Every request the site refused during discovery. Empty is the normal case
    #: and the only one the planner cares about; a non-empty list is the finding.
    blocked: list[BlockedFetch] = field(default_factory=list)

    @property
    def shut_out(self) -> bool:
        """Refused, and nothing to show for the attempt.

        The distinction that matters: a 403 on `robots.txt` alone is a nuisance --
        the conventional sitemap paths may still answer. A 403 on robots.txt *and*
        no URLs from any sitemap means we never got in, and every number derived
        from this recon describes our access rather than the client's site.
        """
        return bool(self.blocked) and not self.urls

    def blockers(self) -> list[str]:
        """The distinct names of whatever refused us, in first-seen order."""
        seen: list[str] = []
        for block in self.blocked:
            if block.blocker not in seen:
                seen.append(block.blocker)
        return seen

    def blocked_summary(self) -> str:
        """One sentence an operator can act on, or empty when nothing was refused."""
        if not self.blocked:
            return ""
        statuses = sorted({block.status for block in self.blocked})
        codes = ", ".join(str(status) for status in statuses)
        return (
            f"{len(self.blocked)} request(s) refused by {' / '.join(self.blockers())} "
            f"(HTTP {codes}). This is a block on us, not a gap on the site."
        )

    def multi_listed(self, url: str) -> int:
        """How many sitemaps list this URL. 1 when unknown, never 0."""
        return self.url_memberships.get(url, 1)

    def sitemap_groups(self) -> list[tuple[str, int]]:
        """(sitemap name, url count), most populous first."""
        counts = Counter(self.url_sources.values())
        return [(_sitemap_label(name), n) for name, n in counts.most_common()]


def parse_robots(text: str) -> RobotsInfo:
    """Extract sitemap references, the `*` group's rules, and crawl-delay.

    Only the `*` user-agent group is read for disallows: this tool identifies itself
    as its own agent, and a site that blocks a named bot but not `*` is not telling
    us to stay out.
    """
    info = RobotsInfo(fetched=True)
    in_star_group = False

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field_name, _, value = line.partition(":")
        field_name = field_name.strip().lower()
        value = value.strip()

        if field_name == "sitemap" and value:
            info.sitemaps.append(value)
        elif field_name == "user-agent":
            in_star_group = value == "*"
        elif field_name == "disallow" and in_star_group and value:
            info.disallowed.append(value)
        elif field_name == "allow" and in_star_group and value:
            info.allowed.append(value)
        elif field_name == "crawl-delay" and in_star_group:
            with suppress(ValueError):
                info.crawl_delay = float(value)

    return info


def parse_sitemap(xml_text: str) -> tuple[list[str], list[str]]:
    """Return (page urls, nested sitemap urls).

    Handles both `<urlset>` and `<sitemapindex>`, with or without a namespace, since
    real sitemaps are inconsistent about it.
    """
    try:
        root = ElementTree.fromstring(xml_text.strip())
    except ElementTree.ParseError:
        return [], []

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    pages: list[str] = []
    nested: list[str] = []
    is_index = local(root.tag) == "sitemapindex"

    for child in root:
        if local(child.tag) not in {"url", "sitemap"}:
            continue
        for node in child:
            if local(node.tag) == "loc" and node.text:
                (nested if is_index else pages).append(node.text.strip())

    return pages, nested


def classify_segment(segment: str) -> str | None:
    """Name the variable a path segment represents, or None if it looks fixed."""
    if not segment:
        return None
    if _NUMERIC.match(segment):
        return "{year}" if _YEAR.match(segment) else "{id}"
    if _DATE.match(segment):
        return "{date}"
    if _UUID.match(segment):
        return "{uuid}"
    if _HAS_DIGITS_AND_WORDS.match(segment):
        return "{slug}"
    return None


def cluster_urls(urls: list[str], collapse_min: int = COLLAPSE_MIN_CHILDREN) -> list[PathTemplate]:
    """Group URLs into path templates, most populous first.

    A segment position collapses to a placeholder when it has many distinct values
    under the same prefix -- that is what distinguishes /blog/{slug} (hundreds of
    posts) from /docs/pricing (one page that happens to sit two deep).
    """
    paths = [_path_segments(u) for u in urls]

    # children[prefix] = set of distinct next segments seen under that prefix
    children: dict[tuple[str, ...], set[str]] = {}
    for segments in paths:
        for i in range(len(segments)):
            children.setdefault(tuple(segments[:i]), set()).add(segments[i])

    grouped: dict[str, list[str]] = {}
    for url, segments in zip(urls, paths, strict=True):
        template_parts: list[str] = []
        prefix: list[str] = []
        for segment in segments:
            siblings = children.get(tuple(prefix), set())
            if len(siblings) >= collapse_min:
                template_parts.append(classify_segment(segment) or "{slug}")
            else:
                template_parts.append(segment)
            # Walk the concrete path, not the templated one, or the prefix keys
            # stop matching what was recorded above.
            prefix.append(segment)
        template = "/" + "/".join(template_parts) if template_parts else "/"
        grouped.setdefault(template, []).append(url)

    templates = [
        PathTemplate(
            template=template,
            count=len(members),
            examples=members[:3],
            max_depth=max(len(_path_segments(m)) for m in members),
        )
        for template, members in grouped.items()
    ]
    templates.sort(key=lambda t: (-t.count, t.template))
    return templates


def summarise_for_plan(recon: SiteRecon, max_templates: int = 60) -> str:
    """Compact, token-cheap inventory for the crawl-planning prompt."""
    lines = [
        f"Site: {recon.site_url}",
        f"URLs discovered: {len(recon.urls)} across {recon.sitemap_count} sitemap(s)",
    ]
    if recon.robots.disallowed:
        shown = ", ".join(recon.robots.disallowed[:15])
        lines.append(f"robots.txt disallows (user-agent *): {shown}")
    if recon.robots.crawl_delay:
        lines.append(f"robots.txt crawl-delay: {recon.robots.crawl_delay}s")

    groups = recon.sitemap_groups()
    if len(groups) > 1:
        lines.append("")
        lines.append("Sitemaps, most populous first:")
        lines.extend(f"  {count:>6}  {name}" for name, count in groups)

    lines.append("")
    lines.append("URL templates, most populous first:")
    for template in recon.templates[:max_templates]:
        example = template.examples[0] if template.examples else ""
        lines.append(f"  {template.count:>6}  {template.template}   e.g. {example}")

    hidden = len(recon.templates) - max_templates
    if hidden > 0:
        lines.append(f"  ... {hidden} further template(s) not shown")

    return "\n".join(lines)


def extension_counts(urls: list[str]) -> Counter[str]:
    """File extensions present, for spotting asset URLs that leaked into a sitemap."""
    counts: Counter[str] = Counter()
    for url in urls:
        path = urlparse(url).path
        _, _, tail = path.rpartition("/")
        if "." in tail:
            counts[tail.rsplit(".", 1)[-1].lower()] += 1
    return counts


def _sitemap_label(sitemap_url: str) -> str:
    """The filename, which is what carries the meaning: "post-sitemap.xml"."""
    tail = urlparse(sitemap_url).path.rsplit("/", 1)[-1]
    return tail or sitemap_url


def _path_segments(url: str) -> list[str]:
    path = urlparse(url).path.strip("/")
    return [s for s in path.split("/") if s] if path else []


def sitemap_candidates(site_url: str, robots: RobotsInfo) -> list[str]:
    """Sitemaps to try: those robots.txt names, then the conventional locations."""
    candidates = list(robots.sitemaps)
    for path in ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml"):
        candidate = urljoin(site_url, path)
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates
