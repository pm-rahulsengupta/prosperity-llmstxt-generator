"""IDX-001..020 — the rules that judge an llms.txt.

Written against a real bad file rather than against the spec in the abstract. The
reference fixture is *our own* output for an agency site, and every rule here that is
not straight from llmstxt.org exists because that file did something a person would
recognise as wrong.

Two design notes that apply throughout:

* **Profile-dependent rules skip loudly when there is no profile.** Validating someone
  else's file, we do not know whether 567 links is a bloated dump or a legitimate docs
  index -- and `docs.anthropic.com/llms.txt` is exactly the latter at 58KB. Guessing
  would make the validator wrong about the reference implementation of the format.
* **Aggregated counts, not per-instance findings.** 106 banned openers is one finding.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from app.core.rules.document import ANY_MD_LINK, IndexDoc
from app.core.rules.registry import Category, Rule, RuleContext, Severity, fail, ok, skip

# Openers that describe the reader's activity rather than the page's content. The
# spec asks descriptions to say what is behind a link so an agent can choose; "Learn
# more about X" says only that the link goes somewhere.
DEFAULT_BANNED_OPENERS = (
    "learn",
    "discover",
    "explore",
    "understand",
    "find out",
    "dive into",
    "unlock",
)
DEFAULT_BANNED_SUPERLATIVES = (
    "award-winning",
    "best",
    "leading",
    "top",
    "world-class",
    "dominate",
    "ultimate",
    "premier",
    "cutting-edge",
    "proven",
    "expert",
)
# Curated pairs, not an -ize/-ise regex: the general pattern counts "enterprise",
# "expertise", "advise" and "size", and reports nonsense.
LOCALE_PAIRS = (
    ("optimize", "optimise"),
    ("analyze", "analyse"),
    ("organize", "organise"),
    ("recognize", "recognise"),
    ("prioritize", "prioritise"),
    ("specialize", "specialise"),
    ("customize", "customise"),
    ("maximize", "maximise"),
    ("color", "colour"),
    ("center", "centre"),
    ("catalog", "catalogue"),
    ("license", "licence"),
    ("behavior", "behaviour"),
    ("favorite", "favourite"),
)
IDENTITY_PATTERNS = (
    "/about",
    "/about-us",
    "/contact",
    "/case-stud",
    "/our-work",
    "/portfolio",
    "/team",
    "/testimonial",
    "/clients",
)
_HTML_TAG = re.compile(r"<(?:div|span|p|img|script|style|br|table|iframe)\b", re.I)
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_FRONT_MATTER = re.compile(r"\A\s*---\s*\n.*?\n---\s*\n", re.S)
_AUTH_HINTS = (
    "/login",
    "/signin",
    "/sign-in",
    "/account",
    "/admin",
    "/wp-admin",
    "/cart",
    "/checkout",
)


def _doc(ctx: RuleContext) -> IndexDoc | None:
    return ctx.index  # type: ignore[return-value]


def _openers(ctx: RuleContext) -> tuple[str, ...]:
    return tuple(ctx.profile.banned_openers) or DEFAULT_BANNED_OPENERS


def _superlatives(ctx: RuleContext) -> tuple[str, ...]:
    return tuple(ctx.profile.banned_superlatives) or DEFAULT_BANNED_SUPERLATIVES


# -- structure --------------------------------------------------------------


def idx_001(ctx):
    doc = _doc(ctx)
    h1s = [h for h in doc.headings if h.level == 1]
    if not h1s:
        return fail("IDX-001", "No H1. It is the only element the spec requires.")
    if len(h1s) > 1:
        return fail(
            "IDX-001",
            f"{len(h1s)} H1s; the document must have exactly one.",
            count=len(h1s),
            examples=[h.text for h in h1s],
        )
    first_content = next((n for n, line in enumerate(doc.lines) if line.strip()), 0)
    if h1s[0].line != first_content:
        return fail("IDX-001", "The H1 is not the first non-blank line.")
    return ok("IDX-001")


def idx_002(ctx):
    doc = _doc(ctx)
    if not doc.blockquote:
        return fail("IDX-002", "No blockquote summary after the H1.")
    if doc.h1_line >= 0 and doc.blockquote_line > doc.h1_line + 3:
        return fail(
            "IDX-002",
            f"The blockquote is {doc.blockquote_line - doc.h1_line} lines below the H1; "
            "it should follow it directly.",
        )
    return ok("IDX-002")


def idx_003(ctx):
    doc = _doc(ctx)
    if doc.intro_headings:
        return fail(
            "IDX-003",
            "The free-form block between the blockquote and the first section contains "
            "headings; the spec allows any markdown there except headings.",
            count=len(doc.intro_headings),
            examples=[h.text for h in doc.intro_headings],
        )
    return ok("IDX-003")


def idx_004(ctx):
    doc = _doc(ctx)
    names = [s.name.strip().lower() for s in doc.sections]
    if "optional" in names and names[-1] != "optional":
        return fail(
            "IDX-004",
            f"`## Optional` is not last; {doc.sections[names.index('optional') + 1].name} follows it.",
        )
    return ok("IDX-004")


# A generated footer is conventional and harmless: a thematic break, and a single
# italicised line saying when the file was made. Our own renderer emits exactly that,
# and a rule that failed it would fail every file we ship. What this rule is actually
# for is prose, extra sections and stray link lists appended after the document ends.
_FOOTER_LINE = re.compile(r"^\s*(?:---+|\*[^*].*\*|_[^_].*_|<!--.*-->)\s*$")


def idx_005(ctx):
    doc = _doc(ctx)
    if not doc.trailing:
        return ok("IDX-005")

    lines = [line for line in doc.trailing.splitlines() if line.strip()]
    if all(_FOOTER_LINE.match(line) for line in lines) and len(lines) <= 3:
        return ok("IDX-005", "trailing content is a conventional generated footer")

    substantive = [line for line in lines if not _FOOTER_LINE.match(line)]
    return fail(
        "IDX-005",
        f"{len(substantive)} line(s) of content appear after the final link list.",
        count=len(substantive),
        examples=substantive,
    )


def idx_006(ctx):
    doc = _doc(ctx)
    malformed = [link for link in doc.links if link.malformed]
    if malformed:
        return fail(
            "IDX-006",
            f"{len(malformed)} list item(s) are not well-formed link lines.",
            count=len(malformed),
            examples=[link.raw.strip() for link in malformed],
        )
    return ok("IDX-006")


def idx_007(ctx):
    doc = _doc(ctx)
    # Malformed items have no URL to judge; IDX-006 already owns them.
    links = [link for link in doc.links if not link.malformed]
    relative = [link.url for link in links if not link.url.startswith(("http://", "https://"))]
    if relative:
        return fail(
            "IDX-007",
            f"{len(relative)} link(s) are not absolute URLs.",
            count=len(relative),
            examples=relative,
        )
    insecure = [link.url for link in links if link.url.startswith("http://")]
    if insecure:
        return fail(
            "IDX-007",
            f"{len(insecure)} link(s) use http rather than https.",
            count=len(insecure),
            examples=insecure,
        )
    # Trailing-slash consistency is judged only on paths that could go either way --
    # a URL ending in a file extension has no choice, and counting it as inconsistent
    # would flag every well-formed docs site.
    candidates = [urlparse(link.url).path for link in links]
    directoryish = [p for p in candidates if p and "." not in p.rsplit("/", 1)[-1] and p != "/"]
    with_slash = sum(1 for p in directoryish if p.endswith("/"))
    without = len(directoryish) - with_slash
    if directoryish and min(with_slash, without) > 0:
        minority = min(with_slash, without)
        if minority / len(directoryish) > 0.05:
            return fail(
                "IDX-007",
                f"Trailing slashes are inconsistent: {with_slash} with, {without} without.",
                count=minority,
            )
    return ok("IDX-007")


def idx_008(ctx):
    doc = _doc(ctx)
    urls, titles = {}, {}
    for link in doc.links:
        if link.malformed:
            continue
        urls.setdefault(link.url, []).append(link)
        titles.setdefault(link.title.strip().lower(), []).append(link)

    dup_urls = {u: v for u, v in urls.items() if len(v) > 1}
    dup_titles = {t: v for t, v in titles.items() if t and len(v) > 1}
    if dup_urls or dup_titles:
        parts = []
        if dup_urls:
            parts.append(f"{len(dup_urls)} duplicate URL(s)")
        if dup_titles:
            parts.append(f"{len(dup_titles)} duplicate title(s)")
        return fail(
            "IDX-008",
            " and ".join(parts) + ".",
            count=len(dup_urls) + len(dup_titles),
            examples=[*dup_urls, *(f"{t!r}" for t in dup_titles)],
        )
    return ok("IDX-008")


def idx_009(ctx):
    doc = _doc(ctx)
    if not ctx.profile.known or not ctx.profile.max_bytes:
        return skip("IDX-009", "no profile, so no byte budget to judge against")
    if doc.byte_size > ctx.profile.max_bytes:
        return fail(
            "IDX-009",
            f"{doc.byte_size:,} bytes against a {ctx.profile.max_bytes:,}-byte budget "
            f"({doc.byte_size / ctx.profile.max_bytes:.1f}x).",
            count=doc.byte_size,
        )
    return ok("IDX-009")


def idx_010(ctx):
    doc = _doc(ctx)
    if not ctx.profile.known or not ctx.profile.max_links:
        return skip("IDX-010", "no profile, so no link budget to judge against")
    count = len(doc.links)
    if count > ctx.profile.max_links:
        return fail(
            "IDX-010",
            f"{count} links against a maximum of {ctx.profile.max_links}. An index is a "
            "curated selection, not a sitemap.",
            count=count,
        )
    if ctx.profile.min_links and count < ctx.profile.min_links:
        return fail(
            "IDX-010",
            f"Only {count} links; the profile expects at least {ctx.profile.min_links}.",
            count=count,
        )
    return ok("IDX-010")


def idx_011(ctx):
    doc = _doc(ctx)
    if not ctx.profile.known or not (ctx.profile.section_min or ctx.profile.section_max):
        return skip("IDX-011", "no profile section bounds")
    problems = []
    counts = {s.name: len(s.links) for s in doc.sections}
    for name, minimum in ctx.profile.section_min.items():
        if counts.get(name, 0) < minimum:
            problems.append(f"{name}: {counts.get(name, 0)} < {minimum}")
    for name, maximum in ctx.profile.section_max.items():
        if counts.get(name, 0) > maximum:
            problems.append(f"{name}: {counts[name]} > {maximum}")
    if problems:
        return fail(
            "IDX-011",
            f"{len(problems)} section(s) outside their bounds.",
            count=len(problems),
            examples=problems,
        )
    return ok("IDX-011")


# -- description quality ----------------------------------------------------


def idx_012(ctx):
    doc = _doc(ctx)
    missing = [link for link in doc.links if not link.malformed and not link.has_description]
    if missing:
        return fail(
            "IDX-012",
            f"{len(missing)} link(s) have no description. A bare title tells an agent "
            "nothing it can act on.",
            count=len(missing),
            examples=[link.url for link in missing],
        )
    return ok("IDX-012")


def idx_013(ctx):
    doc = _doc(ctx)
    banned = _superlatives(ctx)
    pattern = re.compile(r"\b(" + "|".join(re.escape(w) for w in banned) + r")\b", re.I)
    hits = [
        f"{m.group(0)} — {link.description[:70]}"
        for link in doc.links
        for m in pattern.finditer(link.description)
    ]
    if hits:
        return fail(
            "IDX-013",
            f"{len(hits)} superlative(s) in descriptions. They are unverifiable and "
            "read as marketing to a model deciding what to fetch.",
            count=len(hits),
            examples=hits,
        )
    return ok("IDX-013")


def idx_014(ctx):
    doc = _doc(ctx)
    banned = _openers(ctx)
    hits = [
        f"{link.description[:70]}"
        for link in doc.links
        if link.description.strip().lower().startswith(banned)
    ]
    if hits:
        return fail(
            "IDX-014",
            f"{len(hits)} description(s) open with a call to action rather than saying "
            "what the page contains.",
            count=len(hits),
            examples=hits,
        )
    return ok("IDX-014")


def idx_015(ctx):
    doc = _doc(ctx)
    body = doc.text.lower()
    mixed = []
    for american, british in LOCALE_PAIRS:
        a = len(re.findall(rf"\b{american}", body))
        b = len(re.findall(rf"\b{british}", body))
        if a and b:
            mixed.append(f"{american} x{a} / {british} x{b}")
    if mixed:
        return fail(
            "IDX-015",
            f"{len(mixed)} word(s) spelled both ways in the same file.",
            count=len(mixed),
            examples=mixed,
        )
    return ok("IDX-015")


# -- content hygiene --------------------------------------------------------


def idx_016(ctx):
    doc = _doc(ctx)
    suspect = []
    for link in doc.links:
        if not link.url:
            continue
        parsed = urlparse(link.url)
        if parsed.query:
            suspect.append(f"parameterised: {link.url}")
        elif any(hint in parsed.path.lower() for hint in _AUTH_HINTS):
            suspect.append(f"auth-gated: {link.url}")
    if suspect:
        return fail(
            "IDX-016",
            f"{len(suspect)} link(s) look parameterised or auth-gated; an agent cannot "
            "usefully fetch them.",
            count=len(suspect),
            examples=suspect,
        )
    return ok("IDX-016")


def idx_017(ctx):
    doc = _doc(ctx)
    patterns = tuple(ctx.profile.identity_patterns) or IDENTITY_PATTERNS
    optional = doc.optional_section
    if optional is None:
        return ok("IDX-017", "no Optional section")

    stranded = [link.url for link in optional.links if any(p in link.url.lower() for p in patterns)]
    if stranded:
        return fail(
            "IDX-017",
            f"{len(stranded)} identity page(s) — about, contact, case studies — are in "
            "`## Optional`, which marks them as safe to ignore.",
            count=len(stranded),
            examples=stranded,
        )
    return ok("IDX-017")


def idx_018(ctx):
    doc = _doc(ctx)
    problems = []
    if _FRONT_MATTER.match(doc.text):
        problems.append("YAML front matter")
    if images := _IMAGE.findall(doc.text):
        problems.append(f"{len(images)} image(s)")
    if tags := _HTML_TAG.findall(doc.text):
        problems.append(f"{len(tags)} raw HTML tag(s)")
    if problems:
        return fail(
            "IDX-018",
            "Index contains " + ", ".join(problems) + ".",
            count=len(problems),
            examples=problems,
        )
    return ok("IDX-018")


# -- network ----------------------------------------------------------------


def idx_019(ctx):
    doc = _doc(ctx)
    referenced = [
        url for url in ANY_MD_LINK.findall(doc.text) if "llms-full.txt" in url or "llms.txt" in url
    ]
    referenced += re.findall(r"https?://\S*llms-full\.txt", doc.text)
    referenced = sorted(set(referenced))
    if not referenced:
        return ok("IDX-019", "no companion file referenced")
    if not ctx.network_checked:
        return skip("IDX-019", "network checks did not run")
    broken = [
        f"{url} ({ctx.link_status.get(url, 'unchecked')})"
        for url in referenced
        if not isinstance(ctx.link_status.get(url), int) or ctx.link_status.get(url, 0) >= 400
    ]
    if broken:
        return fail(
            "IDX-019",
            f"{len(broken)} referenced companion file(s) do not resolve. The index "
            "points at something that is not there.",
            count=len(broken),
            examples=broken,
        )
    return ok("IDX-019")


def idx_020(ctx):
    doc = _doc(ctx)
    if not ctx.network_checked:
        return skip("IDX-020", "network checks did not run")
    broken = [
        f"{link.url} ({ctx.link_status.get(link.url, 'unchecked')})"
        for link in doc.links
        if link.url
        and (
            not isinstance(ctx.link_status.get(link.url), int)
            or ctx.link_status.get(link.url, 0) >= 400
        )
    ]
    if broken:
        return fail(
            "IDX-020",
            f"{len(broken)} link(s) do not resolve.",
            count=len(broken),
            examples=broken,
        )
    return ok("IDX-020")


INDEX_RULES: list[Rule] = [
    Rule(
        "IDX-001",
        "Exactly one H1, first non-blank line",
        Category.INDEX,
        Severity.ERROR,
        idx_001,
        "The only element the spec requires.",
    ),
    Rule(
        "IDX-002",
        "Blockquote follows the H1",
        Category.INDEX,
        Severity.ERROR,
        idx_002,
        "Carries the context needed to read everything below it.",
    ),
    Rule(
        "IDX-003",
        "No headings in the intro block",
        Category.INDEX,
        Severity.WARNING,
        idx_003,
        "The spec allows any markdown there except headings.",
    ),
    Rule(
        "IDX-004",
        "`## Optional` is last",
        Category.INDEX,
        Severity.WARNING,
        idx_004,
        "Anything after it reads as more important than it is.",
    ),
    Rule(
        "IDX-005",
        "No content after the final link list",
        Category.INDEX,
        Severity.WARNING,
        idx_005,
        "Generated footers land here and are not part of the format.",
    ),
    Rule(
        "IDX-006",
        "Link lines are well formed",
        Category.INDEX,
        Severity.ERROR,
        idx_006,
        "A parser written against the spec will not read a malformed item.",
    ),
    Rule(
        "IDX-007",
        "URLs absolute, https, consistent trailing slash",
        Category.INDEX,
        Severity.ERROR,
        idx_007,
        "The file is fetched out of context; a relative URL has no base.",
    ),
    Rule(
        "IDX-008",
        "No duplicate URLs or titles",
        Category.INDEX,
        Severity.WARNING,
        idx_008,
        "Two identical titles give an agent no way to choose.",
    ),
    Rule(
        "IDX-009",
        "Within the byte budget",
        Category.INDEX,
        Severity.WARNING,
        idx_009,
        "The file must fit in context; the detail lives behind the links.",
    ),
    Rule(
        "IDX-010",
        "Link count within profile bounds",
        Category.INDEX,
        Severity.WARNING,
        idx_010,
        "An index is a curated selection, not a sitemap dump.",
    ),
    Rule(
        "IDX-011",
        "Section counts within bounds",
        Category.INDEX,
        Severity.INFO,
        idx_011,
        "Stops forty blog posts and two service pages.",
    ),
    Rule(
        "IDX-012",
        "Every link has a description",
        Category.INDEX,
        Severity.WARNING,
        idx_012,
        "The highest-leverage content in the file.",
    ),
    Rule(
        "IDX-013",
        "No banned superlatives",
        Category.INDEX,
        Severity.WARNING,
        idx_013,
        "Unverifiable marketing language in a machine-readable file.",
    ),
    Rule(
        "IDX-014",
        "No call-to-action openers",
        Category.INDEX,
        Severity.WARNING,
        idx_014,
        "Describes the reader's activity rather than the page's content.",
    ),
    Rule(
        "IDX-015",
        "Consistent locale spelling",
        Category.INDEX,
        Severity.INFO,
        idx_015,
        "Mixed spelling in one file reads as unedited.",
    ),
    Rule(
        "IDX-016",
        "No auth-gated or parameterised URLs",
        Category.INDEX,
        Severity.WARNING,
        idx_016,
        "An agent cannot usefully fetch them.",
    ),
    Rule(
        "IDX-017",
        "Identity pages outside Optional",
        Category.INDEX,
        Severity.ERROR,
        idx_017,
        "Optional marks a link as safe to ignore; about and contact are not.",
    ),
    Rule(
        "IDX-018",
        "No front matter, images or raw HTML",
        Category.INDEX,
        Severity.WARNING,
        idx_018,
        "None of it is part of the format.",
    ),
    Rule(
        "IDX-019",
        "Referenced companion files resolve",
        Category.INDEX,
        Severity.WARNING,
        idx_019,
        "A dead pointer is worse than no pointer.",
    ),
    Rule(
        "IDX-020",
        "All linked URLs resolve",
        Category.INDEX,
        Severity.WARNING,
        idx_020,
        "The file's entire purpose is the links.",
    ),
]
