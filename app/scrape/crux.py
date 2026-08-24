"""Chrome UX Report — what real users actually experienced.

PageSpeed already returns a slice of CrUX inside `loadingExperience`, and for a
small site that slice is usually empty: it is page-level, and a page needs
substantial traffic before Google will report it without identifying anyone.
Querying CrUX directly is better because it can be asked at **origin**
granularity, where the whole site's traffic pools.

**[measured 2026-08-24, prosperitymedia.com.au]** the difference is not
theoretical:

    URL + PHONE                404 no data
    origin + PHONE             404 no data
    origin, all form factors   OK, CLS p75 = 0.01

So a site PageSpeed reports as having no field data at all does have it, one
query away. The tool was about to report a lab number as the answer.

## Origin is not the page, and the report must say so

A CLS of 0.01 across an origin is a fact about the site, not about the page an
agent will land on. It is far better evidence than a single synthetic run, and
it is still not page-level. Hence three bases rather than two, ordered by how
directly each bears on the page in question, and every report naming which it
used.

The same key serves the daily and the History API. History returns roughly six
months in ~25 weekly collection periods, which is what makes "is this getting
worse" answerable at all -- a single p75 cannot distinguish a site that has
always been fine from one that just recovered.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum

import httpx

logger = logging.getLogger(__name__)

DAILY = "https://chromeuxreport.googleapis.com/v1/records:queryRecord"
HISTORY = "https://chromeuxreport.googleapis.com/v1/records:queryHistoryRecord"

__all__ = ["Basis", "CruxResult", "Trend", "fetch_crux"]


class Basis(StrEnum):
    """Where a number came from, ordered by how directly it bears on the page.

    Never omitted from a report. Two sites both "passing CLS" on different bases
    have not been measured the same way, and only one of them has been measured
    at all.
    """

    FIELD_URL = "field_url"
    FIELD_ORIGIN = "field_origin"
    LAB = "lab"

    @property
    def describe(self) -> str:
        return {
            Basis.FIELD_URL: "real users on this page, 28-day p75",
            Basis.FIELD_ORIGIN: "real users across the whole site, 28-day p75",
            Basis.LAB: "a single lab run, no field data available",
        }[self]

    @property
    def is_field(self) -> bool:
        return self is not Basis.LAB


def _median(values: tuple[float, ...] | list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


class Direction(StrEnum):
    IMPROVING = "improving"
    STEADY = "steady"
    WORSENING = "worsening"


@dataclass(frozen=True, slots=True)
class Trend:
    """Six months of p75, and which way it is going.

    `material` is the smallest change worth telling a client about, in the
    metric's own units. It is not optional and it is not derived from the data,
    because deriving it from the observed spread is how a stable series gets
    reported as a trend: prosperitymedia's CLS went 0.00 to 0.01 over six months
    against a threshold of 0.1, which is real movement in the numbers and noise
    against the budget. The first version of this called that "worsening".
    """

    values: tuple[float, ...] = ()
    first_date: str = ""
    last_date: str = ""
    material: float = 0.0

    @property
    def direction(self) -> Direction | None:
        """`None` where there is too little history to say.

        Compares the **median** of the first quarter against the last.

        Quarters rather than endpoints, because two points on a noisy weekly
        series is a coin toss presented as a measurement. Median rather than
        mean, because one bad week drags a mean: a flat series with a single
        0.30 spike in its last quarter was reported as "worsening" until a test
        caught it, which is the same false-alarm class as the material threshold
        above and would have a client chasing a week that had already passed.
        """
        if len(self.values) < 8:
            return None
        span = max(2, len(self.values) // 4)
        start = _median(self.values[:span])
        end = _median(self.values[-span:])
        if abs(end - start) < self.material:
            return Direction.STEADY
        return Direction.IMPROVING if end < start else Direction.WORSENING

    def describe(self) -> str:
        direction = self.direction
        if direction is None:
            return "not enough history to show a trend"
        return f"{direction.value} over {len(self.values)} periods to {self.last_date}"


@dataclass(frozen=True, slots=True)
class CruxResult:
    """One metric's field value, its basis, and its history."""

    cls_p75: float | None = None
    basis: Basis | None = None
    lcp_p75: float | None = None
    inp_p75: float | None = None
    period_end: str = ""
    trend: Trend | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def measured(self) -> bool:
        return self.cls_p75 is not None


def _p75(record: dict, metric: str) -> float | None:
    raw = ((record.get("metrics") or {}).get(metric) or {}).get("percentiles", {}).get("p75")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _date(period: dict) -> str:
    if not period:
        return ""
    return f"{period.get('year', 0):04d}-{period.get('month', 0):02d}-{period.get('day', 0):02d}"


async def _query(http: httpx.AsyncClient, url: str, body: dict, key: str) -> dict | None:
    """A 404 is the documented "no data" answer, not an error worth reporting."""
    try:
        response = await http.post(url, params={"key": key}, json=body, timeout=45.0)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("crux query failed for %s: %s", body, exc)
        return None


async def fetch_crux(
    site_url: str, api_key: str, *, page_url: str = "", material: float = 0.02
) -> CruxResult:
    """Field data for a page, falling back to its origin. Never raises.

    Three queries at most, and it stops at the first that answers:

    1. the exact page, phone
    2. the origin, phone
    3. the origin, every form factor pooled

    Ordered by how directly each bears on what an agent will meet. Step three is
    what usually answers for a client-sized site, and the result says so rather
    than implying the page itself was measured.
    """
    origin = site_url.rstrip("/")
    notes: list[str] = []

    attempts: list[tuple[dict, Basis, str]] = []
    if page_url:
        attempts.append(({"url": page_url, "formFactor": "PHONE"}, Basis.FIELD_URL, "this page"))
    attempts += [
        ({"origin": origin, "formFactor": "PHONE"}, Basis.FIELD_ORIGIN, "the origin on phones"),
        ({"origin": origin}, Basis.FIELD_ORIGIN, "the origin, all form factors"),
    ]

    async with httpx.AsyncClient(timeout=45.0) as http:
        for body, basis, label in attempts:
            payload = await _query(http, DAILY, body, api_key)
            if payload is None:
                notes.append(f"no CrUX data for {label}")
                continue
            record = payload.get("record") or {}
            cls = _p75(record, "cumulative_layout_shift")
            if cls is None:
                notes.append(f"CrUX answered for {label} but reported no layout shift")
                continue
            return CruxResult(
                cls_p75=cls,
                basis=basis,
                lcp_p75=_p75(record, "largest_contentful_paint"),
                inp_p75=_p75(record, "interaction_to_next_paint"),
                period_end=_date((record.get("collectionPeriod") or {}).get("lastDate", {})),
                trend=await _history(http, origin, api_key, material=material),
                notes=notes,
            )

    return CruxResult(notes=notes)


async def _history(
    http: httpx.AsyncClient, origin: str, api_key: str, *, material: float = 0.02
) -> Trend | None:
    """Six months of weekly p75, origin-level. Absent is not a failure."""
    payload = await _query(http, HISTORY, {"origin": origin}, api_key)
    if payload is None:
        return None
    record = payload.get("record") or {}
    series = (
        ((record.get("metrics") or {}).get("cumulative_layout_shift") or {})
        .get("percentilesTimeseries", {})
        .get("p75s", [])
    )
    values: list[float] = []
    for raw in series:
        # A period with too little traffic comes back as null. Dropped rather
        # than zero-filled: zero is a perfect CLS and would fake an improvement.
        if raw is None:
            continue
        try:
            values.append(float(raw))
        except (TypeError, ValueError):
            continue
    if not values:
        return None

    periods = record.get("collectionPeriods") or []
    return Trend(
        values=tuple(values),
        first_date=_date((periods[0] or {}).get("lastDate", {})) if periods else "",
        last_date=_date((periods[-1] or {}).get("lastDate", {})) if periods else "",
        material=material,
    )
