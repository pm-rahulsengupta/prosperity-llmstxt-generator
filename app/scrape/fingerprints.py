"""Technology detection from the community Wappalyzer fingerprint set.

A BuiltWith alternative, fetched at runtime rather than vendored. The distinction
matters: `enthec/webappanalyzer` is GPL-3.0, this repository is public, and GPL
obligations attach to distribution. Downloading the dataset on first use keeps it
out of the tree entirely — the repo carries the matcher, not the fingerprints —
while the tool still gets the breadth. Nothing here is redistributed.

Wappalyzer's own client runs in a browser and matches on five surfaces: headers,
cookies, meta tags, script sources and evaluated JavaScript globals. Only the last
needs a browser, and it is skipped. That is a deliberate trade: this runs in a web
request in well under a second, against a worker already tuned to
`MAX_BROWSER_CONCURRENCY=2` on a 2GB container, where launching Chromium per
lookup would compete for the one resource that is actually scarce. The cost is
missing technologies that only announce themselves through a JS global; the four
remaining surfaces identify every platform this tool cares about.

Everything degrades rather than fails. With no network, a stale cache is used; with
no cache, `tech_probe`'s built-in signs still identify the common platforms. A site
audit that cannot run is worse than one that runs with less.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# The maintained continuation of Wappalyzer, which went private in 2023. Split
# across a-z plus `_` for everything else.
DATASET_BASE = "https://raw.githubusercontent.com/enthec/webappanalyzer/main/src"
SHARDS = "_abcdefghijklmnopqrstuvwxyz"

CACHE_DIR = Path(tempfile.gettempdir()) / "llmstxt-fingerprints"
# Long, because the dataset changes slowly and a stale technology list is a much
# smaller problem than a site audit that stalls on a GitHub fetch.
CACHE_TTL_SECONDS = 7 * 24 * 3600
FETCH_TIMEOUT = 20.0

# Wappalyzer category ids that change what an agents.md may say. The rest are
# recorded but decide nothing -- knowing the analytics vendor is interesting and
# changes no line of the output.
CATEGORY_ECOMMERCE = 6
CATEGORY_CMS = 1
CATEGORY_BLOG = 11


@dataclass(frozen=True, slots=True)
class TechMatch:
    """One technology, with the surface that identified it."""

    name: str
    categories: tuple[int, ...]
    evidence: str

    @property
    def is_ecommerce(self) -> bool:
        return CATEGORY_ECOMMERCE in self.categories

    @property
    def is_cms(self) -> bool:
        return bool({CATEGORY_CMS, CATEGORY_BLOG} & set(self.categories))


def _pattern(raw: str) -> re.Pattern[str] | None:
    """Compile a Wappalyzer pattern, dropping its metadata suffixes.

    Patterns carry `\\;confidence:50` and `\\;version:\\1` tails that are not part
    of the expression. An empty pattern means "the key existing is the signal",
    which the caller handles; here it compiles to a match-anything.
    """
    body = (raw or "").split("\\;")[0]
    if not body:
        return re.compile("")
    try:
        return re.compile(body, re.I)
    except re.error:
        # A handful of entries use PCRE constructs Python rejects. Skipping one
        # fingerprint is right; letting it abort the whole match is not.
        return None


def _cache_path(shard: str) -> Path:
    return CACHE_DIR / f"{shard}.json"


def _read_cache(shard: str, max_age: float) -> dict | None:
    path = _cache_path(shard)
    if not path.is_file():
        return None
    if max_age and (time.time() - path.stat().st_mtime) > max_age:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_fingerprints(refresh: bool = False) -> dict[str, dict]:
    """The technology definitions, from cache or from the dataset.

    Returns an empty dict rather than raising when nothing can be loaded, so a
    caller with no network degrades to its own built-in signs.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    technologies: dict[str, dict] = {}
    missing: list[str] = []

    for shard in SHARDS:
        cached = _read_cache(shard, 0 if refresh else CACHE_TTL_SECONDS)
        if cached is None:
            missing.append(shard)
        else:
            technologies.update(cached)

    if missing:
        try:
            with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
                for shard in missing:
                    response = client.get(f"{DATASET_BASE}/technologies/{shard}.json")
                    if response.status_code != 200:
                        continue
                    data = response.json()
                    _cache_path(shard).write_text(json.dumps(data), encoding="utf-8")
                    technologies.update(data)
        except (httpx.HTTPError, ValueError, OSError) as exc:
            logger.info("fingerprint dataset unavailable (%s); using what is cached", exc)
            # Fall back to any expired cache rather than nothing: a year-old
            # fingerprint still identifies WordPress.
            for shard in missing:
                if (stale := _read_cache(shard, 0)) is not None:
                    technologies.update(stale)

    return technologies


def _matches_map(spec: dict, values: dict[str, str]) -> str | None:
    """Match a `headers`/`cookies`/`meta` style mapping against real values."""
    for key, raw in (spec or {}).items():
        actual = values.get(key.lower())
        if actual is None:
            continue
        pattern = _pattern(raw if isinstance(raw, str) else "")
        if pattern is None:
            continue
        if pattern.search(actual):
            shown = f"{key}={actual[:40]}" if actual else key
            return shown
    return None


def _matches_list(spec, haystack: str) -> str | None:
    entries = spec if isinstance(spec, list) else [spec]
    for raw in entries:
        if not isinstance(raw, str):
            continue
        pattern = _pattern(raw)
        if pattern is None or not pattern.pattern:
            continue
        found = pattern.search(haystack)
        if found:
            return found.group(0)[:60]
    return None


META_TAG = re.compile(r"""<meta[^>]+name=["']([^"']+)["'][^>]+content=["']([^"']*)["']""", re.I)
SCRIPT_SRC = re.compile(r"""<script[^>]+src=["']([^"']+)["']""", re.I)


def detect(
    url: str,
    headers: dict[str, str],
    html: str,
    technologies: dict[str, dict] | None = None,
) -> list[TechMatch]:
    """Identify technologies from what a single page response reveals.

    Four surfaces, in descending order of trustworthiness: response headers and
    cookies (set by the server), meta tags and script sources (set by the theme).
    JavaScript globals are not evaluated -- that is the browser-only surface.
    """
    technologies = technologies if technologies is not None else load_fingerprints()
    if not technologies:
        return []

    lowered_headers = {k.lower(): (v or "") for k, v in headers.items()}
    cookies = _cookie_names(lowered_headers.get("set-cookie", ""))
    metas = {name.lower(): content for name, content in META_TAG.findall(html or "")}
    scripts = " ".join(SCRIPT_SRC.findall(html or ""))

    found: list[TechMatch] = []
    for name, spec in technologies.items():
        if not isinstance(spec, dict):
            continue
        evidence = (
            _prefixed("header", _matches_map(spec.get("headers"), lowered_headers))
            or _prefixed("cookie", _matches_map(spec.get("cookies"), cookies))
            or _prefixed("meta", _matches_map(spec.get("meta"), metas))
            or _prefixed("script", _matches_list(spec.get("scriptSrc"), scripts))
            or _prefixed("url", _matches_list(spec.get("url"), url))
        )
        if evidence:
            cats = tuple(c for c in (spec.get("cats") or []) if isinstance(c, int))
            found.append(TechMatch(name=name, categories=cats, evidence=evidence))

    found.sort(key=lambda m: m.name.lower())
    return found


def _prefixed(kind: str, value: str | None) -> str | None:
    return f"{kind}: {value}" if value else None


def _cookie_names(set_cookie: str) -> dict[str, str]:
    """Cookie name -> value, from however many Set-Cookie headers httpx joined.

    Wappalyzer keys cookie fingerprints on the name, so the names have to be
    separated out; matching the raw joined header would let one site's cookie
    value satisfy another site's name pattern.
    """
    cookies: dict[str, str] = {}
    for chunk in set_cookie.split(","):
        pair = chunk.strip().split(";")[0]
        if "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        if name := name.strip().lower():
            cookies[name] = value.strip()
    return cookies


def summarise(matches: list[TechMatch]) -> dict[str, list[str]]:
    """Group matches for display, ecommerce first because it decides the profile."""
    grouped: dict[str, list[str]] = {"ecommerce": [], "cms": [], "other": []}
    for match in matches:
        if match.is_ecommerce:
            grouped["ecommerce"].append(match.name)
        elif match.is_cms:
            grouped["cms"].append(match.name)
        else:
            grouped["other"].append(match.name)
    return grouped
