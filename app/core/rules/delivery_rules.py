"""HDR-001..006 and CAT-001..007 — the `_headers` and `ai-catalog.json` rules.

Two artifacts, one module, because both express the same idea in different
syntax: *here is where the machine-readable surfaces are*. Both fail the same
way — by naming something that is not there — and both are read by software that
will act on the claim rather than by a person who can shrug.

`render_headers` states the thesis for both: *"A `Link` header pointing at a 404
is worse than no header: it costs an agent a request and teaches it to distrust
the rest."*

## The rule each set exists for

**HDR-001** and **CAT-001** are the same rule against different files: nothing may
be advertised that the bundle did not produce. Today that is enforced only by two
booleans at the `render_headers` call site and by `build_catalog` refusing to
emit — which is the identical single-point-of-failure `app/core/evidence.py`
describes for AGT-004. The generator refusing to lie is a good defence and it is
one defence, and it does not survive a human editing the file afterwards.

Both need the bundle passed in. Where it is absent they skip, because "we cannot
see what was generated" and "nothing was generated" are different claims.
"""

from __future__ import annotations

import json
import re

from app.core.rules.registry import Category, Rule, Severity, fail, ok, skip

LINK_LINE = re.compile(r"^\s+Link:\s*(.+?)\s*$", re.M)
LINK_TARGET = re.compile(r"<([^>]+)>")
LINK_REL = re.compile(r'rel\s*=\s*"([^"]+)"')
LINK_TYPE = re.compile(r'type\s*=\s*"([^"]+)"')
PATH_LINE = re.compile(r"^(/\S*)\s*$", re.M)

# What each rel should point at, and what it should be typed as. Drawn from the
# generator so a change there shows up here as a disagreement rather than as a
# silently different opinion.
EXPECTED_TYPE = {
    "describedby": "text/markdown",
    "service-desc": "application/vnd.oai.openapi+json",
}

CATALOG_TYPES = {
    "text/markdown",
    "application/json",
    "application/xml",
    "application/ai-catalog+json",
    "application/a2a-agent-card+json",
    "application/vnd.oai.openapi+json",
}

URN = re.compile(r"^urn:air:[a-z0-9.-]+:[a-z0-9-]+:[a-z0-9-]+$")


class DeliveryContext:
    """One generated file, plus what the bundle actually produced.

    `artifacts` is the set of filenames the bundle emitted. `None` means we were
    not told, which is different from an empty set meaning nothing was generated
    — the rules that cross-check advertised surfaces skip on the first and fail
    on the second.
    """

    __slots__ = ("artifacts", "site_url", "text")

    def __init__(
        self, text: str = "", *, artifacts: set[str] | None = None, site_url: str = ""
    ) -> None:
        self.text = text
        self.artifacts = artifacts
        self.site_url = site_url


def _links(text: str) -> list[str]:
    return LINK_LINE.findall(text)


# -- _headers ----------------------------------------------------------------


def _advertises_only_what_exists(ctx: DeliveryContext):
    """HDR-001. The rule the whole set exists for."""
    if ctx.artifacts is None:
        return skip("HDR-001", "no bundle supplied, so nothing can be cross-checked")

    known = {
        "/llms.txt": "llms.txt",
        "/llms-full.txt": "llms-full.txt",
        "/.well-known/ai-catalog.json": "ai-catalog.json",
        "/agents.md": "agents.md",
    }
    missing: list[str] = []
    for link in _links(ctx.text):
        target = LINK_TARGET.search(link)
        if target is None:
            continue
        path = target.group(1)
        artifact = known.get(path)
        if artifact is not None and artifact not in ctx.artifacts:
            missing.append(f"{path} is advertised and was not generated")

    if not missing:
        return ok("HDR-001", "every advertised surface was generated")
    return fail(
        "HDR-001",
        "A Link header points at a file that does not exist. It costs an agent a "
        "request and teaches it to distrust the rest of the file.",
        count=len(missing),
        examples=missing,
    )


def _links_are_well_formed(ctx: DeliveryContext):
    """HDR-002. RFC 8288 shape: an angle-bracketed target and a quoted rel."""
    links = _links(ctx.text)
    if not links:
        return skip("HDR-002", "no Link headers in this file")

    malformed = [
        link for link in links if LINK_TARGET.search(link) is None or LINK_REL.search(link) is None
    ]
    if malformed:
        return fail(
            "HDR-002",
            'Link must be `<uri>; rel="name"`. A target without angle brackets or '
            "an unquoted rel is discarded rather than repaired.",
            count=len(malformed),
            examples=malformed,
        )
    return ok("HDR-002", f"{len(links)} Link header(s), all well-formed")


def _types_match_the_artifact(ctx: DeliveryContext):
    """HDR-003. A markdown file advertised as JSON is a wasted fetch."""
    wrong: list[str] = []
    for link in _links(ctx.text):
        rel = LINK_REL.search(link)
        declared = LINK_TYPE.search(link)
        if rel is None:
            continue
        expected = EXPECTED_TYPE.get(rel.group(1))
        if expected and declared and declared.group(1) != expected:
            wrong.append(f'rel="{rel.group(1)}" declares {declared.group(1)}, expected {expected}')

    if wrong:
        return fail(
            "HDR-003",
            "A declared content type does not match what the file actually is.",
            count=len(wrong),
            examples=wrong,
        )
    return ok("HDR-003")


def _no_duplicate_rels(ctx: DeliveryContext):
    """HDR-004. Two of the same rel means one is being ignored."""
    rels = [m.group(1) for link in _links(ctx.text) if (m := LINK_REL.search(link))]
    duplicated = sorted({rel for rel in rels if rels.count(rel) > 1})
    if not duplicated:
        return ok("HDR-004")
    return fail(
        "HDR-004",
        "The same rel is advertised more than once. Which one an agent follows is not defined.",
        count=len(duplicated),
        examples=duplicated,
    )


def _paths_are_root_relative(ctx: DeliveryContext):
    """HDR-005. A cross-origin Link is a different site's claim.

    `openapi_url` is interpolated raw by `render_headers` and is the one place an
    absolute URL can enter. Advisory rather than an error: pointing at a genuinely
    external OpenAPI document is legitimate.
    """
    external: list[str] = []
    host = ctx.site_url.split("//")[-1].strip("/").lower()
    for link in _links(ctx.text):
        target = LINK_TARGET.search(link)
        if target is None:
            continue
        value = target.group(1)
        if value.startswith(("http://", "https://")) and (not host or host not in value.lower()):
            external.append(value)

    if not external:
        return ok("HDR-005")
    return fail(
        "HDR-005",
        "A Link points at another origin. Legitimate for a hosted API document, "
        "and worth confirming it was meant.",
        count=len(external),
        examples=external,
    )


def _syntax_is_cloudflare(ctx: DeliveryContext):
    """HDR-006. Path at column 0, headers indented. Netlify and Pages both."""
    if not ctx.text.strip():
        return skip("HDR-006", "empty file")
    if not PATH_LINE.search(ctx.text):
        return fail(
            "HDR-006",
            "No path pattern at column 0. Without one every indented header line "
            "belongs to nothing and the file is inert.",
        )
    stray = [
        line
        for line in ctx.text.splitlines()
        if line.strip() and not line.startswith((" ", "\t", "#")) and not PATH_LINE.match(line)
    ]
    if stray:
        return fail(
            "HDR-006",
            "A line at column 0 that is neither a path pattern nor a comment.",
            count=len(stray),
            examples=stray,
        )
    return ok("HDR-006", "path patterns and indented headers are correctly shaped")


# -- ai-catalog.json ---------------------------------------------------------


def _catalog_json(ctx: DeliveryContext) -> dict | None:
    try:
        parsed = json.loads(ctx.text)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _entries_trace_to_a_probe(ctx: DeliveryContext):
    """CAT-001. The ai-catalog analogue of AGT-004.

    `ai_catalog.py` says it outright: *"this file is parsed by machines that will
    connect to whatever it lists, so a wrong entry is not a misleading sentence
    but a failed connection attempt."*
    """
    if ctx.artifacts is None:
        return skip("CAT-001", "no bundle supplied, so nothing can be cross-checked")
    document = _catalog_json(ctx)
    if document is None:
        return skip("CAT-001", "the file is not parseable JSON")

    entries = document.get("entries") or []
    urls = [e.get("url", "") for e in entries if isinstance(e, dict)]
    host = ctx.site_url.split("//")[-1].strip("/").lower()
    foreign = [u for u in urls if u and host and host not in u.lower()]

    if not foreign:
        return ok("CAT-001", f"{len(urls)} entr(y/ies), all on this site")
    return fail(
        "CAT-001",
        "An entry points somewhere this site does not control. Software will connect to it.",
        count=len(foreign),
        examples=foreign,
    )


def _is_valid_json(ctx: DeliveryContext):
    """CAT-002. It is a machine format; malformed means silently unread."""
    if _catalog_json(ctx) is None:
        return fail("CAT-002", "Not parseable as a JSON object. Every reader will skip it.")
    return ok("CAT-002")


def _spec_version_present(ctx: DeliveryContext):
    document = _catalog_json(ctx)
    if document is None:
        return skip("CAT-003", "the file is not parseable JSON")
    if not document.get("specVersion"):
        return fail("CAT-003", "No specVersion. A reader cannot tell which shape to expect.")
    return ok("CAT-003", f"specVersion {document['specVersion']}")


def _identifiers_are_unique(ctx: DeliveryContext):
    """CAT-004. Checked nowhere else. A duplicate id makes both ambiguous."""
    document = _catalog_json(ctx)
    if document is None:
        return skip("CAT-004", "the file is not parseable JSON")

    ids = [e.get("identifier", "") for e in document.get("entries") or [] if isinstance(e, dict)]
    duplicated = sorted({i for i in ids if i and ids.count(i) > 1})
    if duplicated:
        return fail(
            "CAT-004",
            "Two entries share an identifier, so any reference to it is ambiguous.",
            count=len(duplicated),
            examples=duplicated,
        )
    return ok("CAT-004", f"{len(ids)} identifier(s), all distinct")


def _identifiers_are_urns(ctx: DeliveryContext):
    document = _catalog_json(ctx)
    if document is None:
        return skip("CAT-005", "the file is not parseable JSON")

    ids = [e.get("identifier", "") for e in document.get("entries") or [] if isinstance(e, dict)]
    malformed = [i for i in ids if not URN.match(i or "")]
    if malformed:
        return fail(
            "CAT-005",
            "Identifier is not a well-formed `urn:air:domain:kind:slug`.",
            count=len(malformed),
            examples=malformed,
        )
    return ok("CAT-005")


def _worth_publishing(ctx: DeliveryContext):
    """CAT-006. One entry is noise wearing a standard's clothes.

    `worth_publishing` refuses to emit below two, so a published one-entry
    catalog means somebody edited it by hand.
    """
    document = _catalog_json(ctx)
    if document is None:
        return skip("CAT-006", "the file is not parseable JSON")

    entries = document.get("entries") or []
    if len(entries) >= 2:
        return ok("CAT-006", f"{len(entries)} entries")
    return fail(
        "CAT-006",
        f"{len(entries)} entr(y/ies). A catalog listing fewer than two adds a fetch "
        "and no information the site did not already advertise.",
        count=len(entries),
    )


def _types_are_recognised(ctx: DeliveryContext):
    document = _catalog_json(ctx)
    if document is None:
        return skip("CAT-007", "the file is not parseable JSON")

    unknown = [
        e.get("type", "")
        for e in document.get("entries") or []
        if isinstance(e, dict) and e.get("type") and e["type"] not in CATALOG_TYPES
    ]
    if unknown:
        return fail(
            "CAT-007",
            "Entry declares a media type outside the catalog's own set.",
            count=len(unknown),
            examples=unknown,
        )
    return ok("CAT-007")


HEADER_RULES: list[Rule] = [
    Rule(
        "HDR-001",
        "Advertises only what exists",
        Category.INDEX,
        Severity.ERROR,
        _advertises_only_what_exists,
        "A Link pointing at a 404 is worse than no Link.",
    ),
    Rule(
        "HDR-002",
        "Link headers are well-formed",
        Category.INDEX,
        Severity.ERROR,
        _links_are_well_formed,
        "A malformed Link is discarded, not repaired.",
    ),
    Rule(
        "HDR-003",
        "Declared types match",
        Category.INDEX,
        Severity.WARNING,
        _types_match_the_artifact,
        "A wrong type costs a fetch and returns nothing usable.",
    ),
    Rule(
        "HDR-004",
        "No duplicate rels",
        Category.INDEX,
        Severity.WARNING,
        _no_duplicate_rels,
        "Which of two an agent follows is undefined.",
    ),
    Rule(
        "HDR-005",
        "Targets are on this site",
        Category.INDEX,
        Severity.INFO,
        _paths_are_root_relative,
        "A cross-origin Link is another site's claim.",
    ),
    Rule(
        "HDR-006",
        "Cloudflare _headers syntax",
        Category.INDEX,
        Severity.ERROR,
        _syntax_is_cloudflare,
        "Wrong indentation makes the whole file inert.",
    ),
]

CATALOG_RULES: list[Rule] = [
    Rule(
        "CAT-001",
        "Entries trace to this site",
        Category.INDEX,
        Severity.ERROR,
        _entries_trace_to_a_probe,
        "Software connects to whatever this lists; a wrong entry is a failed connection.",
    ),
    Rule(
        "CAT-002",
        "Parseable JSON",
        Category.INDEX,
        Severity.ERROR,
        _is_valid_json,
        "Malformed means silently unread.",
    ),
    Rule(
        "CAT-003",
        "specVersion present",
        Category.INDEX,
        Severity.WARNING,
        _spec_version_present,
        "A reader cannot tell which shape to expect.",
    ),
    Rule(
        "CAT-004",
        "Identifiers are unique",
        Category.INDEX,
        Severity.ERROR,
        _identifiers_are_unique,
        "A duplicate makes every reference to it ambiguous.",
    ),
    Rule(
        "CAT-005",
        "Identifiers are URNs",
        Category.INDEX,
        Severity.WARNING,
        _identifiers_are_urns,
        "The convention is what makes them resolvable.",
    ),
    Rule(
        "CAT-006",
        "Worth publishing",
        Category.INDEX,
        Severity.WARNING,
        _worth_publishing,
        "Fewer than two entries adds a fetch and no information.",
    ),
    Rule(
        "CAT-007",
        "Types are recognised",
        Category.INDEX,
        Severity.WARNING,
        _types_are_recognised,
        "An unknown media type tells a reader nothing.",
    ),
]

HEADER_BY_ID: dict[str, Rule] = {rule.id: rule for rule in HEADER_RULES}
CATALOG_BY_ID: dict[str, Rule] = {rule.id: rule for rule in CATALOG_RULES}
