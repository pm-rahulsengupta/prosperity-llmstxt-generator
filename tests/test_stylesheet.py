"""Properties of the stylesheet that a rendered page cannot show you.

Every rule here guards a defect that shipped and was invisible: a class used in
eight templates with no rule behind it renders as body text, which looks like a
deliberately plain sentence rather than like a mistake. Nothing failed, nothing
logged, and the most severe message on a component card was the quietest thing
on it for as long as anyone had been looking at that card.

So these read the sheet and the templates together. A test on either alone
passes while the page is wrong: the markup is valid, and the CSS is valid, and
the pairing is what is missing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "static" / "css" / "main.css").read_text(encoding="utf-8")

#: Roughly a third of this stylesheet is prose explaining why a rule is what it
#: is, and that prose names the very properties these tests look for -- the
#: comment introducing the focus ring says "box-shadow is reserved entirely for
#: focus", which a naive scan reads as a use of it. Anything asserting about
#: declarations has to read this, not `CSS`.
RULES = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)


def _relative_luminance(hex_colour: str) -> float:
    raw = hex_colour.lstrip("#")
    channels = [int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    la, lb = _relative_luminance(a), _relative_luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def token(name: str) -> str:
    """The value of a custom property, read out of `:root`."""
    found = re.search(rf"^\s*{re.escape(name)}:\s*([^;]+);", CSS, re.M)
    assert found is not None, f"{name} is not declared"
    return found.group(1).strip()


# -- the class that had no rule ------------------------------------------------


def test_hint_has_a_base_rule():
    """46 usages, six rules, and every one of them descendant-scoped.

    31 hints inherited body type, so on a family tab the sentence explaining a
    component rendered at the same size as the component's own heading. Nothing
    guarded this because a class with no rule is not an error anywhere.
    """
    assert re.search(r"^\.hint\s*\{", CSS, re.M), "`.hint` has no base rule"


def test_the_hint_is_the_secondary_prose_tier_not_the_smallest_thing_on_the_page():
    """A hint qualifies the claim above it; it is not chrome.

    The directory-sampling caveat is a hint, and it exists precisely so an
    operator does not over-claim to a client. At the smallest size in the dimmest
    passing grey it is the thing they skip.
    """
    block = CSS.split(".hint {", 1)[1].split("}", 1)[0]

    assert "var(--pm-sm)" in block, "a hint should sit at the secondary-prose size"
    assert "var(--pm-ink-soft)" in block, "a hint should not be the dimmest text on the page"


# -- classes used in templates with nothing behind them ------------------------


@pytest.mark.parametrize("modifier", ["bad", "error", "ok", "wait"])
def test_every_notice_modifier_has_a_rule(modifier: str):
    """`.notice.bad` carries "Not for publication" and had no rule at all."""
    assert re.search(rf"\.notice\.{modifier}\b", CSS), f".notice.{modifier} has no rule"


def test_every_component_state_has_a_rail():
    """Four of five states had one, so the fifth read as an unfinished card.

    Derived from the enum rather than from a list, so a state added later fails
    here instead of rendering as a bare card.
    """
    from app.core.components import ComponentState

    missing = [
        state.value
        for state in ComponentState
        if not re.search(rf"\.component\.{state.value}\b", CSS)
    ]

    assert missing == [], f"component states with no rail: {missing}"


def test_a_disclosure_marks_itself_as_a_control():
    """`details.spec` renders on eight pages and had no rule, so its summary was
    indistinguishable from the prose around it."""
    assert "details.spec" in CSS


# -- the colour rules the sheet states about itself ----------------------------


def test_the_red_that_may_be_text_actually_may_be():
    """`--pm-red` is 4.09:1 on white. Its own comment says it has no accessible
    text pairing, and three rules used it as one anyway."""
    deep = token("--pm-red-deep")

    assert contrast(deep, "#ffffff") >= 4.5, "red-as-text fails AA on white"
    assert contrast("#ffffff", deep) >= 4.5, "white-on-red fails AA for the button"
    # `a:hover` paints the emerald wash underneath a destructive link, and the
    # undeepened red measured *worse* there (3.71:1) than at rest.
    assert contrast(deep, token("--pm-emerald-wash")) >= 4.5, "fails against the hover wash"


def test_imperial_red_is_still_only_a_stroke():
    """The deepened red is for text and fill. The original keeps every border it
    has, and is still fine there -- 1.4.11 wants 3:1 for a boundary."""
    assert contrast(token("--pm-red"), token("--pm-red-wash")) >= 3.0


def test_the_focus_ring_is_declared_once():
    """`box-shadow` is reserved entirely for focus, which is what makes the ring
    unambiguous on every surface. Naming it is what stops it drifting."""
    assert "--pm-focus-ring" in CSS

    rings = re.findall(r"box-shadow:\s*0 0 0 5px", RULES)
    assert rings == [], f"a focus ring is spelled out rather than named: {len(rings)}"


def test_there_is_no_elevation_scale():
    """Depth here is fill plus hairline, because that is the only kind of
    boundary WCAG 1.4.11 can measure. A shadow would quietly reintroduce the
    failure `--pm-rule-strong` was added to fix.
    """
    shadows = [
        line.strip()
        for line in RULES.splitlines()
        if "box-shadow" in line and "none" not in line and "--pm-focus-ring" not in line
    ]

    assert shadows == [], f"box-shadow used for something other than focus: {shadows}"
