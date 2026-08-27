"""Whether a sitemap actually leads anywhere.

The readiness check for `/sitemap.xml` used to ask two questions -- did it answer
below 400, and was it served as XML -- and then pass. It never opened the file.
The component is titled "live and reachable", and it was verifying neither.

Measured on opencorp.com.au, which is why this exists. Its sitemap answers
`200 text/xml` and is a valid `<sitemapindex>` naming thirteen children. Every
one of them points at `ocnewstg.staging.tempurl.host` -- a staging host that
returns `401 Password Protected` and whose robots.txt is `Disallow: /`. A WordPress
sitemap generated on staging and never rewritten for production. Zero production
URLs are reachable through it, and the checklist called it Published.

That is worse than an absent sitemap. An absent one is visible; this one passes
every surface test while a crawler following it collects thirteen 401s, and a
staging hostname is published to anyone who reads it.

**What it checks, and what it cannot.** Reading the index settles the OpenCorp
class for free -- the hosts are right there in the document. Following children
is what settles "on-host but empty", and it costs requests, so it is bounded and
optional. `children=None` means they were not followed, and the verdict says so
rather than implying an empty sitemap was inspected and approved.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from app.scrape.recon import parse_sitemap

__all__ = ["SitemapVerdict", "judge_sitemap", "same_site"]


@dataclass(frozen=True, slots=True)
class SitemapVerdict:
    """Whether the sitemap leads to this site's pages, and why not if it does not."""

    ok: bool
    detail: str
    #: Pages found on this site, when they were counted. `None` means the child
    #: sitemaps were not followed, which is not the same as finding none.
    url_count: int | None = None


def _host(url: str) -> str:
    """Comparable host: lowercased, `www.` dropped, port ignored."""
    netloc = urlparse(url.strip()).netloc.lower()
    return netloc.split("@")[-1].split(":")[0].removeprefix("www.")


def same_site(url: str, site_url: str) -> bool:
    """Whether `url` belongs to the site being audited.

    Subdomains count as the same site -- `assets.example.com` serving a sitemap
    for `example.com` is normal. A wholly different registrable name is not, and
    `ocnewstg.staging.tempurl.host` is not a subdomain of `opencorp.com.au` by
    any reading.
    """
    host, site = _host(url), _host(site_url)
    if not host or not site:
        return False
    return host == site or host.endswith("." + site) or site.endswith("." + host)


def judge_sitemap(
    body: str,
    site_url: str,
    children: dict[str, str | None] | None = None,
) -> SitemapVerdict:
    """Decide whether this sitemap leads to pages on this site.

    `children` maps a child sitemap URL to its body, `None` for one that could
    not be fetched. Passing `children=None` means they were not followed at all;
    the verdict then reports what the index says and stops short of claiming the
    pages were counted.
    """
    pages, nested = parse_sitemap(body)

    if not pages and not nested:
        # Valid XML that is not a sitemap, or a sitemap with nothing in it. Both
        # are the same finding for a crawler: there is nothing here to follow.
        return SitemapVerdict(False, "no URLs and no nested sitemaps in it", 0)

    if nested:
        on_site = [u for u in nested if same_site(u, site_url)]
        if not on_site:
            elsewhere = _host(nested[0])
            return SitemapVerdict(
                False,
                f"lists {len(nested)} sitemap(s), all on {elsewhere} rather than this site",
                0,
            )

        if children is None:
            return SitemapVerdict(
                True,
                f"index of {len(on_site)} sitemap(s) on this site; they were not opened",
                None,
            )

        counted = 0
        unreachable = 0
        for url in on_site:
            child = children.get(url)
            if child is None:
                unreachable += 1
                continue
            child_pages, _ = parse_sitemap(child)
            counted += sum(1 for p in child_pages if same_site(p, site_url))

        if counted:
            return SitemapVerdict(True, f"{counted:,} URL(s) across its sitemaps", counted)
        if unreachable:
            return SitemapVerdict(
                False,
                f"{unreachable} of its {len(on_site)} sitemap(s) could not be fetched, "
                "and the rest list no URLs on this site",
                0,
            )
        return SitemapVerdict(False, "its sitemaps list no URLs on this site", 0)

    on_site = [u for u in pages if same_site(u, site_url)]
    if not on_site:
        return SitemapVerdict(
            False,
            f"lists {len(pages)} URL(s), all on {_host(pages[0])} rather than this site",
            0,
        )
    return SitemapVerdict(True, f"{len(on_site):,} URL(s)", len(on_site))
