"""Search Console data from a file the operator exported themselves.

The service-account path needs a client to add us to their property, which is a
request that takes days and sometimes gets refused. Anyone with Search Console
access can export a CSV in thirty seconds, so this is the path that works on day
one -- and on properties we will never be granted, which on the agency side is
most of them.

Deliberately tolerant about shape and strict about meaning. Google ships the
Pages export as `Pages.csv` inside a zip, the column is named for the UI tab
rather than the data ("Top pages"), the header is localised, and numbers carry
thousands separators and percent signs. None of that is worth failing over. What
is worth failing over is a file that is not page-level data at all: a Queries
export has the right columns and the wrong dimension, and silently scoring pages
on query rows would produce numbers that look plausible and mean nothing.
"""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass

from app.core.metrics import DateRange, PageMetrics, merge_metrics

__all__ = ["GSCImport", "parse_gsc_export"]

# The Pages export names its URL column after the UI tab. Localised exports vary,
# so matching is by substring against a lowered header.
URL_HEADERS = ("top pages", "page", "url", "landing page", "adresse", "página", "pagina", "seite")
CLICK_HEADERS = ("click", "clics", "clicks", "klicks", "cliques")
IMPRESSION_HEADERS = ("impression", "impresiones", "impressionen", "impressões")
CTR_HEADERS = ("ctr",)
POSITION_HEADERS = ("position", "posición", "posizione", "durchschnittliche position")

# Headers that prove the file is the wrong dimension. A Queries export has
# clicks and impressions too, and scoring pages on it would be undetectable.
WRONG_DIMENSION = {
    "query": "queries",
    "search query": "queries",
    "consulta": "queries",
    "suchanfrage": "queries",
    "country": "countries",
    "device": "devices",
    "date": "dates",
    "search appearance": "search appearance",
}


@dataclass(frozen=True, slots=True)
class GSCImport:
    """The parsed file, plus what the operator needs told about it."""

    metrics: dict[str, PageMetrics]
    row_count: int = 0
    merged_count: int = 0
    notes: tuple[str, ...] = ()

    @property
    def url_count(self) -> int:
        return len(self.metrics)


def _pick(headers: list[str], candidates: tuple[str, ...]) -> int | None:
    for index, header in enumerate(headers):
        lowered = header.strip().lower().lstrip("﻿")
        if any(candidate in lowered for candidate in candidates):
            return index
    return None


def _number(raw: str) -> float | None:
    """Parse what a spreadsheet export actually contains.

    Thousands separators, percent signs, and the non-breaking spaces European
    locales use for grouping. A value that cannot be read is None, not zero --
    the whole metrics layer rests on that distinction.
    """
    # The separators are written as escapes on purpose: a literal non-breaking
    # space is invisible in review and indistinguishable from a normal one.
    text = (raw or "").strip().replace("\u00a0", "").replace("\u202f", "").replace(" ", "")
    if not text:
        return None
    percent = text.endswith("%")
    text = text.rstrip("%").replace(",", "").replace(" ", "")
    # A European decimal comma survives as a dot only if there was no grouping;
    # after stripping commas, "1.234" is ambiguous, so trust the dot.
    try:
        value = float(text)
    except ValueError:
        return None
    return value / 100 if percent else value


def _rows_from(payload: bytes | str) -> tuple[list[list[str]], list[str]]:
    """Accept a zip as downloaded, or a single CSV pulled out of one."""
    notes: list[str] = []
    if isinstance(payload, str):
        text = payload
    elif payload[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
            names = bundle.namelist()
            page_files = [n for n in names if "page" in n.lower() and n.lower().endswith(".csv")]
            if not page_files:
                raise ValueError(
                    "That zip has no Pages export in it. It contains: " + ", ".join(names)
                )
            chosen = page_files[0]
            if len(names) > 1:
                notes.append(
                    f"Read {chosen} from the export; ignored {len(names) - 1} other file(s)."
                )
            text = bundle.read(chosen).decode("utf-8-sig", errors="replace")
    else:
        text = payload.decode("utf-8-sig", errors="replace")

    return list(csv.reader(io.StringIO(text))), notes


def parse_gsc_export(
    payload: bytes | str, window: DateRange | None = None, source: str = "gsc-upload"
) -> GSCImport:
    """Parse a Search Console Pages export into metrics keyed by canonical URL.

    `window` is the operator's word for the date range the export covers, since
    the CSV itself does not carry one. It is recorded rather than checked -- but
    it is worth carrying, because a group's verdict is only comparable against
    another run over the same window.
    """
    rows, notes = _rows_from(payload)
    rows = [row for row in rows if any(cell.strip() for cell in row)]
    if not rows:
        raise ValueError("That file has no rows in it.")

    headers = rows[0]
    lowered = {h.strip().lower().lstrip("﻿") for h in headers}
    for header, kind in WRONG_DIMENSION.items():
        if header in lowered:
            raise ValueError(
                f"That looks like the {kind} export, not Pages. It has a '{header}' column, "
                "so its rows are not pages and scoring pages on them would be meaningless. "
                "Export the Pages tab instead."
            )

    url_at = _pick(headers, URL_HEADERS)
    if url_at is None:
        raise ValueError(
            "No URL column found. Expected a column like 'Top pages'; got: "
            + ", ".join(h.strip() for h in headers if h.strip())
        )
    clicks_at = _pick(headers, CLICK_HEADERS)
    impressions_at = _pick(headers, IMPRESSION_HEADERS)
    ctr_at = _pick(headers, CTR_HEADERS)
    position_at = _pick(headers, POSITION_HEADERS)

    if clicks_at is None and impressions_at is None:
        raise ValueError(
            "That file has a URL column but neither clicks nor impressions, so there is "
            "nothing to rank on."
        )
    if clicks_at is None:
        notes.append("No clicks column; coverage will be unavailable and groups held for review.")

    def cell(row: list[str], index: int | None) -> str:
        return row[index] if index is not None and index < len(row) else ""

    parsed: list[PageMetrics] = []
    skipped = 0
    for row in rows[1:]:
        url = cell(row, url_at).strip()
        if not url.startswith(("http://", "https://")):
            skipped += 1
            continue
        clicks = _number(cell(row, clicks_at))
        impressions = _number(cell(row, impressions_at))
        parsed.append(
            PageMetrics(
                url=url,
                clicks=int(clicks) if clicks is not None else None,
                impressions=int(impressions) if impressions is not None else None,
                ctr=_number(cell(row, ctr_at)),
                position=_number(cell(row, position_at)),
                source=source,
                window=window,
            )
        )

    if skipped:
        notes.append(f"Skipped {skipped:,} row(s) whose first column was not a URL.")
    if not parsed:
        raise ValueError(
            "No rows with a usable URL. Search Console exports absolute URLs; if this file "
            "has paths only, it is not a Pages export."
        )

    merged = merge_metrics(parsed)
    if (collapsed := len(parsed) - len(merged)) > 0:
        notes.append(
            f"{collapsed:,} row(s) were tracking-tagged variants and were merged into the "
            "page they belong to."
        )

    return GSCImport(
        metrics=merged, row_count=len(parsed), merged_count=collapsed, notes=tuple(notes)
    )
