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
    return urlparse(url).netloc.replace("www.", "")


def estimate_tokens(text: str) -> int:
    """Rough estimate: ~4 characters per token for English prose."""
    return max(1, len(text) // 4)
