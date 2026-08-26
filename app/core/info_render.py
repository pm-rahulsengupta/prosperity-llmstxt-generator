"""HTML, built so that raw interpolation is not available.

Every other renderer in this package targets markdown, plain text or
`json.dumps`, where injection is not a concern and escaping came free. This one
interpolates crawled page titles, operator free text and model prose into markup
that will be published on a client's domain. There is no precedent in this
codebase for getting that right, so the module builds one choke point and makes
going around it visible.

**Not Jinja.** The app's `Jinja2Templates` autoescapes, and that is the right
tool for the app's own pages. An artifact is assembled the way every other
artifact here is assembled, and mixing a template engine in invites the first
`|safe` somebody needs at four in the afternoon. Instead `el()` escapes
everything that is not already an `Html`, and a source-level test asserts no
f-string in this module interpolates into markup outside it.

**Escaping is context-dependent, and two contexts are refused rather than
escaped.** `html.escape` is correct for text and for double-quoted attribute
values. It does nothing useful for a URL scheme: `href="javascript:alert(1)"` is
already escaped and still executes. So `href` and `src` are validated against an
allowlist of schemes *and* against the set of URLs a probe actually saw, before
escaping ever happens. There is no `<script>` at all, and the one `<style>` block
is a literal with nothing interpolated into it.

**Double-escaping is the likeliest visible defect.** Crawled titles routinely
arrive already carrying `&amp;` from a CMS that escaped them once. Escaping again
gives `&amp;amp;` on a client's page. The rule is decided once, here --
`unescape` then `escape` -- so `Ben &amp; Jerry's` and `Ben & Jerry's` produce
the same output, and a test pins both.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urlparse

__all__ = ["SAFE_SCHEMES", "Html", "attr", "el", "text"]

#: Everything else -- `javascript:`, `data:`, `vbscript:` -- is dropped rather
#: than escaped, because escaping does not disarm a scheme.
SAFE_SCHEMES: frozenset[str] = frozenset({"https", "http", "mailto"})

_TAG = re.compile(r"^[a-z][a-z0-9]*$")
#: Tags that carry no closing form.
_VOID: frozenset[str] = frozenset({"meta", "link", "br", "hr", "img", "input"})


class Html(str):
    """A string that is already escaped.

    The only thing `el` will not escape again. Anything reaching `el` that is not
    one of these is treated as hostile text, which is the correct default for
    crawled titles, operator free text and model prose alike.
    """

    __slots__ = ()


def text(value: str) -> Html:
    """Escape once, and only once.

    `unescape` first because crawled titles frequently arrive already escaped by
    the source CMS. Without it `Ben &amp; Jerry's` renders as `Ben &amp;amp;
    Jerry's` on a client's page -- the defect nobody catches in review because it
    looks like a data problem rather than a code one.
    """
    return Html(html.escape(html.unescape(str(value)), quote=True))


def attr(name: str, value: str) -> Html:
    """One attribute, always double-quoted.

    Quoting is not optional. An unquoted attribute value can end the attribute
    with a space, and `html.escape` does not stop that -- it escapes the quote
    characters that unquoted markup never had.
    """
    return Html(f'{name}="{html.escape(html.unescape(str(value)), quote=True)}"')


def safe_url(value: str, *, allowed: frozenset[str] | None = None) -> str:
    """A URL fit to appear in an `href`, or the empty string.

    Two gates, and the second is the one that matters here. The scheme must be
    one we are willing to publish; and where the caller supplies `allowed`, the
    URL must be one a probe actually saw. That second gate is this module's half
    of the rule AGT-004 enforces for markdown -- an `href` is a URL an agent
    follows just as much as a bare one.
    """
    candidate = str(value).strip()
    if not candidate:
        return ""
    scheme = (urlparse(candidate).scheme or "").lower()
    if scheme not in SAFE_SCHEMES:
        return ""
    if allowed is not None and candidate.rstrip("/") not in allowed:
        return ""
    return candidate


def el(tag: str, *children: str, **attrs: str) -> Html:
    """One element. Every attribute value and every non-`Html` child is escaped.

    Attribute names are restricted to word characters and dashes, so a key can
    never introduce markup, and `class_` maps to `class` because `class` is a
    keyword.
    """
    if not _TAG.match(tag):
        raise ValueError(f"not a tag name: {tag!r}")

    rendered = []
    for key, value in attrs.items():
        name = key.rstrip("_").replace("_", "-")
        if not re.match(r"^[a-z][a-z0-9-]*$", name):
            raise ValueError(f"not an attribute name: {key!r}")
        if value == "":
            continue
        rendered.append(str(attr(name, value)))

    opening = " ".join([tag, *rendered])
    if tag in _VOID:
        return Html(f"<{opening}>")

    inner = "".join(
        str(child) if isinstance(child, Html) else str(text(child)) for child in children
    )
    return Html(f"<{opening}>{inner}</{tag}>")


def join(*parts: str) -> Html:
    """Concatenate already-escaped fragments without re-escaping them."""
    return Html("".join(str(part) for part in parts))
