"""MD-001..005, INF-001..007 and OKF-001..006 — the three artifacts added for v2.

Every other rule set in this package judges a single markdown or text file. Two
of these three judge a **directory**, which changes what a finding is: "this file
is wrong" becomes "n of 419 files are wrong", and a rule that reported only the
first would hide the shape of the problem. So the directory rules count and give
examples, which is what `fail(..., count=, examples=)` was already built for.

## Why these exist at all

`tests/test_nav.py::test_every_generated_artifact_now_has_a_rule_set` fails when
a component declares an artifact with no rule set behind it. That test is the
reason this module was written before the artifacts were wired into the bundle
rather than after: an artifact this tool generates and does not check renders in
the UI as a silent pass, and a silent pass on a file we hand a client is the
failure mode this codebase has now found four times.

## The one rule that matters most

**MD-004.** The markdown twin of a page can be named `about/index.md` or
`about/index.html.md`, and which one works is a property of the client's host.
A directory that mixes both conventions cannot be served by either rule, so half
the links in llms.txt 404 — and they 404 *after* the client has published,
because nothing local can tell the difference. It is an error for that reason
and not because inconsistency is untidy.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from app.core.rules.registry import Category, Rule, Severity, fail, ok, skip

__all__ = [
    "INFO_BY_ID",
    "INFO_RULES",
    "MARKDOWN_BY_ID",
    "MARKDOWN_RULES",
    "OKF_BY_ID",
    "OKF_RULES",
    "ArtifactContext",
]

_SOURCE_LINE = re.compile(r"^Source:\s*(\S+)", re.M)
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_TYPE_FIELD = re.compile(r"^type:\s*(\S.*)$", re.M)
# `(?<!!)` excludes images, and the target stops at whitespace so a link title is
# not swallowed into it. Without both, OKF-003 -- an ERROR that caps the bundle at
# 49 -- reported `![Fleet](assets/fleet.png)` and `[Home](/index.md "Home page")`
# as internal links the bundle does not contain, on a bundle that is correct.
#
# Latent for our own output, which emits neither form. Live for MD-006, which runs
# the same pattern over the *client's* llms.txt, where both are ordinary markdown.
_MD_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(\s*([^)\s]+)")
_SCRIPT = re.compile(r"<\s*script\b", re.I)
# Both quote styles. `href='javascript:alert(1)'` was not seen at all by a
# double-quote-only pattern, on a rule whose rationale is "this file is published
# on a client's own domain under their name."
_HREF = re.compile(r"""href\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_TITLE = re.compile(r"<title>(.*?)</title>", re.I | re.S)
_META_DESC = re.compile(r'<meta[^>]+name="description"[^>]*>', re.I)
_CANONICAL = re.compile(r'<link[^>]+rel="canonical"[^>]*>', re.I)
#: The trailing boundary is a lookahead rather than `\b` because there is no word
#: boundary between the `9` and the `T` of `2026-09-09T00:00:00+00:00`, which is
#: the exact form `log.md` writes. With `\b` this matched a bare date and missed
#: every ISO timestamp, so OKF-005 reported a dated log as undated.
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?!\d)")
# Attribute order is not fixed in HTML, so `<meta content="noindex"
# name="robots">` is the same tag and was missed by a pattern that assumed
# `name` came first -- on the rule whose whole point is that a page which
# cannot be indexed cannot be cited.
_NOINDEX = re.compile(r"""<meta\b(?=[^>]*\bname\s*=\s*["']robots)(?=[^>]*noindex)[^>]*>""", re.I)

# `"/"` accepted `//evil.com/x`, which is protocol-relative and goes off-site.
# A root-relative path is one slash followed by something that is not a slash.
#: The suffix layout appends `.md` to the whole path, so its filenames end in the
#: page extension plus `.md`.
#:
#: A local copy of `md_pages.PAGE_EXTENSIONS` rather than an import: this package
#: is the lowest layer and importing upward makes a cycle through `full_text`.
#: The same arrangement `delivery_rules.EXPECTED_TYPE` uses for the same reason,
#: and `test_the_suffix_endings_match_the_layout_that_produces_them` is what
#: stops the two drifting -- which is the whole failure this rule had: three of
#: the six extensions were listed, so `.aspx.md` landed in the wrong bucket.
PAGE_EXTENSIONS = (".html", ".htm", ".php", ".aspx", ".asp", ".jsp")
_SUFFIX_ENDINGS = tuple(f"{ext}.md" for ext in PAGE_EXTENSIONS)

SAFE_SCHEMES = ("https:", "http:", "mailto:", "#")


def _href_is_safe(href: str) -> bool:
    value = (href or "").strip()
    if not value:
        return True
    lowered = value.lower()
    if lowered.startswith("//"):
        return False
    return lowered.startswith(SAFE_SCHEMES) or value.startswith("/")


class ArtifactContext:
    """One artifact, whether it is a file or a directory.

    `files` is populated for a directory artifact and `text` for a single file.
    Both being empty means we were handed nothing, which every rule treats as a
    skip rather than a pass -- the distinction `registry` states in its own
    docstring: a rule that did not run is not a rule that passed.
    """

    __slots__ = ("facts", "files", "index_text", "site_url", "text")

    def __init__(
        self,
        text: str = "",
        *,
        files: dict[str, str] | None = None,
        site_url: str = "",
        facts: int = 0,
        index_text: str = "",
    ) -> None:
        self.text = text
        self.files = files or {}
        self.site_url = site_url
        self.facts = facts
        #: The llms.txt this directory is supposed to be linked from. Empty means
        #: we were not given one, which MD-006 treats as a skip.
        self.index_text = index_text


# -- markdown page versions -------------------------------------------------


def _every_file_states_its_source(ctx: ArtifactContext):
    if not ctx.files:
        return skip("MD-001", "no markdown directory was generated")
    missing = [name for name, body in ctx.files.items() if not _SOURCE_LINE.search(body)]
    if missing:
        return fail(
            "MD-001",
            "markdown files do not state the URL they came from",
            count=len(missing),
            examples=missing,
        )
    return ok("MD-001", f"{len(ctx.files)} files state their source")


def _paths_are_relative_and_unique(ctx: ArtifactContext):
    if not ctx.files:
        return skip("MD-002", "no markdown directory was generated")
    bad = [n for n in ctx.files if n.startswith("/") or ".." in n or "\\" in n]
    if bad:
        return fail("MD-002", "markdown paths escape the site root", count=len(bad), examples=bad)
    return ok("MD-002", "paths are site-relative")


def _files_carry_a_body(ctx: ArtifactContext):
    if not ctx.files:
        return skip("MD-003", "no markdown directory was generated")
    thin = []
    for name, body in ctx.files.items():
        without = _SOURCE_LINE.sub("", body)
        stripped = [ln for ln in without.splitlines() if ln.strip() and not ln.startswith("#")]
        if len("\n".join(stripped).strip()) < 80:
            thin.append(name)
    if thin:
        return fail(
            "MD-003",
            "markdown files carry a heading and little else",
            count=len(thin),
            examples=thin,
        )
    return ok("MD-003", "every file carries a body")


def _naming_is_consistent(ctx: ArtifactContext):
    """One convention for the whole directory, or half of it cannot be served."""
    if not ctx.files:
        return skip("MD-004", "no markdown directory was generated")
    # Derived from the layout's own extension list rather than a copy of three of
    # them. `md_path_for` also produces `.aspx.md`, `.asp.md` and `.jsp.md`, and
    # those landed in `replace` -- so a site with `/default.aspx` and `/about.html`
    # picked SUFFIX and then reported itself as mixing conventions, on an ERROR
    # whose docstring calls it "the one rule that matters most".
    suffix = {n for n in ctx.files if n.endswith(_SUFFIX_ENDINGS)}
    replace = set(ctx.files) - suffix
    if suffix and replace:
        smaller = suffix if len(suffix) <= len(replace) else replace
        return fail(
            "MD-004",
            "the directory mixes page.md and page.html.md naming; no single host rule serves both",
            count=len(smaller),
            examples=sorted(smaller),
        )
    return ok("MD-004", "one naming convention throughout")


def _no_colliding_paths(ctx: ArtifactContext):
    if not ctx.files:
        return skip("MD-005", "no markdown directory was generated")
    lowered: dict[str, list[str]] = {}
    for name in ctx.files:
        lowered.setdefault(name.lower(), []).append(name)
    clashes = [names for names in lowered.values() if len(names) > 1]
    if clashes:
        flat = [n for names in clashes for n in names]
        return fail(
            "MD-005",
            "paths differ only by case, which collides on a case-insensitive host",
            count=len(flat),
            examples=flat,
        )
    return ok("MD-005", "no case collisions")


def _the_index_points_at_the_twins(ctx: ArtifactContext):
    """MD-006. The rule the whole directory exists to satisfy.

    Generating 419 markdown files changes nothing on its own. What llms.txt v2
    asks for is that the *index* points at them, and that step is one function
    call away from being skipped.

    It was skipped, on the first real site. `rewrite_links` compared the link's
    host against the site's verbatim, the site is filed as `redspot.com.au` and
    every page it lists is `www.redspot.com.au`, so all 419 links were left
    pointing at HTML -- and the file was byte-for-byte what it had been, which is
    exactly what "nothing needed changing" looks like.

    Counting is the check. A `.md` link on a site publishing no directory is
    caught by nothing here either, which is why the count is compared against the
    directory rather than merely required to be non-zero.
    """
    if not ctx.files:
        return skip("MD-006", "no markdown directory was generated")
    if not ctx.index_text.strip():
        return skip("MD-006", "no llms.txt was supplied to cross-check")

    published = {f"/{name}" for name in ctx.files}
    linked = 0
    html_with_a_twin: list[str] = []
    for target in _MD_LINK.findall(ctx.index_text):
        path = urlsplit(target).path or "/"
        if path in published:
            linked += 1
            continue
        for candidate in (
            path.rstrip("/") + "/index.md",
            path.rstrip("/") + "/index.html.md",
            path.rsplit(".", 1)[0] + ".md" if "." in path.rsplit("/", 1)[-1] else "",
            path + ".md",
        ):
            if candidate and candidate in published:
                html_with_a_twin.append(target)
                break

    if html_with_a_twin:
        return fail(
            "MD-006",
            "llms.txt links to the HTML page where a markdown twin was generated, "
            "so the directory is published and nothing points into it",
            count=len(html_with_a_twin),
            examples=html_with_a_twin,
        )
    if not linked:
        return fail(
            "MD-006",
            "llms.txt links at none of the generated markdown files",
        )
    return ok("MD-006", f"{linked} llms.txt links point at their markdown twin")


MARKDOWN_RULES: list[Rule] = [
    Rule(
        "MD-001",
        "Every file states its source URL",
        Category.MARKDOWN,
        Severity.ERROR,
        _every_file_states_its_source,
        "A markdown file fetched on its own cannot be cited without one.",
    ),
    Rule(
        "MD-002",
        "Paths stay under the site root",
        Category.MARKDOWN,
        Severity.ERROR,
        _paths_are_relative_and_unique,
        "A path that escapes the root is not publishable.",
    ),
    Rule(
        "MD-003",
        "Files carry a body",
        Category.MARKDOWN,
        Severity.WARNING,
        _files_carry_a_body,
        "An empty page and a page we failed to read look identical to an agent.",
    ),
    Rule(
        "MD-004",
        "One naming convention",
        Category.MARKDOWN,
        Severity.ERROR,
        _naming_is_consistent,
        "No single host rule serves both page.md and page.html.md.",
    ),
    Rule(
        "MD-005",
        "No case-only collisions",
        Category.MARKDOWN,
        Severity.WARNING,
        _no_colliding_paths,
        "Two files differing only by case overwrite on most hosts.",
    ),
    Rule(
        "MD-006",
        "llms.txt points at the twins",
        Category.MARKDOWN,
        Severity.ERROR,
        _the_index_points_at_the_twins,
        "Generating the directory changes nothing if the index still links to HTML.",
    ),
]


# -- the AI info page --------------------------------------------------------


def _has_title_and_description(ctx: ArtifactContext):
    if not ctx.text.strip():
        return skip("INF-001", "no ai-info page was generated")
    missing = []
    if not _TITLE.search(ctx.text):
        missing.append("<title>")
    if not _META_DESC.search(ctx.text):
        missing.append('<meta name="description">')
    if missing:
        return fail("INF-001", "the page is missing head elements", examples=missing)
    return ok("INF-001", "title and description present")


def _carries_no_script(ctx: ArtifactContext):
    if not ctx.text.strip():
        return skip("INF-002", "no ai-info page was generated")
    if _SCRIPT.search(ctx.text):
        return fail("INF-002", "the page contains a script element")
    return ok("INF-002", "no script")


def _hrefs_are_safe(ctx: ArtifactContext):
    if not ctx.text.strip():
        return skip("INF-003", "no ai-info page was generated")
    bad = [
        href
        for double, single in _HREF.findall(ctx.text)
        if (href := double or single) and not _href_is_safe(href)
    ]
    if bad:
        return fail(
            "INF-003", "hrefs use a scheme we will not publish", count=len(bad), examples=bad
        )
    return ok("INF-003", "every href is a safe scheme")


def _states_a_date(ctx: ArtifactContext):
    if not ctx.text.strip():
        return skip("INF-004", "no ai-info page was generated")
    if not _DATE.search(ctx.text):
        return fail("INF-004", "the page does not say when it was last updated")
    return ok("INF-004", "last-updated date present")


def _has_canonical(ctx: ArtifactContext):
    if not ctx.text.strip():
        return skip("INF-005", "no ai-info page was generated")
    if not _CANONICAL.search(ctx.text):
        return fail("INF-005", "the page declares no canonical URL")
    return ok("INF-005", "canonical present")


def _is_worth_publishing(ctx: ArtifactContext):
    """A page of three claims or fewer is not worth a footer link.

    Skipped rather than failed when the count was not supplied, because zero
    facts and "nobody told us the fact count" are different claims.
    """
    if not ctx.text.strip():
        return skip("INF-006", "no ai-info page was generated")
    if ctx.facts <= 0:
        return skip("INF-006", "fact count not supplied")
    if ctx.facts < 3:
        return fail(
            "INF-006",
            f"the page carries {ctx.facts} sourced facts; it adds little a model "
            "could not read off the homepage",
        )
    return ok("INF-006", f"{ctx.facts} sourced facts")


def _is_indexable(ctx: ArtifactContext):
    """The whole point is that a search index can return it."""
    if not ctx.text.strip():
        return skip("INF-007", "no ai-info page was generated")
    # Attribute order is not fixed in HTML. `<meta content="noindex" name="robots">`
    # is the same tag and was missed, on a rule whose whole point is that an
    # AI-info page which cannot be indexed cannot be cited.
    if _NOINDEX.search(ctx.text):
        return fail("INF-007", "the page is marked noindex, so it can never be cited")
    return ok("INF-007", "indexable")


INFO_RULES: list[Rule] = [
    Rule(
        "INF-001",
        "Title and meta description",
        Category.INFO,
        Severity.ERROR,
        _has_title_and_description,
        "Without them the page cannot be returned usefully by a search index.",
    ),
    Rule(
        "INF-002",
        "No script",
        Category.INFO,
        Severity.ERROR,
        _carries_no_script,
        "Parsing speed is the point; a script is also an injection surface.",
    ),
    Rule(
        "INF-003",
        "Safe href schemes",
        Category.INFO,
        Severity.ERROR,
        _hrefs_are_safe,
        "This file is published on a client's own domain under their name.",
    ),
    Rule(
        "INF-004",
        "States when it was updated",
        Category.INFO,
        Severity.WARNING,
        _states_a_date,
        "Freshness is the signal this page trades on.",
    ),
    Rule(
        "INF-005",
        "Declares a canonical URL",
        Category.INFO,
        Severity.WARNING,
        _has_canonical,
        "A cited page that moves takes the citation with it.",
    ),
    Rule(
        "INF-006",
        "Carries enough to be worth citing",
        Category.INFO,
        Severity.WARNING,
        _is_worth_publishing,
        "A thin page spends a footer link and returns nothing.",
    ),
    Rule(
        "INF-007",
        "Indexable",
        Category.INFO,
        Severity.ERROR,
        _is_indexable,
        "An AI info page that cannot be indexed cannot be cited.",
    ),
]


# -- the OKF bundle ----------------------------------------------------------


def _has_an_entry_point(ctx: ArtifactContext):
    if not ctx.files:
        return skip("OKF-001", "no OKF bundle was generated")
    if "index.md" not in ctx.files:
        return fail("OKF-001", "the bundle has no index.md, so an agent has no entry point")
    return ok("OKF-001", "index.md present")


def _every_concept_has_a_type(ctx: ArtifactContext):
    if not ctx.files:
        return skip("OKF-002", "no OKF bundle was generated")
    bad = []
    for name, body in ctx.files.items():
        block = _FRONTMATTER.match(body)
        if block is None or not _TYPE_FIELD.search(block.group(1)):
            bad.append(name)
    if bad:
        return fail(
            "OKF-002",
            "files have no YAML frontmatter carrying a type",
            count=len(bad),
            examples=bad,
        )
    return ok("OKF-002", f"{len(ctx.files)} concepts typed")


def _internal_links_resolve(ctx: ArtifactContext):
    """A broken link inside the bundle is a dead end in the graph.

    Only relative links are checked. An absolute URL is a claim about the live
    site, which `MD-001` and the delivery rules cover; this rule is about whether
    the directory hangs together on its own.
    """
    if not ctx.files:
        return skip("OKF-003", "no OKF bundle was generated")
    known = set(ctx.files)
    broken: list[str] = []
    for name, body in ctx.files.items():
        base = name.rsplit("/", 1)[0] if "/" in name else ""
        for target in _MD_LINK.findall(body):
            if "://" in target or target.startswith(("#", "mailto:")):
                continue
            resolved = _resolve(base, target.split("#", 1)[0])
            if resolved and resolved not in known:
                broken.append(f"{name} -> {target}")
    if broken:
        return fail(
            "OKF-003",
            "internal links point at files the bundle does not contain",
            count=len(broken),
            examples=broken,
        )
    return ok("OKF-003", "every internal link resolves")


def _resolve(base: str, target: str) -> str:
    parts = [p for p in (base.split("/") if base else []) if p]
    for piece in target.split("/"):
        if piece in ("", "."):
            continue
        if piece == "..":
            if parts:
                parts.pop()
        else:
            parts.append(piece)
    return "/".join(parts)


def _no_orphan_concepts(ctx: ArtifactContext):
    if not ctx.files or "index.md" not in ctx.files:
        return skip("OKF-004", "no bundle, or no index to walk from")
    reachable = {"index.md"}
    frontier = ["index.md"]
    while frontier:
        name = frontier.pop()
        base = name.rsplit("/", 1)[0] if "/" in name else ""
        for target in _MD_LINK.findall(ctx.files.get(name, "")):
            if "://" in target or target.startswith(("#", "mailto:")):
                continue
            resolved = _resolve(base, target.split("#", 1)[0])
            if resolved in ctx.files and resolved not in reachable:
                reachable.add(resolved)
                frontier.append(resolved)
    orphans = sorted(set(ctx.files) - reachable)
    if orphans:
        return fail(
            "OKF-004",
            "concepts are unreachable from index.md",
            count=len(orphans),
            examples=orphans,
        )
    return ok("OKF-004", "every concept is reachable")


def _has_a_dated_log(ctx: ArtifactContext):
    if not ctx.files:
        return skip("OKF-005", "no OKF bundle was generated")
    body = ctx.files.get("log.md", "")
    if not body:
        return fail("OKF-005", "the bundle has no log.md, so its staleness is invisible")
    if not _DATE.search(body):
        return fail("OKF-005", "log.md carries no date")
    return ok("OKF-005", "log.md present and dated")


OKF_RULES: list[Rule] = [
    Rule(
        "OKF-001",
        "Has an entry point",
        Category.OKF,
        Severity.ERROR,
        _has_an_entry_point,
        "An agent enters an OKF bundle at index.md or not at all.",
    ),
    Rule(
        "OKF-002",
        "Every concept declares a type",
        Category.OKF,
        Severity.ERROR,
        _every_concept_has_a_type,
        "type is the one field the spec requires.",
    ),
    Rule(
        "OKF-003",
        "Internal links resolve",
        Category.OKF,
        Severity.ERROR,
        _internal_links_resolve,
        "The links are the graph; a broken one is a dead end.",
    ),
    Rule(
        "OKF-004",
        "No orphan concepts",
        Category.OKF,
        Severity.WARNING,
        _no_orphan_concepts,
        "A concept nothing links to will never be read.",
    ),
    Rule(
        "OKF-005",
        "Dated change log",
        Category.OKF,
        Severity.WARNING,
        _has_a_dated_log,
        "An agent reasoning over a stale bundle is confidently wrong.",
    ),
]

MARKDOWN_BY_ID: dict[str, Rule] = {rule.id: rule for rule in MARKDOWN_RULES}
INFO_BY_ID: dict[str, Rule] = {rule.id: rule for rule in INFO_RULES}
OKF_BY_ID: dict[str, Rule] = {rule.id: rule for rule in OKF_RULES}
