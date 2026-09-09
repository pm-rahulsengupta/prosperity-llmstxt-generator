"""Text and URL helpers, ported from the source tool's private helpers.

Behaviour is preserved deliberately — these are well-tuned. The one change is
that the en-dash and em-dash separators are real characters again. In the source
they had been double-encoded during the Flask migration, so the clause-boundary
branch of `_truncate_text` and the separator branch of `_truncate_title` could
never match real text and silently degraded every title on the site.
"""

from __future__ import annotations

from urllib.parse import urlparse

# Sentence terminators, tried before clause boundaries.
_SENTENCE_SEPARATORS = (". ", "! ", "? ")
# Clause boundaries. The dashes here are U+2013 and U+2014 — see module docstring.
_CLAUSE_SEPARATORS = (", ", "; ", " \u2013 ", " \u2014 ", " - ")
# Title separators, in the "Page Name | Brand" SEO convention.
_TITLE_SEPARATORS = (" | ", " - ", " \u2013 ", " \u2014 ", " : ")

# Keep at least this share of the text, or the cut is not worth making.
_MIN_KEEP_RATIO = 0.4


def same_site(url: str, site: str) -> bool:
    """Whether `url` is on `site`, or on a subdomain of it.

    Three rules judged this with `host in url`, which is wrong in both
    directions and wrong in the direction that matters:

    * `"redspot.com.au" in "redspot.com.au.attacker.net"` is **True**, so a
      suffix-extended lookalike passed as the site's own. AGT-009 exists because
      "a file quietly directing agents elsewhere is how a hijack looks", and it
      passed the hijack. CAT-001 searched the whole URL rather than the host, so
      `https://evil.example/?ref=redspot.com.au` passed it too, on a file whose
      own failure message is "Software will connect to it."
    * `myredspot.com.au` contains `redspot.com.au` and is a different company.
    * In the other direction, a site filed as `www.example.com` matched nothing
      on `example.com`, so every same-site URL read as cross-origin. That is the
      `rewrite_links` bug from this morning, unfixed in three more places.

    Compared on hosts through `domain_of`, so one spelling of a domain decides
    it -- the rule `58b4107` applied to the tables and `domain_of` states. A
    subdomain counts: `shop.example.com` is the site's own infrastructure, and a
    leading dot is what separates it from `notexample.com`.
    """
    host = _host(url)
    base = _host(site)
    if not host or not base:
        return False
    return host == base or host.endswith("." + base)


def _host(value: str) -> str:
    """The comparable host of a URL **or** of a bare hostname.

    `domain_of` runs `urlparse`, which puts a scheme-less string in `path` and
    leaves `netloc` empty -- so a bare host like `ucp.dev`, which is exactly what
    AGT-009's trusted set is built from, compared as "". Callers hold both
    shapes and neither is wrong, so the normalisation belongs here rather than at
    four call sites that would each have to remember.
    """
    text = (value or "").strip()
    if not text:
        return ""
    return domain_of(text if "//" in text else f"//{text}")


def unquoted(summary: str) -> str:
    """A site summary without a blockquote marker it arrived carrying.

    Lives here rather than in `render` because three artifacts now consume the
    stored summary and only one of them was stripping the marker. The llms.txt
    renderer supplies its own `>`, so it removed any leading one and shipped a
    clean file; `okf` wrote the raw value into YAML frontmatter and `ai_info`
    into a `<meta name="description">`, so both published a literal `>` on a
    client's domain -- measured on redspot, where the model returned its blurb
    pre-quoted and the summary has been stored that way ever since.

    Every leading marker is removed, not just one: `>>` and `> > ` are both a
    person or a model saying "this is the quote", twice.
    """
    text = (summary or "").strip()
    while text.startswith(">"):
        text = text[1:].lstrip()
    return text


def safe_int(val: object) -> int:
    try:
        return int(float(str(val).strip()))
    except (TypeError, ValueError):
        return 0


def safe_float(val: object) -> float:
    try:
        return float(str(val).strip().rstrip("%"))
    except (TypeError, ValueError):
        return 0.0


def truncate_text(text: str, max_chars: int = 120, ellipsis: bool = True) -> str:
    """Truncate at a natural boundary: sentence end, then clause, then word."""
    if not text or len(text) <= max_chars:
        return text

    floor = max_chars * _MIN_KEEP_RATIO

    for sep in _SENTENCE_SEPARATORS:
        idx = text.rfind(sep, 0, max_chars)
        if idx > floor:
            return text[: idx + 1].strip()

    for sep in _CLAUSE_SEPARATORS:
        idx = text.rfind(sep, 0, max_chars)
        if idx > floor:
            return text[:idx].strip()

    idx = text.rfind(" ", 0, max_chars)
    if idx > 0:
        return text[:idx].strip() + ("..." if ellipsis else "")

    return text[:max_chars].strip()


def truncate_title(title: str, max_chars: int = 60) -> str:
    """Drop the "| Brand Name" suffix convention, then truncate what is left."""
    if not title:
        return ""
    for sep in _TITLE_SEPARATORS:
        idx = title.find(sep)
        if 0 < idx <= max_chars:
            return title[:idx].strip()
    if len(title) <= max_chars:
        return title.strip()
    return truncate_text(title, max_chars, ellipsis=False)


def title_from_url(url: str) -> str:
    """Fallback title: the last path segment, de-slugged."""
    path = urlparse(url).path.strip("/")
    if not path:
        return "Home"
    segment = path.split("/")[-1]
    for ext in (".html", ".htm", ".php", ".aspx"):
        if segment.endswith(ext):
            segment = segment[: -len(ext)]
            break
    return segment.replace("-", " ").replace("_", " ").title() or "Home"


def description_from_url(url: str) -> str:
    """Fallback description: a two-level breadcrumb, e.g. "Blog > Best Bags"."""
    path = urlparse(url).path.strip("/")
    if not path:
        return "Homepage"
    parts = [p.replace("-", " ").replace("_", " ").title() for p in path.split("/") if p]
    return " > ".join(parts[-2:])


def domain_of(url: str) -> str:
    """The host, lowercased, with one leading `www.` removed.

    Deliberately identical to `app.db.repo.domain_of`, and kept separate only
    because `app/core/` holds no database import. The two are the same rule and
    have to stay the same rule: this one names the site in the rendered file,
    that one keys every table, and a client reading `WWW.Example.com` at the top
    of their `llms.txt` while the app files them under `example.com` is one
    disagreement with two visible halves.

    Two corrections against the original one-liner, both of which the repo copy
    already had:

    * **Lowercase first.** Hostnames are case-insensitive, and `removeprefix` is
      not, so `WWW.REDSPOT.COM.AU` kept its prefix and became a third spelling of
      a domain the tables were already splitting two ways.
    * **`removeprefix`, not `replace`.** `replace` strips the substring wherever
      it occurs, so a host that contains `www.` after the first label loses it
      from the middle and resolves to a domain that is not the client's.
    """
    return urlparse(url).netloc.lower().removeprefix("www.")


def estimate_tokens(text: str) -> int:
    """Rough estimate: ~4 characters per token for English prose."""
    return max(1, len(text) // 4)


def content_fingerprint(markdown: str, min_chars: int = 200) -> str:
    """A stable hash of a page's visible content, for exact-duplicate detection.

    Normalised before hashing -- whitespace collapsed, case folded -- so two URLs
    serving the same page with different indentation still collide. Deliberately
    exact rather than fuzzy: this catches a page reachable at two URLs, which is the
    common case on a crawl. Near-duplicate detection over templated pages is a
    different problem with a different tool.

    Returns "" for content too short to be worth comparing, which keeps thin pages
    and empty extractions from all hashing to the same value and deduplicating each
    other out of existence.
    """
    import hashlib
    import re

    normalised = re.sub(r"\s+", " ", (markdown or "").strip().casefold())
    if len(normalised) < min_chars:
        return ""
    return hashlib.blake2b(normalised.encode("utf-8"), digest_size=16).hexdigest()
