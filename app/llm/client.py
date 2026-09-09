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

import asyncio
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
    CHAT = "chat"


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
    # An edit turn returns a handful of operations and one line of prose. The
    # budget is for a user asking to rewrite every description at once.
    Stage.CHAT: StageBudget(max_tokens=4_000, temperature=0.2),
}


@dataclass
class LLMUsage:
    """What the run spent, per stage, and where it gave up and fell back."""

    calls: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    fallbacks: list[str] = field(default_factory=list)
    # Tokens broken down by the model that actually served the call. Costing needs
    # this: the configured model can change between a run and the day someone adds
    # up what it cost, and a stage-name-to-model guess made later would be wrong.
    by_model: dict[str, dict[str, int]] = field(default_factory=dict)

    def record(self, stage: Stage, prompt: int, completion: int, model: str = "") -> None:
        self.calls[stage] = self.calls.get(stage, 0) + 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        if model:
            entry = self.by_model.setdefault(model, {"calls": 0, "prompt": 0, "completion": 0})
            entry["calls"] += 1
            entry["prompt"] += prompt
            entry["completion"] += completion

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
            "by_model": {model: dict(counts) for model, counts in self.by_model.items()},
        }


def _unfence(content: str) -> str:
    """Strip a markdown code fence the model wrapped its JSON in.

    `response_format={"type": "json_schema", ..., "strict": True}` is an OpenAI
    feature, and an OpenAI-compatible gateway is free to accept the parameter and
    then ignore it. Measured on 2026-09-09 against the Prosperity OmniRoute
    gateway: `kr/claude-sonnet-4.5`, `kr/glm-5`, `kr/deepseek-3.2` and
    `kr/qwen3-coder-next` all returned schema-correct JSON wrapped in a ```json
    fence, `json.loads` raised on the first backtick, and every stage recorded
    "invalid JSON" and fell back to the heuristic path. The answer was there;
    three characters of packaging stood between it and being used.

    That is the failure this module exists to stop -- a stage that has never once
    produced output while nothing says so -- arriving through a different door, so
    it is fixed here rather than by forbidding those providers.

    Deliberately narrow, and written without a regex because the cases that matter
    are positional. The fence must open the string and close it, so a fence inside
    a JSON *value* -- a QA finding quoting a code block -- is untouched, and an
    unfenced response is returned unchanged. If what is left still is not JSON, the
    existing decode error stands and the fallback is recorded exactly as before.
    """
    text = content.strip()
    if not text.startswith("```") or not text.endswith("```") or len(text) < 6:
        return content

    # Everything up to the first newline is the fence's info string ("json", "" ...).
    # A fence with no newline at all is not wrapping a document; leave it be.
    newline = text.find("\n")
    if newline == -1:
        return content
    if not text[3:newline].strip().isascii() or any(c in text[3:newline] for c in "`\"'"):
        return content

    return text[newline + 1 : -3].strip("\r\n").rstrip()


#: A rate limit is a request to wait, not a refusal, so it is the one failure worth
#: asking again about. Three attempts and eight seconds of total sleep: the redspot
#: run's 429s carried "reset after 5s", and a summarise batch that waits six seconds
#: is cheaper than 25 pages falling to URL-slug descriptions in a client's file.
#: Bounded rather than exponential-to-exhaustion because the worker runs one job at
#: a time and a stuck stage blocks the queue.
RATE_LIMIT_BACKOFF = (6, 12)
RATE_LIMIT_ATTEMPTS = len(RATE_LIMIT_BACKOFF) + 1


def _is_rate_limit(exc: Exception) -> bool:
    """Whether the provider asked us to slow down, rather than refusing outright.

    Matched on the status code where the SDK exposes one, and on the text where it
    does not: an OpenAI-compatible gateway may raise its own class, and OmniRoute
    reports a 429 inside a message body -- "[kiro/claude-sonnet-4.5] [429]: Too
    many requests, please wait before trying again. (reset after 5s)". Four
    summarise batches on the redspot run ended there, so a hundred pages took
    heuristic copy while the provider was telling us exactly how long to wait.

    Deliberately not matched on "quota" or "billing". An exhausted account returns
    429 too and retrying it three times only spends the same failure three times;
    that is the `insufficient_quota` case, which must fall back at once.
    """
    if getattr(exc, "status_code", None) == 429:
        text = str(exc).lower()
        return "insufficient_quota" not in text and "credit balance" not in text
    text = str(exc).lower()
    if "insufficient_quota" in text or "credit balance" in text:
        return False
    return "429" in text or "rate limit" in text or "too many requests" in text


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
            Stage.CHAT: self.settings.llm_model_chat,
        }[stage]

    def _ensure_client(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI

            kwargs: dict[str, Any] = {"api_key": self.settings.openai_api_key}
            if self.settings.openai_base_url:
                kwargs["base_url"] = self.settings.openai_base_url
            self._client = AsyncOpenAI(**kwargs)
        return self._client

    @property
    def schema_is_enforced(self) -> bool:
        """Whether `response_format` can be relied on to shape the reply.

        True only against OpenAI itself. Strict `json_schema` is OpenAI's feature
        and an OpenAI-*compatible* endpoint is free to accept the parameter and
        drop it, which is not a refusal anyone can see: the request succeeds, the
        usage is billed, and the reply is whatever the model felt like returning.

        Measured 2026-09-09 through the Prosperity OmniRoute gateway, same model,
        same call, three different shapes -- fenced JSON, an empty string, and, for
        the group-intent prompt, four paragraphs of English beginning
        "**Classification: hub**". One preflight recorded a plan built from a real
        model answer and an intent stage that had fallen back, from two calls a
        second apart.

        `openai_base_url` is the only signal available at this layer, and it is a
        sound one: it is unset for OpenAI and set for everything else.
        """
        return not self.settings.openai_base_url

    def _system_for(self, system: str, schema: dict[str, Any], schema_name: str) -> str:
        """The system prompt, plus the schema when the endpoint will not enforce it.

        This module's rule is that "structured outputs carry the schema, so the
        prompt carries context and quality guidance only". That rule assumes the
        schema is carried by something. Where it is not, the choice is between
        putting it in the prompt and losing every stage to the heuristic path, and
        the prompt is plainly the better of the two.

        Left untouched on the OpenAI path, so the calls this tool was measured
        against go out byte-for-byte as before and keep the guarantee that makes
        the extra instruction unnecessary there.
        """
        if self.schema_is_enforced:
            return system
        return (
            f"{system}\n\n"
            "---\n\n"
            "Reply with a single JSON object and nothing else. No prose before or "
            "after it, no markdown code fence, no explanation of your reasoning. "
            f"It must validate against this JSON Schema named {schema_name!r}:\n\n"
            f"{json.dumps(schema)}"
        )

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

        request = {
            "model": self.model_for(stage),
            "messages": [
                {"role": "system", "content": self._system_for(system, schema, schema_name)},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema, "strict": True},
            },
            "max_completion_tokens": budget.max_tokens,
            "temperature": budget.temperature,
        }

        response = None
        for attempt in range(RATE_LIMIT_ATTEMPTS):
            try:
                response = await client.chat.completions.create(**request)
                break
            except Exception as exc:
                last = attempt == RATE_LIMIT_ATTEMPTS - 1
                if last or not _is_rate_limit(exc):
                    self.usage.record_fallback(stage, f"{type(exc).__name__}: {exc}")
                    return None
                delay = RATE_LIMIT_BACKOFF[attempt]
                logger.info(
                    "%s rate-limited, retrying in %ss (attempt %d of %d)",
                    stage,
                    delay,
                    attempt + 2,
                    RATE_LIMIT_ATTEMPTS,
                )
                await asyncio.sleep(delay)

        if response is None:  # pragma: no cover - the loop returns or breaks
            return None

        choice = response.choices[0]
        if usage := getattr(response, "usage", None):
            self.usage.record(
                stage,
                usage.prompt_tokens or 0,
                usage.completion_tokens or 0,
                # The model the API says served it, not the one we asked for --
                # an alias like `gpt-4o` resolves to a dated snapshot.
                model=getattr(response, "model", "") or self.model_for(stage),
            )

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

        content = _unfence(choice.message.content or "")

        # An empty body is not a parse failure and must not be reported as one. It
        # reads as "invalid JSON: Expecting value: line 1 column 1 (char 0)", which
        # sends a reader looking for malformed JSON that was never there -- one of
        # the redspot preflight's two calls returned exactly this, and the message
        # is why it took a reproduction to find out what had happened.
        if not content.strip():
            self.usage.record_fallback(
                stage,
                f"{self.model_for(stage)} returned an empty response "
                f"(finish_reason={choice.finish_reason!r})",
            )
            return None

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            # The prose case, distinguished from a truncated or malformed object.
            # A gateway that drops `response_format` answers the question in
            # English, and "invalid JSON" describes that badly enough to hide it.
            opener = content.strip()[:80].replace("\n", " ")
            self.usage.record_fallback(stage, f"invalid JSON: {exc} -- response began {opener!r}")
            return None

        if not isinstance(parsed, dict):
            self.usage.record_fallback(stage, "expected a JSON object")
            return None
        return parsed
