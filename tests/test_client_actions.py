"""Managing a client: stopping a crawl, deleting, and what adding one does.

These are the controls an operator reaches for when something has gone wrong --
a run that never finished, a client added by mistake -- so the tests are about
them being reachable and about them refusing to do the wrong thing quietly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.presentation import RUN_LOOK, Tone, run_look
from app.db.models import RunStatus

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"


def markup(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


# -- coming back to where you were, without becoming an open redirect ----------


def test_a_return_path_on_this_origin_is_honoured():
    from app.main import _back_to

    assert _back_to("/clients", "/runs/x") == "/clients"
    assert _back_to("/sites/a.example/settings", "/runs/x") == "/sites/a.example/settings"


@pytest.mark.parametrize(
    "hostile",
    [
        "https://evil.example/steal",
        "http://evil.example",
        # The one a bare `startswith("/")` check lets straight through: browsers
        # read a protocol-relative URL as a different origin.
        "//evil.example",
        "////evil.example",
        # Browsers normalise a backslash to a slash in the authority position,
        # so these are scheme-relative to Chrome and Firefox.
        chr(47) + chr(92) + "evil.example",
        chr(47) + chr(92) + chr(92) + "evil.example",
        "javascript:alert(1)",
        "",
        "   ",
    ],
)
def test_anything_off_this_origin_falls_back(hostile):
    """A form field that reaches a `Location` header is an open redirect.

    `back` is posted by the client list and by the client profile, so it is
    attacker-controllable by anyone who can get an operator to submit a form.
    """
    from app.main import _back_to

    assert _back_to(hostile, "/runs/x") == "/runs/x"


def test_the_cancel_form_posts_a_return_path_from_both_places():
    """Bouncing an operator to a run page they did not ask for loses their place."""
    assert '<input type="hidden" name="back" value="/clients">' in markup("clients.html")
    assert 'name="back" value="/sites/{{ domain }}/settings"' in markup("client_settings.html")


# -- how a run reads -----------------------------------------------------------


@pytest.mark.parametrize("state", list(RunStatus))
def test_every_run_state_has_words_of_its_own(state):
    """`run.html` and `index.html` each printed the enum with its underscores
    swapped for spaces, so a run sat at "awaiting review" -- which names the
    state of the machine rather than saying the operator is what it waits for."""
    look = run_look(state)

    assert look.headline
    assert "_" not in look.headline


def test_the_state_that_is_waiting_on_a_person_says_so():
    assert run_look(RunStatus.AWAITING_REVIEW).headline == "Waiting for you"
    assert "spent" in run_look(RunStatus.AWAITING_REVIEW).holding


def test_only_a_finished_run_reads_as_good():
    good = {state for state in RunStatus if run_look(state).tone is Tone.GOOD}

    assert good == {RunStatus.COMPLETE}


def test_a_stopped_run_is_quiet_rather_than_a_failure():
    """Somebody chose to stop it. Painting that red reports a fault that is not one."""
    assert run_look(RunStatus.CANCELLED).tone is Tone.QUIET
    assert run_look(RunStatus.FAILED).tone is Tone.BAD


def test_the_table_covers_the_enum_exactly():
    """A stage added to `RunStatus` must not fall through to the raw value."""
    assert set(RUN_LOOK) == {state.value for state in RunStatus}


def test_an_unknown_state_does_not_read_as_finished():
    assert run_look("something_new").tone is not Tone.GOOD


# -- in flight -----------------------------------------------------------------


def test_in_flight_is_derived_from_the_enum_rather_than_listed():
    """A hand-written list would quietly stop counting a stage added later."""
    from app.db import repo

    assert set(repo.IN_FLIGHT) == {s for s in RunStatus if not s.is_terminal}
    assert RunStatus.COMPLETE not in repo.IN_FLIGHT
    assert RunStatus.CANCELLED not in repo.IN_FLIGHT


# -- adding a client -----------------------------------------------------------


def test_the_first_check_is_on_by_default_in_the_form():
    """Without it a new client has no snapshot, so the overview, the checklist
    and the handover all render "nobody has checked this yet" -- a client added
    and left looks identical to one whose site could not be reached."""
    form = markup("client_new.html")

    assert 'name="check_now"' in form
    assert "checked>" in form


def test_the_route_does_not_default_the_checkbox_to_true():
    """An unchecked box sends *nothing*.

    So a `Form(True)` default would make unticking it do nothing at all -- the
    control would be decoration. The `checked` attribute is the default an
    operator sees; the default for a request that omits the field is "do not
    touch their server".
    """
    import inspect

    from app.main import create_client

    assert inspect.signature(create_client).parameters["check_now"].default.default is False


def test_adding_a_client_ends_in_onboarding():
    import inspect

    from app.main import create_client

    source = inspect.getsource(create_client)

    assert '/brief"' in source, "adding a client must lead into the brief"


def test_a_failed_first_check_does_not_lose_the_client():
    """An unreachable site, a WAF or a timeout must not undo the add.

    The config is committed before the probe runs, and the probe has its own
    handler -- so the client is on file with no snapshot, which is the state the
    "Check the site now" button on their profile exists to fix.
    """
    import inspect

    source = inspect.getsource(__import__("app.main", fromlist=["create_client"]).create_client)
    body = source.split("if check_now:", 1)

    assert len(body) == 2, "the check is no longer conditional"
    assert "await session.commit()" in body[0], "the client is not committed before the probe"
    assert "except Exception:" in body[1], "a failed probe would 500 and roll the client back"


# -- one delete surface --------------------------------------------------------


def test_the_delete_page_is_admin_only():
    """The POST is `require_admin`; a GET anyone could open would offer a 403."""
    import inspect

    from app.main import confirm_delete_client

    depends = inspect.signature(confirm_delete_client).parameters["user"].default

    assert depends.dependency.__name__ == "require_admin"


def test_the_preview_and_the_delete_count_the_same_rows():
    """What an operator confirms and what actually goes cannot disagree.

    Both call `_client_row_counts`; this is the guard on one of them growing its
    own query later.
    """
    import inspect

    from app.db import repo

    for function in (repo.preview_client_deletion, repo.delete_client):
        assert "_client_row_counts" in inspect.getsource(function)


# -- a client with a crawl still running ---------------------------------------


def test_the_delete_page_refuses_while_a_crawl_is_running():
    """`run.html` already states this rule for a single run.

    Deleting the *client* removed every one of its runs with no such check --
    the same hazard with a larger blast radius. Found with two clients sat
    unfinished on the live instance, one of them the client it was about to be
    used on.
    """
    import sys

    sys.path.insert(0, str(ROOT / "tests"))
    from tests.test_nav import _render

    html = _render(
        "client_delete.html",
        domain="x.example",
        error=None,
        unfinished_run_id="abc-123",
        unfinished_run_state="Crawling",
    )

    assert 'action="/sites/x.example/delete"' not in html, "the delete form is still reachable"
    assert 'action="/runs/abc-123/cancel"' in html, "refused with no way to satisfy the rule"
    assert "worker mid-stage" in html


def test_the_delete_form_returns_once_nothing_is_running():
    import sys

    sys.path.insert(0, str(ROOT / "tests"))
    from tests.test_nav import _render

    html = _render(
        "client_delete.html",
        domain="x.example",
        error=None,
        unfinished_run_id="",
        unfinished_run_state="",
    )

    assert 'action="/sites/x.example/delete"' in html
    assert "Type <code>x.example</code> to confirm" in html


def test_the_post_checks_too_rather_than_trusting_the_page():
    """A confirmation page left open for ten minutes says nothing about now."""
    import inspect

    from app.main import delete_client_route

    source = inspect.getsource(delete_client_route)
    guard = source.split("form = await request.form()", 1)[0]

    assert "unfinished_runs" in guard, "the POST would delete a client mid-crawl"
    assert "HTTP_409_CONFLICT" in guard


def test_stopping_from_the_delete_page_returns_to_it():
    """So the delete an operator came to do is one click away once it is safe."""
    assert 'name="back" value="/sites/{{ domain }}/delete"' in markup("client_delete.html")


def test_the_profile_reads_every_in_flight_run_not_only_the_ones_it_lists():
    """It scanned the five rows it renders, which is not the same question.

    Measured live: prosperitymedia.com.au had a run Queued for five days sitting
    *sixth*, so the profile found nothing while the client list -- which queries
    every in-flight run -- reported one, and the delete guard refused a delete
    the profile offered no way to unblock.
    """
    import inspect

    from app.main import _settings_context

    source = inspect.getsource(_settings_context)

    assert "repo.unfinished_runs(session)" in source
    assert "for r in runs if not" not in source, "back to scanning the rendered page"
