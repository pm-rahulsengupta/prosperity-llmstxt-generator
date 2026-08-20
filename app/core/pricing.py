"""What a run cost, and — just as importantly — what it cannot cost out.

geo-tracker's `/admin/costs` reports figures a provider itself returned, so its
numbers are billed amounts. Ours cannot be: OpenAI's chat completions API returns
token counts, not money, and DataForSEO's SERP response does not price the call
either. So these are **estimates computed from published rates**, and the UI says
so rather than implying an invoice.

The distinction geo-tracker draws between priced and unpriced runs is the part
worth copying exactly. A model with no rate in the table below is not free — it is
unknown — and folding it in at zero would quietly understate the total. It is
counted separately, and the per-run average divides by priced runs only:

    "Averaged over priced runs only: dividing by all runs would report a per-run
     cost that is wrong by however much of the fleet is unpriced."

Rates are USD per million tokens and go stale. Check them against
platform.openai.com/pricing before quoting a number to anyone.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# USD per 1M tokens, (input, output). Checked 2026-08-20.
# A model absent from this table is reported as unpriced, never as free.
MODEL_RATES: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "o3": (2.00, 8.00),
    "o4-mini": (1.10, 4.40),
}

# DataForSEO SERP Google Organic, live/advanced. Their pricing is per call and
# varies by plan, so this is a default rather than a fact about your account.
SERP_CALL_USD = 0.0025


def rate_for(model: str) -> tuple[float, float] | None:
    """Rates for a model id, tolerating dated snapshots like `gpt-4o-2024-11-20`."""
    if model in MODEL_RATES:
        return MODEL_RATES[model]
    # Longest matching prefix wins, so `gpt-4o-mini-...` does not match `gpt-4o`.
    matches = [name for name in MODEL_RATES if model.startswith(name)]
    if not matches:
        return None
    return MODEL_RATES[max(matches, key=len)]


@dataclass(slots=True)
class RunCost:
    """One run's spend, with the part that could not be priced kept visible."""

    llm_usd: float = 0.0
    serp_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_calls: int = 0
    serp_calls: int = 0
    # Models seen with no rate. Their tokens are counted, their money is not.
    unpriced_models: list[str] = field(default_factory=list)
    unpriced_tokens: int = 0

    @property
    def total_usd(self) -> float:
        return self.llm_usd + self.serp_usd

    @property
    def fully_priced(self) -> bool:
        return not self.unpriced_models

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def cost_of(stats: dict | None) -> RunCost:
    """Price one run from its stored `Run.stats`. Pure, and tolerant of old rows.

    Runs recorded before per-model tokens were stored have no `by_model` block.
    Their tokens are still counted, but they are reported unpriced rather than
    guessed at from whichever model happens to be configured today.
    """
    cost = RunCost()
    stats = stats or {}

    llm = stats.get("llm") or {}
    cost.prompt_tokens = int(llm.get("prompt_tokens") or 0)
    cost.completion_tokens = int(llm.get("completion_tokens") or 0)
    cost.llm_calls = sum(int(n) for n in (llm.get("calls") or {}).values())

    by_model = llm.get("by_model") or {}
    if not by_model and cost.total_tokens:
        cost.unpriced_models = ["(not recorded)"]
        cost.unpriced_tokens = cost.total_tokens
    for model, counts in by_model.items():
        prompt = int(counts.get("prompt") or 0)
        completion = int(counts.get("completion") or 0)
        rates = rate_for(model)
        if rates is None:
            cost.unpriced_models.append(model)
            cost.unpriced_tokens += prompt + completion
            continue
        input_rate, output_rate = rates
        cost.llm_usd += (prompt / 1_000_000) * input_rate
        cost.llm_usd += (completion / 1_000_000) * output_rate

    cost.serp_calls = int(stats.get("serp_calls") or 0)
    cost.serp_usd = cost.serp_calls * SERP_CALL_USD

    return cost


@dataclass(slots=True)
class CostTotals:
    """Aggregate across runs, keeping the unpriced share visible."""

    runs: int = 0
    priced_runs: int = 0
    llm_usd: float = 0.0
    serp_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_calls: int = 0
    serp_calls: int = 0
    unpriced_models: set[str] = field(default_factory=set)

    @property
    def total_usd(self) -> float:
        return self.llm_usd + self.serp_usd

    @property
    def cost_per_run(self) -> float | None:
        """Averaged over priced runs only.

        Dividing by every run would report a per-run cost wrong by however much of
        the fleet could not be priced — geo-tracker's rule, and the reason its
        admin page distinguishes the two counts at all.
        """
        return self.total_usd / self.priced_runs if self.priced_runs else None


def totals_of(costs: list[RunCost]) -> CostTotals:
    totals = CostTotals(runs=len(costs))
    for cost in costs:
        totals.llm_usd += cost.llm_usd
        totals.serp_usd += cost.serp_usd
        totals.prompt_tokens += cost.prompt_tokens
        totals.completion_tokens += cost.completion_tokens
        totals.llm_calls += cost.llm_calls
        totals.serp_calls += cost.serp_calls
        totals.unpriced_models.update(cost.unpriced_models)
        if cost.fully_priced:
            totals.priced_runs += 1
    return totals


def usd(value: float | None, digits: int = 2) -> str:
    """Four decimals where it matters: a single triage call costs a fraction of a
    cent, and rounding to 2 shows every small run as $0.00."""
    if value is None:
        return "—"
    return f"${value:,.{digits}f}"
