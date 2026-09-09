"""A markdown version of each page, at a path an agent can guess.

llms.txt v2 says the links in an llms.txt "should therefore point to LLM-friendly
content, such as the markdown versions of pages". Until this module existed this
tool had no way to honour that: every link pointed at the HTML page, and an agent
following one paid to strip navigation, advertising and script out of a document
we had already stripped once during the crawl and then thrown away.

The bodies here are the same `PageEntry.markdown` that `llms-full.txt` is built
from. Nothing is re-fetched and nothing is re-parsed, which is what makes the two
files consistent by construction rather than by a check.

## Two naming conventions, and why both

The spec permits `.md` appended (`page.html.md`) or the extension replaced
(`page.md`), and directory URLs take `index.md`. Which one a site can serve is a
fact about its host, not about its content: a static host that maps
`/about/` to `about/index.html` wants `about/index.md`, and a host serving
`/about.html` wants `about.html.md`. `SUFFIX` and `REPLACE` are therefore both
produced from the same body, and `layout_for` picks by what the crawl saw the
site actually serve.

## Why the link rewrite is conditional

`rewrite_links` exists to point llms.txt at these files, and it is deliberately
awkward to call: it takes the set of paths that were actually generated, and
rewrites only those. A link to `/about.md` on a site that never publishes the
directory is worse than the HTML link it replaced -- the agent spends a request
to learn we were guessing. That is the same rule the component registry states
about templates, applied to a URL instead of a file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from app.core.full_text import normalise_body
from app.core.models import PageEntry
from app.core.text import domain_of

__all__ = [
    "PAGE_EXTENSIONS",
    "REPLACE",
    "SUFFIX",
    "MarkdownPages",
    "layout_for",
    "md_path_for",
    "render_md_pages",
    "render_page_md",
    "rewrite_links",
]

#: `/about/` -> `/about/index.md`, `/a/b.html` -> `/a/b.md`. What a static host
#: that already maps directories to `index.html` will serve without configuration.
REPLACE = "replace"
#: `/about/` -> `/about/index.html.md`, `/a/b.html` -> `/a/b.html.md`. What a host
#: serving the URL verbatim will match, because the original path is still a prefix.
SUFFIX = "suffix"

#: Extensions we treat as "this URL names a document", so replacing the extension
#: is meaningful. Anything else is treated as a directory path.
PAGE_EXTENSIONS = (".html", ".htm", ".php", ".aspx", ".asp", ".jsp")


@dataclass(slots=True)
class MarkdownPages:
    """The generated directory, and what it could not cover.

    `skipped` is not an error list. A page with no crawled body is a page the
    crawl could not read -- already counted and disclosed elsewhere -- and this
    module records it again only so the count in the bundle matches the count in
    llms.txt rather than silently differing by three.
    """

    layout: str
    files: dict[str, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.files)

    @property
    def paths(self) -> frozenset[str]:
        """Site-absolute paths, as an agent would request them."""
        return frozenset(f"/{name}" for name in self.files)


def _path_of(url: str) -> str:
    path = urlsplit(url).path or "/"
    return path


def md_path_for(url: str, layout: str = REPLACE) -> str:
    """The site-absolute path of the markdown twin of `url`.

    Always returns a path under the site root with no leading component of its
    own, so the caller can join it to a directory or to a domain without either
    knowing what the other did.
    """
    path = _path_of(url)
    lowered = path.lower()

    if path.endswith("/") or "." not in path.rsplit("/", 1)[-1]:
        base = path.rstrip("/") + "/index.html"
    else:
        base = path

    if layout == SUFFIX:
        return base + ".md"

    for ext in PAGE_EXTENSIONS:
        if lowered.endswith(ext) or base.lower().endswith(ext):
            return base[: -len(ext)] + ".md"
    return base + ".md"


def layout_for(pages: list[PageEntry]) -> str:
    """Pick the naming convention from what the site already serves.

    A site whose URLs carry `.html` is being served file-for-file by something,
    and `page.html.md` sits beside `page.html` without a rewrite rule. A site of
    extensionless directory URLs is being routed, and `index.md` is what a router
    already resolving `/about/` will find.

    Ties go to `REPLACE`, which is the form the spec lists first and the form
    that reads better in an llms.txt a human will also open.
    """
    if not pages:
        return REPLACE
    explicit = sum(1 for p in pages if _path_of(p.url).lower().endswith(PAGE_EXTENSIONS))
    return SUFFIX if explicit * 2 > len(pages) else REPLACE


def render_page_md(page: PageEntry, canonical: str = "") -> str:
    """One page as markdown, with the source URL stated in the document.

    The canonical link is not decoration. These files are designed to be fetched
    on their own, out of the context of the llms.txt that named them, and a
    markdown document with no URL in it cannot be cited -- an agent that quotes
    it has nothing to attribute the quote to. `agents.md` already asks agents to
    cite the source page; this is what makes that possible for a file that is not
    the source page.
    """
    url = canonical or page.url
    title = (page.title or page.h1 or "").strip()

    lines: list[str] = []
    if title:
        lines.append(f"# {title}\n")
    if page.description.strip():
        lines.append(f"> {page.description.strip()}\n")
    lines.append(f"Source: {url}\n")

    body = normalise_body(page.markdown or "")
    if body:
        lines.append(body)
    return "\n".join(lines).rstrip() + "\n"


def render_md_pages(pages: list[PageEntry], layout: str | None = None) -> MarkdownPages:
    """The whole directory, keyed by path relative to the site root.

    Pages with no body are skipped rather than emitted empty. An empty markdown
    file published at a guessable path is the worst of both worlds: the agent
    finds it, reads nothing, and has no way to tell an empty page from a page we
    failed to read.
    """
    chosen = layout or layout_for(pages)
    out = MarkdownPages(layout=chosen)

    for page in pages:
        if not (page.markdown or "").strip():
            out.skipped.append(page.url)
            continue
        name = md_path_for(page.url, chosen).lstrip("/")
        # A collision means two URLs share a markdown path -- `/a` and `/a/`, or
        # `/a.html` and `/a.php`. First wins and the second is recorded, because
        # silently overwriting would make the file count right and the contents
        # wrong.
        if name in out.files:
            out.skipped.append(page.url)
            continue
        out.files[name] = render_page_md(page)

    return out


def rewrite_links(llms_txt: str, published: frozenset[str], site_url: str = "") -> str:
    """Point each llms.txt link at its markdown twin, where one will exist.

    Only rewrites a link whose markdown path is in `published`. Everything else
    is left pointing at the HTML page, which is correct rather than merely safe:
    the HTML page is what that content is served as.
    """
    if not published:
        return llms_txt

    # `domain_of`, not the raw netloc. The site is filed as `redspot.com.au` and
    # every page it links to is `www.redspot.com.au`, so comparing hosts verbatim
    # rejected all 419 links and rewrote none of them -- silently, because
    # "nothing matched" and "nothing needed changing" produce the same file.
    #
    # This is the rule `text.domain_of` exists to state and the same one `58b4107`
    # applied to the tables: a site has one spelling of its domain, and the `www.`
    # is not part of it.
    site = domain_of(site_url) if site_url else ""

    out: list[str] = []
    for line in llms_txt.splitlines():
        out.append(_rewrite_line(line, published, site))
    return "\n".join(out) + ("\n" if llms_txt.endswith("\n") else "")


def _rewrite_line(line: str, published: frozenset[str], site: str) -> str:
    stripped = line.lstrip()
    if not stripped.startswith("- ["):
        return line
    open_paren = line.find("](")
    if open_paren == -1:
        return line
    close = line.find(")", open_paren)
    if close == -1:
        return line

    url = line[open_paren + 2 : close]
    parts = urlsplit(url)
    if site and domain_of(url) != site:
        return line

    md = md_path_for(url, REPLACE)
    if md not in published:
        md = md_path_for(url, SUFFIX)
        if md not in published:
            return line

    replaced = urlunsplit((parts.scheme, parts.netloc, md, parts.query, parts.fragment))
    return line[: open_paren + 2] + replaced + line[close:]
