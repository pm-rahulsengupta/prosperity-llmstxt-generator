"""A token bucket, for bounding work rather than for stopping attackers.

**It buys no guessing resistance and is not there for any.** A share token is 256
random bits; at ten thousand attempts a second against ten thousand live links,
the expected time to find one exceeds the age of the universe by a wide margin.
Anyone proposing a rate limit as a defence against guessing here is misapplying
the control, and this docstring exists so that this does not get "hardened"
repeatedly by people who did not read the token module.

What it does buy is real: **bounded CPU**. Rendering a share page runs
`_from_snapshot` -> `_assemble` -> `_refined` -> `_derive_state` -> `reports_for`
on every view, uncached, and the combined report is the biggest of them. A leaked
link, a client's uptime monitor, a Slack unfurl bot or a mail-security scanner
stuck in a retry loop can degrade the staff app for everyone. Railway's edge does
no rate limiting, so there is nothing upstream to lean on.

Keyed on the token, never on the IP. `app/web.py` runs uvicorn with
`forwarded_allow_ips="*"`, which makes `X-Forwarded-For` client-controlled: an
IP-keyed limiter would be trivially evaded by the traffic worth limiting, and
weaponisable by anyone who wanted to lock a real client out of their own audit.

In-process, not database-backed. On a multi-process web service each process
keeps its own bucket, so the effective ceiling is N x `rate_per_minute`. That is
correct rather than a compromise: the thing being bounded is per-process render
cost, and a per-process bound bounds it. A shared counter would add a database
write to every page view in order to defend against page views.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["TokenBucket"]


@dataclass
class TokenBucket:
    """Refills continuously; `allow` is the only method that moves the clock.

    The clock is injected rather than read, so the tests measure refill without
    sleeping and cannot go flaky on a slow machine.
    """

    rate_per_minute: float = 60.0
    burst: float = 20.0
    #: Bounded so a flood of distinct tokens cannot grow the process's memory.
    #: Least-recently-seen keys are dropped, which at worst forgives a caller who
    #: has not been seen since the last eviction.
    max_keys: int = 10_000
    _seen: dict[str, tuple[float, float]] = field(default_factory=dict, repr=False)

    def allow(self, key: str, *, now: float) -> bool:
        tokens, last = self._seen.pop(key, (self.burst, now))
        tokens = min(self.burst, tokens + (now - last) * self.rate_per_minute / 60.0)

        permitted = tokens >= 1.0
        if permitted:
            tokens -= 1.0

        # Re-inserting after the pop puts the key at the end of the dict, which
        # is what makes the eviction below least-recently-seen rather than
        # least-recently-created.
        self._seen[key] = (tokens, now)
        while len(self._seen) > self.max_keys:
            self._seen.pop(next(iter(self._seen)))
        return permitted
