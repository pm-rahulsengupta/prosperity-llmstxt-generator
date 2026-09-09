def test_a_mixed_spelling_is_found_through_its_inflections():
    """`\b{word}\b` counted only the exact base form, so `licensed` twenty times
    beside `licence` once reported nothing -- the American count was zero and a
    conflict needs both sides.

    The redspot case in the docstring was caught only because that pair happens
    to be uninflected in the text. The next one would not have been.
    """
    from app.core.copyrules import locale_conflicts

    assert locale_conflicts("optimize this, optimised that")
    assert locale_conflicts("analyze this, analysing that")
    assert locale_conflicts("we organize it, they organised it")


def test_a_noun_verb_pair_is_not_a_mixed_spelling():
    """The counter-example to the fix above, and the reason it is bounded.

    In British English the noun is `licence` and the verb is `license`, so
    "licences ... licensed ... licensing" is correct rather than mixed. Counting
    inflections across that pair reported a correctly written British file as
    inconsistent -- a false positive introduced by the fix for a false negative,
    which is the trade this sweep exists to avoid.

    The original report cited `licensed` x20 beside `licence` x1 as a missed
    conflict. It is not one.
    """
    from app.core.copyrules import locale_conflicts

    assert locale_conflicts("licence " + "licensed " * 20) == []
    assert locale_conflicts("all licences here, licensed and licensing") == []
    # The base forms together are still a real conflict.
    assert locale_conflicts("a licence and a license")


def test_one_spelling_throughout_is_not_a_conflict():
    """The rule reports mixing, not dialect. Widening the match must not start
    reporting a consistent file."""
    from app.core.copyrules import locale_conflicts

    assert locale_conflicts("all licences here, licensed and licensing") == []
    assert locale_conflicts("colour, colours and colouring") == []
    assert locale_conflicts("we optimise, optimised and are optimising") == []
