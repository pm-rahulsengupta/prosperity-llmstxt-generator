"""The token bucket, on an injected clock so nothing sleeps."""

from __future__ import annotations

from app.core.throttle import TokenBucket


def test_a_burst_is_allowed_then_refused():
    bucket = TokenBucket(rate_per_minute=60, burst=3)

    allowed = [bucket.allow("k", now=0.0) for _ in range(5)]

    assert allowed == [True, True, True, False, False]


def test_it_refills_on_the_clock_it_is_given():
    """Injected rather than read, so this cannot go flaky on a slow machine."""
    bucket = TokenBucket(rate_per_minute=60, burst=2)
    bucket.allow("k", now=0.0)
    bucket.allow("k", now=0.0)
    assert bucket.allow("k", now=0.0) is False

    assert bucket.allow("k", now=1.0) is True, "one token a second at 60/min"


def test_keys_do_not_share_a_budget():
    """Keyed on the token: one client's traffic must not close another's link."""
    bucket = TokenBucket(rate_per_minute=60, burst=1)
    bucket.allow("a", now=0.0)

    assert bucket.allow("b", now=0.0) is True


def test_the_key_table_stays_bounded():
    """A flood of distinct tokens must not grow the process's memory."""
    bucket = TokenBucket(rate_per_minute=60, burst=1, max_keys=100)

    for n in range(5_000):
        bucket.allow(f"token-{n}", now=float(n))

    assert len(bucket._seen) <= 100


def test_eviction_drops_the_least_recently_seen():
    bucket = TokenBucket(rate_per_minute=60, burst=1, max_keys=2)
    bucket.allow("old", now=0.0)
    bucket.allow("mid", now=1.0)
    bucket.allow("old", now=2.0)  # touched, so "mid" is now the stale one

    bucket.allow("new", now=3.0)

    assert set(bucket._seen) == {"old", "new"}


def test_it_never_hands_back_more_than_the_burst():
    """A key idle for a week must not arrive with a week of credit."""
    bucket = TokenBucket(rate_per_minute=60, burst=5)
    bucket.allow("k", now=0.0)

    allowed = [bucket.allow("k", now=604_800.0) for _ in range(7)]

    assert allowed.count(True) == 5
