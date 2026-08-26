"""Pacing a crawl to the rate a site asked for.

`recon.py` has parsed `Crawl-delay` and the `Disallow` list since the beginning
and used neither: both were read, formatted into one summary line for the
operator, and then ignored by the crawler. So the tool asked a site how it wanted
to be crawled, wrote the answer down, and crawled at full speed anyway.

That is a correctness problem before it is an etiquette one. Measured on
nrma.com.au, whose robots.txt is a stock Drupal 7 file with one line added:

    User-agent: *
    Crawl-delay: 10

Eight concurrent fetchers against a site asking for one request every ten
seconds is 80x its stated rate. What comes back is throttling, 429s and 403s --
which the ladder in `fetch.py` reads as "try harder" and escalates to a browser,
making the load worse. The site was never blocking us; we were being asked to
slow down and did not.

It is also the one place this tool cannot afford to be inconsistent. Its whole
product is advising clients on what their robots.txt should say to AI crawlers.
A crawler that ignores robots.txt while generating robots.txt guidance is not a
position that survives a client asking about it.

**Delay applies per host, not per request in flight.** `Crawl-delay` is a
statement about how often a host is willing to be hit, so the gate is a single
lock and a timestamp rather than a semaphore: concurrency is irrelevant if every
request still has to wait its turn at the door.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

__all__ = ["Politeness", "path_is_allowed", "split_disallowed"]

#: Above this we stop treating the number as a request and start treating it as a
#: misconfiguration. Some sites ship `Crawl-delay: 3600` from a template; honouring
#: it literally would mean one page an hour and a crawl that never finishes.
#: Capped rather than ignored, and the cap is reported.
MAX_DELAY_SECONDS = 30.0


def _match_length(path: str, rule: str) -> int:
    """How many characters of `rule` match `path`, or -1 for no match.

    Length is what decides between a conflicting `Allow` and `Disallow`, so the
    matcher has to return it rather than a bool. `*` and `$` are honoured, which
    is what every major crawler implements and what real files are written
    against.
    """
    rule = rule.strip()
    if not rule:
        # `Disallow:` with no value is the documented way to say "allow all".
        return -1
    if rule.endswith("$"):
        return len(rule) - 1 if fnmatch.fnmatchcase(path, rule[:-1]) else -1
    if "*" in rule:
        pattern = rule if rule.endswith("*") else rule + "*"
        return len(rule) if fnmatch.fnmatchcase(path, pattern) else -1
    return len(rule) if path.startswith(rule) else -1


def path_is_allowed(path: str, disallowed: list[str], allowed: list[str] | None = None) -> bool:
    """Whether `path` may be fetched, given the `*` group's rules.

    **Longest match wins, and a tie goes to `Allow`.** That is the rule every
    major crawler implements, and `crawl_rules.py` already states it -- but the
    first version of this function read `Disallow` only, so a site that blocks a
    directory and re-permits one path inside it (`Disallow: /blog/` with
    `Allow: /blog/public/`) had the permitted path silently skipped. Silently
    dropping pages a client explicitly allowed is the exact failure the rest of
    this module is built to avoid.
    """
    path = path or "/"
    block = max((_match_length(path, rule) for rule in disallowed), default=-1)
    if block < 0:
        return True
    permit = max((_match_length(path, rule) for rule in (allowed or [])), default=-1)
    return permit >= block


@dataclass
class Politeness:
    """One host's stated crawl rate, enforced.

    `delay` of 0 means no `Crawl-delay` was published, and `wait()` is then a
    no-op that costs an uncontended lock acquire -- so this can sit in the fetch
    path unconditionally rather than being wired in only when it applies.
    """

    delay: float = 0.0
    #: Set when `Crawl-delay` exceeded `MAX_DELAY_SECONDS`, so the operator can be
    #: told we are deliberately not honouring the published number in full.
    capped_from: float | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _next_at: float = field(default=0.0, repr=False)

    @classmethod
    def from_robots(cls, crawl_delay: float | None) -> Politeness:
        if not crawl_delay or crawl_delay <= 0:
            return cls()
        if crawl_delay > MAX_DELAY_SECONDS:
            logger.info("crawl-delay %.0fs capped to %.0fs", crawl_delay, MAX_DELAY_SECONDS)
            return cls(delay=MAX_DELAY_SECONDS, capped_from=crawl_delay)
        return cls(delay=float(crawl_delay))

    @property
    def applies(self) -> bool:
        return self.delay > 0

    def estimate_seconds(self, pages: int) -> float:
        """How long `pages` will take at this rate, ignoring fetch time itself.

        The floor, not the estimate: a crawl cannot be faster than this, and for a
        site with a delay it dominates everything else.
        """
        return max(0, pages - 1) * self.delay

    async def wait(self) -> None:
        """Block until this host may be hit again.

        The lock is held only long enough to claim the next slot, not for the
        sleep itself -- holding it across the sleep would serialise every waiter
        behind the whole queue rather than letting each take its own turn.
        """
        if not self.applies:
            return
        async with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_at)
            self._next_at = slot + self.delay
        remaining = slot - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)


def split_disallowed(
    urls: list[str], disallowed: list[str], allowed: list[str] | None = None
) -> tuple[list[str], dict[str, int]]:
    """Partition a crawl list by the site's own `Disallow` rules.

    Shaped after `split_embargoed`, and reported the same way: the survivors plus
    a count per rule, so an operator can answer "why is this page missing"
    without a debugging session. A URL dropped silently is indistinguishable from
    one the crawler failed on.

    These are the site's rules about itself, so a page removed here is one the
    client has said should not be indexed -- which is also a page that has no
    business appearing in an llms.txt written for them.
    """
    if not disallowed:
        return urls, {}

    kept: list[str] = []
    counts: dict[str, int] = {}
    for url in urls:
        path = urlparse(url).path or "/"
        if path_is_allowed(path, disallowed, allowed):
            kept.append(url)
            continue
        # Name the rule that actually did it -- the longest match, which is the
        # one that decided. Reporting the first rule in file order would name a
        # weaker rule that an `Allow` may have already overridden.
        hit = max(disallowed, key=lambda rule: _match_length(path, rule))
        counts[hit] = counts.get(hit, 0) + 1
    return kept, counts
