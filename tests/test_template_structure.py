"""Structural properties every template has to hold, whatever it renders.

Two bugs prompted this file and neither was visible in a browser, which is why
they survived. One put 45 lines of a working panel outside its own block, so the
route computed four values for markup Jinja never reached. The other nested a
`form` inside a `p`, and the browser silently closed the paragraph early -- the
row still looked right because it wrapped anyway.

Both are properties of the file rather than of the page, so both are checked by
reading the source rather than by rendering it. A test that rendered the page and
looked at the output would have passed on the second one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"

#: Every template that extends another, i.e. every one whose markup has to live
#: inside a block. A standalone template (`base.html`, `client/base.html`) is
#: exempt because it has no parent to put markup into.
_EXTENDS = re.compile(r"{%-?\s*extends\s")
_BLOCK_CONTENT_END = re.compile(r"{%-?\s*endblock\s*-?%}")

#: `form` is flow content, `p` accepts only phrasing content. The parser closes
#: the paragraph rather than reporting an error, so the element lands outside
#: whatever layout the paragraph established.
_NOT_PHRASING = ("form", "div", "ul", "ol", "table", "section", "h1", "h2", "h3")


def _templates() -> list[Path]:
    return sorted(p for p in TEMPLATES.rglob("*.html"))


def _stripped(text: str) -> str:
    """The file without Jinja comments, which may legitimately mention anything."""
    return re.sub(r"{#.*?#}", "", text, flags=re.S)


@pytest.mark.parametrize("path", _templates(), ids=lambda p: str(p.name))
def test_no_markup_sits_outside_a_block(path: Path):
    """A child template's markup after the last `endblock` never renders.

    `admin/costs.html` closed its content block before an entire "Interactive
    spend" panel, so 45 lines of table were dead and the route kept computing
    `by_stage`, `interactive_usd`, `interactive_calls` and `interactive_ceiling`
    to fill it. Nothing failed; the page was simply missing a section nobody had
    seen recently enough to miss.
    """
    text = _stripped(path.read_text(encoding="utf-8"))
    if not _EXTENDS.search(text):
        return

    ends = list(_BLOCK_CONTENT_END.finditer(text))
    assert ends, f"{path.name} extends a parent but closes no block"

    trailing = text[ends[-1].end() :].strip()

    assert trailing == "", (
        f"{path.name} has markup after its last endblock, which never renders:\n{trailing[:200]}"
    )


@pytest.mark.parametrize("path", _templates(), ids=lambda p: str(p.name))
def test_no_paragraph_contains_flow_content(path: Path):
    """A `p` accepts phrasing content only; anything else closes it early.

    `partials/checked.html` put the Refresh form inside `<p class="hint checked">`.
    `.checked` is `display: flex`, so the button was meant to sit on the row with
    the text -- and did not, because the paragraph had already ended. It looked
    correct because the row wraps anyway, which is the reason this is a test and
    not a screenshot.
    """
    text = _stripped(path.read_text(encoding="utf-8"))

    offenders: list[str] = []
    for opening in re.finditer(r"<p(?:\s[^>]*)?>", text):
        closing = text.find("</p>", opening.end())
        inner = text[opening.end() : closing if closing != -1 else len(text)]
        for tag in _NOT_PHRASING:
            if re.search(rf"<{tag}(?:\s|>)", inner):
                offenders.append(f"<{tag}> inside {opening.group(0)}")

    assert offenders == [], f"{path.name}: {offenders}"


#: Classes whose CSS makes them a scroll container. A container that scrolls and
#: cannot take focus cannot be scrolled from the keyboard at all -- SC 2.1.1, and
#: not a theoretical one: `pre.output` on a redspot run showed 512px of a
#: 12,727px region, so 96% of the file the page exists to show was mouse-only.
_SCROLLERS = ("output", "doc-scroll")


@pytest.mark.parametrize("path", _templates(), ids=lambda p: str(p.name))
def test_every_scroll_container_can_take_focus(path: Path):
    """Whatever scrolls must be reachable by keyboard.

    Checked against the class list rather than by parsing the stylesheet, because
    the failure is a property of the pairing: the CSS says `overflow: auto` and
    the markup has to say `tabindex`. A test on either half alone passes while
    the page stays broken.
    """
    text = _stripped(path.read_text(encoding="utf-8"))

    missing: list[str] = []
    for element in re.finditer(r"<(?:pre|div)\s[^>]*>", text):
        tag = element.group(0)
        classes = re.search(r'class="([^"]*)"', tag)
        if classes is None:
            continue
        names = classes.group(1).split()
        if any(scroller in names for scroller in _SCROLLERS) and 'tabindex="0"' not in tag:
            missing.append(tag[:90])

    assert missing == [], f"{path.name}: scroll container not focusable: {missing}"
