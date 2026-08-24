"""Hosted Lighthouse, for the two checks a static parse cannot settle.

Six components were reported as needing a person because nothing here could
decide them. Two of those name Lighthouse in their own `verify` string --
`cls` says `npx lighthouse <site> --view` and `tap-targets` says
`Lighthouse: "Tap targets are sized appropriately"` -- so the honest thing was
always to run it rather than to ask an operator to.

PageSpeed Insights rather than a local Lighthouse, deliberately. The container
already carries Chromium for Scrapling and a second browser stack would be
800MB and a second thing to keep alive, for a measurement Google will run on
their hardware for free. The other four stay manual and that is correct:
`cursor` and `overlays` need computed styles Lighthouse does not audit, `webmcp`
needs arbitrary JS evaluation, and `web-bot-auth` is a CDN setting rather than a
fact about the page.

## Lab is not field, and the difference is the whole design

A response carries two different measurements and reporting either as the other
would be its own lie:

* `lighthouseResult` -- **lab**. One run, Google's hardware, simulated network.
  Exists for every URL.
* `loadingExperience` -- **CrUX field**, real Chrome users, 28-day p75. Exists
  only where a URL has enough traffic to be reported without identifying anyone.

"CLS under 0.1" means the field number where there is one. So: field when
present, lab when not **and say which**, and `None` when neither -- never `0`.
A small client site usually has no field data at all, and that is a fact about
their traffic rather than about their layout. **[measured 2026-08-24]**
prosperitymedia.com.au returns no field data.

## The audit id moved

`tap-targets` **does not exist in Lighthouse 13** [measured 2026-08-24 against
13.4.1]. The WCAG 2.5.8 check is now `target-size`, in the accessibility
category. The component's `verify` string still quotes the old name, which is
why this module resolves ids itself rather than trusting that string. Both are
tried, newest first, so this keeps working across a version bump in either
direction.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum

import httpx

logger = logging.getLogger(__name__)

ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

# Newest first. `tap-targets` was removed in Lighthouse 13 and replaced by
# `target-size`; trying both means neither a bump nor a rollback breaks this.
TAP_TARGET_AUDITS = ("target-size", "tap-targets")
CLS_AUDIT = "cumulative-layout-shift"

# The threshold the component states, and Google's own "good" boundary.
CLS_GOOD = 0.1

__all__ = ["Basis", "LighthouseFindings", "Metric", "measure"]


class Basis(StrEnum):
    """Where a number came from. Never omitted from a report."""

    FIELD = "field"
    LAB = "lab"


@dataclass(frozen=True, slots=True)
class Metric:
    """One measurement, and how it was taken.

    `value` of `None` means not measured. There is no default, because for every
    metric here zero is a real and *good* value -- a CLS of 0 is a perfect score,
    so defaulting an unmeasured site to 0 would report the best possible result
    for a site nobody looked at.
    """

    value: float | None
    basis: Basis
    detail: str = ""

    @property
    def measured(self) -> bool:
        return self.value is not None

    def describe(self) -> str:
        if self.value is None:
            return "not measured"
        where = "real users, 28-day p75" if self.basis is Basis.FIELD else "lab, one run"
        return f"{self.value:g} ({where})"


@dataclass(frozen=True, slots=True)
class LighthouseFindings:
    """What one PageSpeed run established about one URL."""

    url: str
    cls: Metric | None = None
    # None means the audit was not present in this Lighthouse version. False
    # means it ran and failed. The two must not be merged.
    tap_targets_ok: bool | None = None
    tap_targets_detail: str = ""
    lighthouse_version: str = ""
    fetched_at: str = ""
    error: str = ""

    @property
    def usable(self) -> bool:
        return not self.error


async def measure(
    url: str,
    api_key: str,
    *,
    strategy: str = "mobile",
    timeout: float = 90.0,
    client: httpx.AsyncClient | None = None,
) -> LighthouseFindings:
    """Run PageSpeed against one URL. Never raises.

    Mobile by default: tap-target size is a mobile concern by definition, and an
    agent driving a viewport is closer to a phone than a desktop.

    A failure returns findings with `error` set rather than raising, so one slow
    URL narrows the audit instead of failing it -- the same rule the readiness
    sampler already follows.
    """
    params = {
        "url": url,
        "strategy": strategy,
        "key": api_key,
        "category": ["PERFORMANCE", "ACCESSIBILITY", "SEO"],
    }
    owned = client is None
    http = client or httpx.AsyncClient(timeout=timeout)
    try:
        response = await http.get(ENDPOINT, params=params, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        # A 429 here is our quota, not their site. Saying so keeps somebody
        # else's rate limit out of a client-facing finding.
        return LighthouseFindings(url=url, error=f"{type(exc).__name__}: {exc}"[:200])
    finally:
        if owned:
            await http.aclose()

    return _read(url, payload)


def _read(url: str, payload: dict) -> LighthouseFindings:
    lighthouse = payload.get("lighthouseResult") or {}
    audits = lighthouse.get("audits") or {}

    tap_ok, tap_detail = _tap_targets(audits)
    return LighthouseFindings(
        url=url,
        cls=_cls(payload, audits),
        tap_targets_ok=tap_ok,
        tap_targets_detail=tap_detail,
        lighthouse_version=lighthouse.get("lighthouseVersion", ""),
        fetched_at=lighthouse.get("fetchTime", ""),
    )


def _cls(payload: dict, audits: dict) -> Metric | None:
    """Field first, lab second, `None` when neither answered."""
    metrics = (payload.get("loadingExperience") or {}).get("metrics") or {}
    field = metrics.get("CUMULATIVE_LAYOUT_SHIFT_SCORE") or {}
    if (percentile := field.get("percentile")) is not None:
        # CrUX reports CLS multiplied by 100 so it can stay an integer.
        return Metric(value=percentile / 100, basis=Basis.FIELD, detail=field.get("category", ""))

    audit = audits.get(CLS_AUDIT) or {}
    if (value := audit.get("numericValue")) is not None:
        return Metric(
            value=round(float(value), 4),
            basis=Basis.LAB,
            detail="no field data for this URL, so this is a single lab run",
        )
    return None


def _tap_targets(audits: dict) -> tuple[bool | None, str]:
    """`None` where no version of the audit is present -- not a pass."""
    for name in TAP_TARGET_AUDITS:
        audit = audits.get(name)
        if audit is None:
            continue
        score = audit.get("score")
        if score is None:
            # Lighthouse reports `null` for an audit that could not apply, which
            # is not the same as one that passed.
            return None, f"{name} did not apply to this page"
        detail = audit.get("displayValue") or ""
        return bool(score >= 1), f"{name}: {detail}" if detail else name
    return None, "no tap-target audit in this Lighthouse version"


async def measure_many(
    urls: list[str], api_key: str, *, max_concurrency: int = 2, timeout: float = 90.0
) -> list[LighthouseFindings]:
    """One run per sampled page, gently.

    Capped at two: PageSpeed takes ten to thirty seconds per URL and is rate
    limited per key, and the readiness sampler only ever hands over three or
    four pages. Firing them all at once buys a couple of seconds and risks a 429
    that would report as a failure to measure.
    """
    gate = asyncio.Semaphore(max_concurrency)

    async with httpx.AsyncClient(timeout=timeout) as http:

        async def one(url: str) -> LighthouseFindings:
            async with gate:
                return await measure(url, api_key, timeout=timeout, client=http)

        return list(await asyncio.gather(*(one(u) for u in urls)))
