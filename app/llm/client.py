"""One OpenAI client, four stages, a token budget that fits each of them.

The single most consequential defect in the source tool is here. It set
`max_tokens=100` once, globally, and passed it to every call -- including the one
that asked for a semantic section grouping of up to 80 pages. That response cannot
fit in 100 tokens, so it truncated, `json.loads` raised, and the code fell back to
URL-path grouping inside a bare `except`. The headline "AI semantic grouping"
feature has therefore never once produced output. Nothing in the UI said so.

Two rules follow, and they are the reason this module exists:

1. Every stage declares its own budget, sized to what it is actually asked to
   return.
2. A fallback is logged, counted and surfaced on the run. Degrading to heuristics
   is fine; degrading silently is not.

Structured outputs carry the schema, so the prompt carries context and quality
guidance only -- the convention geo-tracker uses in
`packages/lib/src/onboarding/analyze.ts`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.config import Settings

logger = logging.getLogger(__name__)


class Stage(StrEnum):
    PLAN = "plan"
    TRIAGE = "triage"
    SUMMARISE = "summarise"
    QA = "qa"


@dataclass(frozen=True, slots=True)
class StageBudget:
    """Sized to the output, not guessed.

    `plan` returns one rule per URL template and there can be 60 of them; `triage`
    returns three short fields per page over a batch of 40; `summarise` returns a
    title and a description per page; `qa` returns prose findings.
    """

    max_tokens: int
    temperature: float


BUDGETS: dict[Stage, StageBudget] = {
    Stage.PLAN: StageBudget(max_tokens=6_000, temperature=0.1),
    Stage.TRIAGE: StageBudget(max_tokens=4_000, temperature=0.0),
    Stage.SUMMARISE: StageBudget(max_tokens=3_000, temperature=0.3),
    Stage.QA: StageBudget(max_tokens=2_000, temperature=0.1),
}


@dataclass
class LLMUsage:
    """What the run spent, per stage, and where it gave up and fell back."""

    calls: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    fallbacks: list[str] = field(default_factory=list)

    def record(self, stage: Stage, prompt: int, completion: int) -> None:
        self.calls[stage] = self.calls.get(stage, 0) + 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion

    def record_fallback(self, stage: Stage, reason: str) -> None:
        message = f"{stage}: {reason}"
        self.fallbacks.append(message)
        # Loud on purpose. The source swallowed exactly this into a bare except.
        logger.warning("LLM stage fell back to the heuristic path -- %s", message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": dict(self.calls),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "fallbacks": list(self.fallbacks),
        }


class LLMUnavailable(RuntimeError):
    """Raised only where a caller has asked for a hard failure instead of a fallback."""


class LLMClient:
    """Async OpenAI wrapper that returns validated objects or None.

    None means "use the heuristic path", and it is always accompanied by a recorded
    fallback. Callers never see a raw exception from a model call.
    """

    def __init__(self, settings: Settings, usage: LLMUsage | None = None) -> None:
        self.settings = settings
        self.usage = usage or LLMUsage()
        self._client: Any = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.openai_api_key)

    def model_for(self, stage: Stage) -> str:
        return {
            Stage.PLAN: self.settings.llm_model_plan,
            Stage.TRIAGE: self.settings.llm_model_triage,
            Stage.SUMMARISE: self.settings.llm_model_summarise,
            Stage.QA: self.settings.llm_model_qa,
        }[stage]

    def _ensure_client(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI

            kwargs: dict[str, Any] = {"api_key": self.settings.openai_api_key}
            if self.settings.openai_base_url:
                kwargs["base_url"] = self.settings.openai_base_url
            self._client = AsyncOpenAI(**kwargs)
        return self._client

    async def structured(
        self,
        stage: Stage,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
    ) -> dict[str, Any] | None:
        """One structured call. Returns the validated object, or None on any failure."""
        if not self.enabled:
            self.usage.record_fallback(stage, "no OPENAI_API_KEY configured")
            return None

        budget = BUDGETS[stage]
        client = self._ensure_client()

        try:
            response = await client.chat.completions.create(
                model=self.model_for(stage),
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "schema": schema, "strict": True},
                },
                max_completion_tokens=budget.max_tokens,
                temperature=budget.temperature,
            )
        except Exception as exc:
            self.usage.record_fallback(stage, f"{type(exc).__name__}: {exc}")
            return None

        choice = response.choices[0]
        if usage := getattr(response, "usage", None):
            self.usage.record(stage, usage.prompt_tokens or 0, usage.completion_tokens or 0)

        # The specific failure the source could not see. `length` means the budget
        # was too small for what was asked, and the JSON is truncated -- so say that,
        # rather than reporting a parse error and leaving the cause unexplained.
        if choice.finish_reason == "length":
            self.usage.record_fallback(
                stage,
                f"response hit the {budget.max_tokens}-token budget and was truncated",
            )
            return None
        if choice.finish_reason == "content_filter":
            self.usage.record_fallback(stage, "response was filtered by the provider")
            return None

        content = choice.message.content or ""
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            self.usage.record_fallback(stage, f"invalid JSON: {exc}")
            return None

        if not isinstance(parsed, dict):
            self.usage.record_fallback(stage, "expected a JSON object")
            return None
        return parsed
