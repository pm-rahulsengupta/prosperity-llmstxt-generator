"""The edit wrapper must fail on every no-op that actually happened."""

from __future__ import annotations

import pytest

from tools.edit import EditError, ensure_absent, ensure_present, insert_before, replace_once


@pytest.fixture
def sample(tmp_path):
    path = tmp_path / "m.py"
    path.write_text('x = "a"\ny = "b"\nz = "a"\n', encoding="utf-8")
    return path


def test_a_replacement_that_matches_nothing_raises(sample):
    """The whole point. `str.replace` returns a string either way."""
    with pytest.raises(EditError, match="not found"):
        replace_once(sample, "nonexistent", "new")


def test_the_reformatted_quote_case_is_named(sample):
    """Ruff turning 'a' into "a" under a search string. Happened twice."""
    with pytest.raises(EditError) as exc:
        replace_once(sample, "x = 'a'", "x = 'c'")
    assert "Closest lines" in str(exc.value)
    assert 'x = "a"' in str(exc.value)


def test_an_already_applied_edit_says_so_rather_than_just_failing(sample):
    with pytest.raises(EditError, match="already been applied"):
        replace_once(sample, 'x = "old"', 'x = "a"')


def test_matching_more_often_than_intended_raises(sample):
    """Two of the three matches would be edits nobody looked at."""
    with pytest.raises(EditError, match="matches 2 times"):
        replace_once(sample, '"a"', '"c"')


def test_a_deliberate_multi_match_is_allowed(sample):
    replace_once(sample, '"a"', '"c"', count=2)
    assert sample.read_text(encoding="utf-8").count('"c"') == 2


def test_a_successful_replacement_writes(sample):
    replace_once(sample, 'y = "b"', 'y = "z"')
    assert 'y = "z"' in sample.read_text(encoding="utf-8")


def test_replacing_something_with_itself_raises(sample):
    with pytest.raises(EditError, match="no change"):
        replace_once(sample, 'y = "b"', 'y = "b"')


def test_a_missing_file_raises_rather_than_creating_one(tmp_path):
    with pytest.raises(EditError, match="does not exist"):
        replace_once(tmp_path / "nope.py", "a", "b")


def test_insert_before_puts_it_before(sample):
    insert_before(sample, 'y = "b"', "# note\n")
    assert '# note\ny = "b"' in sample.read_text(encoding="utf-8")


def test_ensure_present_catches_the_formatter_removing_an_edit(sample):
    """The fourth no-op: the edit landed, ruff deleted the unused half."""
    ensure_present(sample, 'x = "a"')
    with pytest.raises(EditError, match="expected but absent"):
        ensure_present(sample, "from app.core.onboarding import SiteBrief")


def test_ensure_absent_catches_a_removal_that_did_not_remove(sample):
    with pytest.raises(EditError, match="still present"):
        ensure_absent(sample, 'x = "a"')
