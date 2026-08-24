"""CRW-001..009 — the rules that judge a robots.txt.

These judge a file we did not write. `render_robots` produces an *addition* the
operator pastes into a robots.txt that already exists and already carries rules
nobody here has seen — its docstring is explicit that replacing it wholesale is
"how a tool takes a site out of Google". So the interesting failures are all
properties of the merged result, and the generator cannot see the merge.

That gives this set a job the others do not have: **checking work after a human
touched it.**

The rule that justifies the file is CRW-001. Every branch of `render_robots` is
internally consistent by construction — `BLOCK_ALL` emits `Disallow: /` for every
bot *and* `search=no`. A merged file is under no such discipline, and a
`Content-Signal` that says `search=yes` above a `Disallow: /` for OAI-SearchBot
is a site whose owner believes they have opted into AI search and has not. That
contradiction is silent, common, and invisible to every other check we run.

Two things this set will not do.

**It does not require our block to be present.** A client may have written their
own rules, correctly, before we arrived. A validator that failed them for not
using our wording would be checking authorship rather than outcome.

**It does not treat a missing bot as a failure.** `robots.txt` is deny-by-
exception: a bot with no rule is allowed by the catch-all. Absence of a
`GPTBot` block on a site that wants to allow training is correct, and marking it
down would push operators toward writing rules they do not mean.
"""

from __future__ import annotations

import re

from app.core.rules.registry import Category, Rule, Severity, fail, ok, skip

# The agents this tool knows how to reason about. Drawn from `app/core/bundle.py`
# rather than restated, so a bot added there is understood here.
try:  # pragma: no cover - exercised by the import, not by a branch
    from app.core.bundle import SEARCH_BOTS, TRAINING_BOTS
except ImportError:  # pragma: no cover
    SEARCH_BOTS = ("OAI-SearchBot", "PerplexityBot", "ClaudeBot", "Google-Extended")
    TRAINING_BOTS = ("GPTBot", "CCBot", "anthropic-ai")

KNOWN_AI_BOTS = tuple(SEARCH_BOTS) + tuple(TRAINING_BOTS)

# Bots that carry a site's ordinary search visibility. Blocking one of these by
# accident is a different and larger problem than an AI policy mistake.
SEARCH_ENGINE_BOTS = ("Googlebot", "Bingbot", "DuckDuckBot", "Applebot")

USER_AGENT = re.compile(r"^\s*user-agent\s*:\s*(.+?)\s*$", re.I | re.M)
DIRECTIVE = re.compile(r"^\s*(allow|disallow)\s*:\s*(.*?)\s*$", re.I | re.M)
CONTENT_SIGNAL = re.compile(r"^\s*content-signal\s*:\s*(.+?)\s*$", re.I | re.M)
SITEMAP = re.compile(r"^\s*sitemap\s*:\s*(.+?)\s*$", re.I | re.M)

SIGNAL_KEYS = ("ai-train", "search", "ai-input")


class CrawlContext:
    """A robots.txt and what we know about the policy it should express.

    Its own class rather than more fields on `RuleContext`, following
    `AgentsContext`: these rules read a flat file, not a parsed document, and
    nothing in `RuleContext` can hold one.

    `intended_policy` is what the operator said in onboarding. `None` means we
    were not told, and every rule that compares stated intent against the file
    skips rather than guessing which of the two is wrong.
    """

    __slots__ = ("fetched", "intended_policy", "site_url", "text")

    def __init__(
        self,
        text: str = "",
        *,
        intended_policy: str | None = None,
        site_url: str = "",
        fetched: bool = False,
    ) -> None:
        self.text = text
        self.intended_policy = intended_policy
        self.site_url = site_url
        # Whether this is the live file or something we generated. A generated
        # block is not expected to carry the catch-all or the sitemap.
        self.fetched = fetched


def _groups(text: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """Split into (user-agent, [(directive, value)]) groups, in file order.

    A group runs from a `User-agent:` line to the next one. Consecutive
    `User-agent:` lines share the directives that follow, which is the one piece
    of robots.txt syntax people most often get wrong when merging by hand.
    """
    collected: dict[str, list[tuple[str, str]]] = {}
    pending: list[str] = []
    seen_directive = False

    for line in text.splitlines():
        if (agent := USER_AGENT.match(line)) is not None:
            # A User-agent after directives starts a new group. Before any, it
            # joins the current one -- consecutive agent lines share what follows,
            # and that is the piece of the syntax hand-merging gets wrong.
            if seen_directive:
                pending = []
                seen_directive = False
            pending.append(agent.group(1))
            continue

        if (rule := DIRECTIVE.match(line)) is not None and pending:
            seen_directive = True
            for name in pending:
                collected.setdefault(name, []).append((rule.group(1).lower(), rule.group(2)))

    return list(collected.items())


def _blocks_everything(directives: list[tuple[str, str]]) -> bool:
    """Whether a group denies the whole site.

    `Allow` wins over `Disallow` for the same path in every major
    implementation, so a group carrying both is not a block.
    """
    disallows_root = any(name == "disallow" and value == "/" for name, value in directives)
    allows_root = any(name == "allow" and value in ("/", "") for name, value in directives)
    return disallows_root and not allows_root


def _signal(ctx: CrawlContext) -> dict[str, str]:
    match = CONTENT_SIGNAL.search(ctx.text)
    if match is None:
        return {}
    parsed: dict[str, str] = {}
    for part in match.group(1).split(","):
        if "=" in part:
            key, _, value = part.partition("=")
            parsed[key.strip().lower()] = value.strip().lower()
    return parsed


# -- the rules ---------------------------------------------------------------


def _signal_agrees_with_rules(ctx: CrawlContext):
    """CRW-001. The check nothing else in this tool can make.

    A `Content-Signal` is a statement of intent. The `Disallow` lines are what
    actually happens. When they disagree, the site owner believes one thing and
    the crawlers do another, and nobody finds out until the traffic does not
    arrive.
    """
    signal = _signal(ctx)
    if not signal:
        return skip("CRW-001", "no Content-Signal line, so there is nothing to contradict")

    groups = dict(_groups(ctx.text))
    conflicts: list[str] = []

    if signal.get("search") == "yes":
        for bot in SEARCH_BOTS:
            directives = groups.get(bot)
            if directives and _blocks_everything(directives):
                conflicts.append(f"search=yes but {bot} is disallowed from the whole site")

    if signal.get("ai-train") == "yes":
        for bot in TRAINING_BOTS:
            directives = groups.get(bot)
            if directives and _blocks_everything(directives):
                conflicts.append(f"ai-train=yes but {bot} is disallowed from the whole site")

    if not conflicts:
        return ok("CRW-001", "the stated signal and the crawl rules agree")
    return fail(
        "CRW-001",
        "Content-Signal contradicts the crawl rules. The stated policy is not the "
        "one that will be enforced, and nothing else reports this.",
        count=len(conflicts),
        examples=conflicts,
    )


def _signal_is_complete(ctx: CrawlContext):
    """CRW-002. All three directives, or a reader has to guess the rest."""
    signal = _signal(ctx)
    if not signal:
        return skip("CRW-002", "no Content-Signal line to check")

    missing = [key for key in SIGNAL_KEYS if key not in signal]
    if missing:
        return fail(
            "CRW-002",
            f"Content-Signal omits {', '.join(missing)}. A partial signal leaves the "
            "unstated permissions undefined rather than denied.",
            count=len(missing),
            examples=missing,
        )

    bad = [f"{k}={v}" for k, v in signal.items() if k in SIGNAL_KEYS and v not in ("yes", "no")]
    if bad:
        return fail("CRW-002", "Content-Signal values must be yes or no.", examples=bad)
    return ok("CRW-002", "all three directives present and well-formed")


def _one_signal_only(ctx: CrawlContext):
    """CRW-003. Two lines means one of them is being ignored, silently."""
    found = CONTENT_SIGNAL.findall(ctx.text)
    if len(found) <= 1:
        return ok("CRW-003")
    return fail(
        "CRW-003",
        f"{len(found)} Content-Signal lines. Only one applies and which one is "
        "implementation-defined, so the policy is whatever the reader decides.",
        count=len(found),
        examples=found,
    )


def _every_group_has_a_rule(ctx: CrawlContext):
    """CRW-004. A `User-agent:` with no directive under it does nothing at all."""
    named = [m.group(1) for m in USER_AGENT.finditer(ctx.text)]
    if not named:
        return skip("CRW-004", "no user-agent groups in this file")

    groups = dict(_groups(ctx.text))
    empty = [name for name in named if not groups.get(name)]
    if not empty:
        return ok("CRW-004", f"{len(named)} group(s), each carrying at least one rule")
    return fail(
        "CRW-004",
        "A User-agent line with no Allow or Disallow beneath it is inert. The bot "
        "falls through to the catch-all, which is rarely what was intended.",
        count=len(empty),
        examples=empty,
    )


def _no_unknown_ai_agents(ctx: CrawlContext):
    """CRW-005. A typo'd bot name matches nothing and fails silently.

    Only advisory: a client may legitimately name crawlers this tool has never
    heard of, and failing them for that would be checking our vocabulary rather
    than their file.
    """
    named = {m.group(1) for m in USER_AGENT.finditer(ctx.text)}
    known = {b.lower() for b in KNOWN_AI_BOTS} | {b.lower() for b in SEARCH_ENGINE_BOTS} | {"*"}

    suspicious = [
        name
        for name in named
        if name.lower() not in known
        and any(token in name.lower() for token in ("gpt", "ai", "bot", "claude", "perplexity"))
    ]
    if not suspicious:
        return ok("CRW-005")
    return fail(
        "CRW-005",
        "Agent name this tool does not recognise. If it is a typo the rule matches "
        "nothing and the bot is allowed by the catch-all.",
        count=len(suspicious),
        examples=suspicious,
    )


def _catch_all_exists(ctx: CrawlContext):
    """CRW-006. Live files only. A generated block is an addition, not a file."""
    if not ctx.fetched:
        return skip("CRW-006", "this is a generated block, not the site's whole robots.txt")
    named = {m.group(1) for m in USER_AGENT.finditer(ctx.text)}
    if "*" in named:
        return ok("CRW-006")
    return fail(
        "CRW-006",
        "No `User-agent: *` group. Every crawler without a rule of its own is "
        "unmanaged, which is a policy nobody chose.",
    )


def _search_engines_not_blocked(ctx: CrawlContext):
    """CRW-007. The expensive accident.

    An AI policy that also blocks Googlebot is not an AI policy, it is an
    outage. Highest severity in the set for that reason.
    """
    if not ctx.fetched:
        return skip("CRW-007", "this is a generated block, not the site's whole robots.txt")

    groups = dict(_groups(ctx.text))
    blocked = [bot for bot in SEARCH_ENGINE_BOTS if _blocks_everything(groups.get(bot, []))]
    catch_all = groups.get("*", [])
    if _blocks_everything(catch_all):
        blocked.append("* (every crawler without its own rule)")

    if not blocked:
        return ok("CRW-007", "ordinary search crawlers are not blocked")
    return fail(
        "CRW-007",
        "Ordinary search crawlers are disallowed from the whole site. If this was "
        "meant as an AI policy it has taken organic search with it.",
        count=len(blocked),
        examples=blocked,
    )


def _sitemap_is_absolute(ctx: CrawlContext):
    """CRW-008. A relative Sitemap line is ignored by every major crawler."""
    found = SITEMAP.findall(ctx.text)
    if not found:
        return skip("CRW-008", "no Sitemap line in this file")

    relative = [url for url in found if not url.lower().startswith(("http://", "https://"))]
    if relative:
        return fail(
            "CRW-008",
            "Sitemap must be an absolute URL. A relative one is discarded rather than resolved.",
            count=len(relative),
            examples=relative,
        )
    return ok("CRW-008", f"{len(found)} sitemap(s), all absolute")


def _policy_matches_intent(ctx: CrawlContext):
    """CRW-009. What the operator said, against what the file does.

    Skips when we were not told the intent, rather than assuming the file is
    right. The two disagreeing is a question for a person, not a verdict.
    """
    if ctx.intended_policy is None:
        return skip("CRW-009", "no stated policy to compare the file against")

    signal = _signal(ctx)
    if not signal:
        return skip("CRW-009", "the file carries no Content-Signal to compare")

    expected = {
        "block_all": {"ai-train": "no", "search": "no"},
        "allow_search_only": {"ai-train": "no", "search": "yes"},
        "allow_all": {"ai-train": "yes", "search": "yes"},
    }.get(ctx.intended_policy)

    if expected is None:
        return skip("CRW-009", f"unrecognised stated policy {ctx.intended_policy!r}")

    wrong = [
        f"{key}: file says {signal.get(key, 'nothing')}, you asked for {value}"
        for key, value in expected.items()
        if signal.get(key) != value
    ]
    if not wrong:
        return ok("CRW-009", "the published policy matches what you asked for")
    return fail(
        "CRW-009",
        "The live file does not express the policy recorded in onboarding.",
        count=len(wrong),
        examples=wrong,
    )


CRAWL_RULES: list[Rule] = [
    Rule(
        "CRW-001",
        "Content-Signal agrees with the crawl rules",
        Category.INDEX,
        Severity.ERROR,
        _signal_agrees_with_rules,
        "A stated policy the rules contradict is a policy the site does not have.",
    ),
    Rule(
        "CRW-002",
        "Content-Signal is complete",
        Category.INDEX,
        Severity.WARNING,
        _signal_is_complete,
        "An omitted directive is undefined, not denied.",
    ),
    Rule(
        "CRW-003",
        "One Content-Signal line",
        Category.INDEX,
        Severity.WARNING,
        _one_signal_only,
        "Which of two applies is implementation-defined.",
    ),
    Rule(
        "CRW-004",
        "Every group carries a rule",
        Category.INDEX,
        Severity.WARNING,
        _every_group_has_a_rule,
        "A User-agent with no directive beneath it does nothing.",
    ),
    Rule(
        "CRW-005",
        "Agent names are recognised",
        Category.INDEX,
        Severity.INFO,
        _no_unknown_ai_agents,
        "A typo'd crawler name matches nothing and fails silently.",
    ),
    Rule(
        "CRW-006",
        "A catch-all group exists",
        Category.INDEX,
        Severity.WARNING,
        _catch_all_exists,
        "Crawlers without a rule of their own should not be unmanaged by accident.",
    ),
    Rule(
        "CRW-007",
        "Search crawlers are not blocked",
        Category.INDEX,
        Severity.ERROR,
        _search_engines_not_blocked,
        "An AI policy that blocks Googlebot is an outage.",
    ),
    Rule(
        "CRW-008",
        "Sitemap is absolute",
        Category.INDEX,
        Severity.WARNING,
        _sitemap_is_absolute,
        "A relative Sitemap line is discarded rather than resolved.",
    ),
    Rule(
        "CRW-009",
        "Published policy matches the brief",
        Category.INDEX,
        Severity.ERROR,
        _policy_matches_intent,
        "The file and the operator's stated intent must not disagree silently.",
    ),
]

CRAWL_BY_ID: dict[str, Rule] = {rule.id: rule for rule in CRAWL_RULES}
