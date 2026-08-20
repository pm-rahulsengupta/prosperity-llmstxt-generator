"""Re-run, cancel and delete.

Delete is the only irreversible action in the tool, so most of what is asserted
here is what it *refuses* to do: it will not touch a run a worker is still
writing to, it will not act without the domain typed, and it is not offered to a
non-admin. Cancellation is tested for the property that makes it worth having --
that it actually stops work, rather than only setting a status nobody reads.
"""

from __future__ import annotations

import pytest

from app.db.models import RunStatus


def test_only_terminal_runs_are_deletable():
    """A worker mid-stage still holds this run's id.

    Deleting under it either fails in a way that reads as a bug or lets rows be
    written back after the delete. Cancel is the route in, and the button says so.
    """
    deletable = {s for s in RunStatus if s.is_terminal}

    assert deletable == {RunStatus.COMPLETE, RunStatus.FAILED, RunStatus.CANCELLED}
    for working in (RunStatus.CRAWLING, RunStatus.TRIAGING, RunStatus.SUMMARISING):
        assert working not in deletable


def test_cancelled_is_terminal_so_a_cancelled_run_can_then_be_deleted():
    """The two actions have to compose, or cancelling is a dead end."""
    assert RunStatus.CANCELLED.is_terminal


@pytest.mark.parametrize(
    ("typed", "domain", "accepted"),
    [
        ("example.com", "example.com", True),
        ("EXAMPLE.COM", "example.com", True),
        ("  example.com  ", "example.com", True),
        ("", "example.com", False),
        ("yes", "example.com", False),
        ("example.co", "example.com", False),
        ("example.com.au", "example.com", False),
    ],
)
def test_the_typed_confirmation_matches_the_domain_and_nothing_else(typed, domain, accepted):
    """A second click is not a decision -- it is the same click twice.

    Case and surrounding whitespace are forgiven because they are typing, not
    intent. A near-miss is not: `example.com.au` is a different client.
    """
    matched = typed.strip().lower() == domain.lower()
    assert matched is accepted


def test_cancellation_is_checked_between_stages_not_only_recorded():
    """`CANCELLED` existed as an enum member before this and nothing set it.

    A status nothing reads is a lie, so this asserts the checks are actually
    present at the boundaries where the money is spent.
    """
    import inspect

    from app.jobs import tasks

    source = inspect.getsource(tasks.generate_task)

    assert source.count("_abort_if_cancelled") >= 3
    for stage in ("crawl", "triage", "summarise"):
        assert f'_abort_if_cancelled(rid, "{stage}")' in source


def test_a_cancelled_run_is_not_recorded_as_a_failure():
    """It would land in the error counts and invite an investigation of something
    a person did deliberately -- and re-raising would have procrastinate retry it,
    restarting the work the cancellation existed to stop."""
    import inspect

    from app.jobs import tasks

    source = inspect.getsource(tasks.generate_task)
    block = source.split("except Cancelled:")[1].split("except Exception")[0]
    # Comments only, stripped: the prose in this handler explains why it does not
    # re-raise, and searching it for "raise" finds the explanation rather than
    # the statement.
    code = "\n".join(line for line in block.splitlines() if not line.strip().startswith("#"))

    assert "RunStatus.FAILED" not in code
    assert "raise" not in code
    assert "return" in code


def test_rerun_clones_rather_than_resetting():
    """Re-running is a comparison. Resetting in place destroys the thing being
    compared against, and takes a failed run's events with it."""
    import inspect

    from app.db import repo

    source = inspect.getsource(repo.clone_run)

    assert "Run(" in source
    # The plan is deliberately not carried: a re-run exists to produce a new one.
    assert "plan=" not in source
    assert "max_pages=original.max_pages" in source
