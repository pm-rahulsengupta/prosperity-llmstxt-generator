"""Spec compliance and quality checks for a generated llms.txt.

Ported from the source tool's nine validators, with the network call taken out.
The source ran up to five blocking `requests.head` calls with a 5s timeout inside
the validator, which sat inside the HTTP request cycle -- worst case 25 seconds
added to every generation, on the same thread serving the page.

Here `validate` is pure. Link liveness is a separate function the worker calls,
and it checks every link rather than the first five, because "the first five URLs
resolve" was never the question anyone was asking.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.core.models import PageEntry, ValidationIssue

# Above this, the file starts eating context that the answer needed.
SIZE_WARN_KB = 50.0

_LINK_LINE = re.compile(r"^- \[.+\]\(https?://.+\): ?")
_ANY_LIST_LINE = re.compile(r"^- ")
_HTML_TAG = re.compile(r"<(div|span|a |p |img |script|style)", re.IGNORECASE)
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

_CONTACT_HINTS = ("contact", "customer-service", "store-locator", "support")


def validate(content: str, pages: Iterable[PageEntry]) -> list[ValidationIssue]:
    """Check a rendered llms.txt against the spec and against practical limits."""
    pages = list(pages)
    issues: list[ValidationIssue] = []
    lines = content.splitlines()

    size_kb = len(content.encode("utf-8")) / 1024
    if size_kb > SIZE_WARN_KB:
        issues.append(
            ValidationIssue(
                "warning",
                f"File size is {size_kb:.1f} KB — recommended to keep under "
                f"{SIZE_WARN_KB:.0f} KB for LLM context efficiency.",
                "size",
            )
        )

    if not content.startswith("# "):
        issues.append(
            ValidationIssue("error", "Missing required H1 title at the top of the file.", "h1")
        )

    if not any(line.startswith(">") for line in lines[:5]):
        issues.append(
            ValidationIssue(
                "error",
                "Missing blockquote summary (> ...) after H1 title — required by the "
                "llms.txt spec.",
                "blockquote",
            )
        )

    if "## " not in content:
        issues.append(
            ValidationIssue(
                "info",
                "No H2 sections found. Grouping pages under sections improves LLM navigation.",
                "sections",
            )
        )

    relative = [
        url
        for line in lines
        for url in _MARKDOWN_LINK.findall(line)
        if not url.startswith(("http://", "https://"))
    ]
    if relative:
        shown = ", ".join(relative[:3])
        more = f" (+{len(relative) - 3} more)" if len(relative) > 3 else ""
        issues.append(
            ValidationIssue(
                "error",
                f"Relative URL found: {shown}{more} — all URLs must be absolute.",
                "relative-url",
            )
        )

    malformed = [
        line for line in lines if _ANY_LIST_LINE.match(line) and not _LINK_LINE.match(line)
    ]
    if malformed:
        issues.append(
            ValidationIssue(
                "warning",
                f"{len(malformed)} malformed link entr"
                f"{'y' if len(malformed) == 1 else 'ies'}, e.g. {malformed[0][:80]!r} — "
                "expected format: `- [Title](url): Description`",
                "link-format",
            )
        )

    if _HTML_TAG.search(content):
        issues.append(
            ValidationIssue(
                "warning",
                "HTML tags detected in output — llms.txt should be plain Markdown, not HTML.",
                "html",
            )
        )

    has_contact = "## Contact" in content or any(
        "contact" in p.display_title.lower() or any(h in p.url.lower() for h in _CONTACT_HINTS)
        for p in pages
    )
    if not has_contact:
        issues.append(
            ValidationIssue(
                "info",
                "No Contact / Customer Service section detected. Consider adding contact "
                "pages, store locators, or support links so AI systems can answer queries "
                "like 'how do I contact [brand]' or '[brand] near me'.",
                "contact",
            )
        )

    if "llms-full.txt" not in content:
        issues.append(
            ValidationIssue(
                "info",
                "No llms-full.txt reference found. Consider linking to the full companion file.",
                "full-reference",
            )
        )

    return issues


def issues_from_link_check(results: dict[str, int | str]) -> list[ValidationIssue]:
    """Turn a URL -> status map into issues.

    Kept separate from `validate` so the pure path stays pure: the worker performs
    the fetches and hands the outcome here.
    """
    broken = {url: status for url, status in results.items() if _is_broken(status)}
    if not broken:
        return []
    sample = ", ".join(f"{url} ({status})" for url, status in list(broken.items())[:5])
    more = f" (+{len(broken) - 5} more)" if len(broken) > 5 else ""
    return [
        ValidationIssue(
            "warning",
            f"{len(broken)} link(s) did not resolve: {sample}{more}",
            "broken-link",
        )
    ]


def _is_broken(status: int | str) -> bool:
    return not isinstance(status, int) or status >= 400


def worst_level(issues: Iterable[ValidationIssue]) -> str:
    levels = {i.level for i in issues}
    for level in ("error", "warning", "info"):
        if level in levels:
            return level
    return "ok"
