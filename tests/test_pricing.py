"""Costing a run — and refusing to pretend an unknown cost is a zero one."""

from __future__ import annotations

from app.core.pricing import CostTotals, cost_of, rate_for, totals_of, usd


def stats(by_model: dict, serp: int = 0) -> dict:
    prompt = sum(m.get("prompt", 0) for m in by_model.values())
    completion = sum(m.get("completion", 0) for m in by_model.values())
    return {
        "llm": {
            "calls": {"plan": 1},
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "by_model": by_model,
        },
        "serp_calls": serp,
    }


def test_a_dated_snapshot_matches_its_base_model():
    """OpenAI resolves `gpt-4o` to something like `gpt-4o-2024-11-20`."""
    assert rate_for("gpt-4o-2024-11-20") == rate_for("gpt-4o")


def test_the_longest_prefix_wins():
    """`gpt-4o-mini` must not be priced as `gpt-4o` — it is ~16x cheaper."""
    assert rate_for("gpt-4o-mini-2024-07-18") == rate_for("gpt-4o-mini")
    assert rate_for("gpt-4o-mini") != rate_for("gpt-4o")


def test_an_unknown_model_has_no_rate():
    assert rate_for("some-model-we-have-never-seen") is None


def test_a_priced_run_totals_correctly():
    # 1M input + 1M output on gpt-4o = $2.50 + $10.00
    cost = cost_of(stats({"gpt-4o": {"calls": 1, "prompt": 1_000_000, "completion": 1_000_000}}))
    assert round(cost.llm_usd, 2) == 12.50
    assert cost.fully_priced
    assert cost.unpriced_models == []


def test_an_unknown_model_is_unpriced_not_free():
    """The rule the whole page rests on: unknown is not zero."""
    cost = cost_of(stats({"mystery-model": {"calls": 1, "prompt": 500_000, "completion": 100_000}}))

    assert cost.llm_usd == 0.0
    assert not cost.fully_priced
    assert cost.unpriced_models == ["mystery-model"]
    # Its tokens are still counted -- we know the volume, just not the price.
    assert cost.unpriced_tokens == 600_000


def test_a_run_from_before_per_model_tracking_is_unpriced():
    """Old rows have tokens but no by_model block. Guessing the model from
    whatever is configured today would silently misprice history."""
    cost = cost_of({"llm": {"prompt_tokens": 30_000, "completion_tokens": 5_000, "calls": {}}})
    assert not cost.fully_priced
    assert cost.unpriced_models == ["(not recorded)"]
    assert cost.unpriced_tokens == 35_000


def test_serp_calls_are_costed():
    cost = cost_of(stats({}, serp=4))
    assert cost.serp_calls == 4
    assert cost.serp_usd > 0


def test_empty_stats_do_not_explode():
    for empty in (None, {}, {"llm": {}}):
        cost = cost_of(empty)
        assert cost.total_usd == 0.0
        assert cost.fully_priced


def test_cost_per_run_divides_by_priced_runs_only():
    """geo-tracker's rule. Dividing by every run reports a per-run cost wrong by
    however much of the fleet could not be priced."""
    priced = cost_of(stats({"gpt-4o": {"calls": 1, "prompt": 1_000_000, "completion": 0}}))
    unpriced = cost_of(stats({"mystery": {"calls": 1, "prompt": 1_000_000, "completion": 0}}))

    totals = totals_of([priced, unpriced])

    assert totals.runs == 2
    assert totals.priced_runs == 1
    assert round(totals.total_usd, 2) == 2.50
    # $2.50 over the one run we can actually price -- not $1.25 over both.
    assert round(totals.cost_per_run, 2) == 2.50
    assert "mystery" in totals.unpriced_models


def test_cost_per_run_is_none_rather_than_zero_when_nothing_is_priced():
    totals = totals_of([cost_of(stats({"mystery": {"calls": 1, "prompt": 10, "completion": 10}}))])
    assert totals.cost_per_run is None
    assert usd(totals.cost_per_run) == "—"


def test_no_runs_is_not_a_division_by_zero():
    assert CostTotals().cost_per_run is None


def test_small_amounts_are_not_rounded_away_to_zero():
    """A triage call costs a fraction of a cent; 2dp would show every small run
    as $0.00 and make the page useless."""
    assert usd(0.0004, 2) == "$0.00"
    assert usd(0.0004, 4) == "$0.0004"
