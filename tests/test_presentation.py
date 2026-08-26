"""How a state reads, and what colour it wears.

These exist because the UI was measured saying something untrue on a live
client: `/llms.txt` carried a positive lime "ready" chip while the site returned
404 for it, and the readiness score counted the same file as a hard failure. The
tab and the score were describing one file and disagreeing.

The tests are about the property, not the wording: a component the probe could
not confirm must never render a chip that reads as good, whatever we happen to
hold for it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.components import COMPONENTS, ComponentState, Family
from app.core.presentation import (
    NOT_APPLICABLE,
    NOT_PUBLISHED,
    PUBLISHED,
    SURFACE_LOOK,
    Tone,
    look_for,
    surface_look,
)
from app.core.site_state import ComponentStatus

ROOT = Path(__file__).resolve().parents[1]


def status(state: ComponentState, *, detail: str = "") -> ComponentStatus:
    component = next(c for c in COMPONENTS if c.family is Family.CONTENT and c.artifact)
    return ComponentStatus(component, state, detail, artifact_name=component.artifact)


# -- the defect this module exists to fix ---------------------------------------


def test_a_file_we_generated_but_they_have_not_published_reads_as_not_published():
    """The measured defect, in one assertion.

    `ready` means *we made a copy*, not *your site has it*. Rendered as a
    positive chip it contradicted both the 404 underneath it and the readiness
    score, which counted the same file as a failure.
    """
    look = look_for(status(ComponentState.READY, detail="404"))

    assert look.headline == NOT_PUBLISHED
    assert look.tone is Tone.BAD


def test_but_it_still_says_we_have_the_file():
    """Leading with the site's state must not lose the remedy.

    The finding is that it is not published; that we have one ready is the
    useful next sentence, not a reason to soften the finding.
    """
    look = look_for(status(ComponentState.READY))

    assert "needs uploading" in look.holding.lower()


def test_only_a_confirmed_probe_earns_a_good_chip():
    """The whole rule, stated once.

    Four of the five states mean the client's site does not have this. Only
    `LIVE` -- which `derive` sets from a probe PASS -- reads as good.
    """
    good = {state for state in ComponentState if look_for(status(state)).tone is Tone.GOOD}

    assert good == {ComponentState.LIVE}


def test_a_published_file_reads_as_published_with_nothing_further():
    look = look_for(status(ComponentState.LIVE, detail="200 text/plain"))

    assert look.headline == PUBLISHED
    assert look.holding == "", "there is no remedy for something already working"


def test_not_applicable_is_quiet_rather_than_alarming():
    """It implies no action, so it must not borrow the colour of anything that does."""
    look = look_for(status(ComponentState.NOT_APPLICABLE))

    assert look.headline == NOT_APPLICABLE
    assert look.tone is Tone.QUIET


def test_template_and_missing_differ_in_words_not_in_colour():
    """Both mean "not published". They differ in what we can do about it.

    Rendering them in different colours -- as the old UI did, teal and red --
    made one situation look like two.
    """
    template = look_for(status(ComponentState.TEMPLATE))
    missing = look_for(status(ComponentState.MISSING))

    assert template.tone is missing.tone is Tone.BAD
    assert template.holding != missing.holding


# -- the surface table -----------------------------------------------------------


def test_absent_no_longer_shares_a_colour_with_a_prepared_file():
    """On one screen, lime meant both "we prepared this" and "the site 404s".

    Measured on the live overview: four surfaces reading `absent` in the same
    lime chip that `ready` used two panels above.
    """
    assert SURFACE_LOOK["absent"].tone is Tone.BAD
    assert SURFACE_LOOK["present"].tone is Tone.GOOD


def test_unreachable_is_not_reported_as_an_absence():
    """A network failure is a fact about us, not about the client's site.

    `agents_probe` is emphatic about this and the table must not flatten it.
    """
    look = SURFACE_LOOK["unreachable"]

    assert look.tone is not Tone.BAD
    assert "could not" in look.headline.lower()


def test_an_unknown_surface_state_does_not_read_as_good():
    """A state added upstream and not mapped here must fail safe."""
    fake = type("S", (), {"state": type("V", (), {"value": "something_new"})()})()

    assert surface_look(fake).tone is not Tone.GOOD


# -- one colour, one meaning -------------------------------------------------------


def test_no_pill_colour_carries_two_meanings():
    """The test that would have caught the original defect.

    `absent` and `ready` shared the lime chip, so the same colour meant "we
    prepared this for you" and "the site publishes nothing". This walks every
    headline the app can render and asserts each tone maps to one idea.
    """
    by_tone: dict[Tone, set[str]] = {}
    for state in ComponentState:
        look = look_for(status(state))
        by_tone.setdefault(look.tone, set()).add(look.headline)
    for look in SURFACE_LOOK.values():
        by_tone.setdefault(look.tone, set()).add(look.headline)

    # A tone may cover several headlines only where they say the same thing:
    # every BAD headline must be a form of "not published".
    bad = by_tone[Tone.BAD]
    assert all("not published" in h.lower() or "wrong type" in h.lower() for h in bad), bad
    assert by_tone[Tone.GOOD] == {PUBLISHED}


def test_no_template_derives_a_pill_class_inline():
    """Colour is decided in one module now.

    Every inline `{% if state == 'live' %}ok{% elif ... %}` was a place the
    mapping could drift, and two of them had already drifted from each other.
    """
    offenders = []
    for path in (ROOT / "templates").rglob("*.html"):
        if "client/" in path.as_posix():
            continue  # the client report has its own vocabulary, tested separately
        text = path.read_text(encoding="utf-8")
        if re.search(r"pill \{%\s*if\s+\w+\.state", text):
            offenders.append(path.name)

    assert offenders == [], f"pill colour derived inline in: {offenders}"


# -- the layout bug ------------------------------------------------------------------


def test_a_wide_template_scrolls_inside_its_own_container():
    """It was `overflow-x: visible`, so a 1996px block pushed the page sideways.

    Asserted against the stylesheet: the markup alone cannot show an overflow.
    """
    css = (ROOT / "static" / "css" / "main.css").read_text(encoding="utf-8")
    block = css.split(".doc-scroll {", 1)

    assert len(block) == 2, "the scroll container is gone"
    assert "overflow-x: auto" in block[1].split("}", 1)[0]


def test_the_template_body_is_wrapped_in_it():
    markup = (ROOT / "templates" / "partials" / "component.html").read_text(encoding="utf-8")

    assert 'doc-scroll"><pre class="doc template"' in markup


# -- the vanishing score --------------------------------------------------------------


def test_the_spec_score_is_not_gated_on_the_file_being_unpublished():
    """`publishable` requires READY, so publishing made the number disappear.

    A number that vanishes reads as "it passed". The file still exists and is
    still worth judging once it is live.
    """
    markup = (ROOT / "templates" / "partials" / "component.html").read_text(encoding="utf-8")

    assert "{% if status.component.artifact and reports is defined %}" in markup
    assert "{% if status.publishable and reports is defined %}" not in markup


@pytest.mark.parametrize("state", list(ComponentState))
def test_every_state_has_a_look(state):
    """A state added upstream must not render a blank chip."""
    assert look_for(status(state)).headline


def test_only_a_probe_decided_detail_is_shown_as_a_finding():
    """`derive` writes three kinds of string into `detail`.

    A probe result ("404", "200 text/plain"), our own bookkeeping ("generated
    and ready to publish"), and -- for a manual mark -- an operator's email
    address. Under the words "What we found" the last two read as claims about
    the client's site, and one of them publishes a colleague's address.

    The client-facing report already draws this line with `probe_decided`; the
    staff card now draws the same one.
    """
    markup = (ROOT / "templates" / "partials" / "component.html").read_text(encoding="utf-8")

    assert "{% if status.detail and status.probe_decided %}" in markup


def test_our_bookkeeping_does_not_render_as_a_finding():
    import sys

    sys.path.insert(0, str(ROOT / "tests"))
    from tests.test_nav import _render

    html = _render("family.html")

    assert "What we found: generated and ready to publish" not in html
    assert "marked done by" not in html


# -- the residue -----------------------------------------------------------------


def test_the_search_data_nav_item_lands_on_something():
    """It pointed at `#search-console`, which existed nowhere.

    So the item landed five panels above the thing it named. I then renamed the
    href to `#search-data` without adding the target, which moved the dead
    anchor rather than fixing it. Both halves are asserted here.
    """
    from app.nav import build_nav

    items = {i.title: i for g in build_nav("/", "x.example") for i in g.items}
    href = items["Search data"].url
    anchor = href.split("#", 1)[1]

    brief = (ROOT / "templates" / "brief.html").read_text(encoding="utf-8")
    assert f'id="{anchor}"' in brief, f"nav points at #{anchor}, which brief.html does not define"


def test_the_sidebar_emits_no_duplicate_ids():
    """We audit clients for this exact thing (WCAG 4.1.1, `unique-ids`).

    Three nav groups each built `hint-1-0`, `hint-2-1`, ... from the inner loop
    only, so every disabled item's `aria-describedby` resolved to whichever
    duplicate the parser met first.
    """
    import sys

    sys.path.insert(0, str(ROOT / "tests"))
    from collections import Counter

    from lxml import html as lxml_html

    from tests.test_nav import _render

    doc = lxml_html.fromstring(_render("clients.html", rows=[], deleted="", domain=""))
    ids = [element.get("id") for element in doc.xpath("//*[@id]")]
    repeated = [value for value, count in Counter(ids).items() if count > 1]

    assert repeated == [], f"duplicate ids: {repeated}"
