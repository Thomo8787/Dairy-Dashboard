"""Shared shift and timeline helpers for parlour scatter/efficiency charts."""

from __future__ import annotations

import datetime as dt
from typing import Any, Iterable

from services.milking_efficiency_summary import (
    DAY_SECONDS,
    SHIFT_OPTIONS,
    _overnight_anchor,
    _row_start_seconds,
    _spans_midnight,
    _unwrap_from_anchor,
    uses_overnight_shift_window,
)

SHIFT_IDS = ("Morning", "Day", "Night")
SHIFT_SORT = {"Morning": 0, "Day": 1, "Night": 2}
MAX_CHART_SPAN_DAYS = 31

_SHIFT_ALIASES: dict[str, str] = {}
for _opt in SHIFT_OPTIONS:
    for _raw in _opt["db_values"]:
        _SHIFT_ALIASES[_raw.strip().lower()] = _opt["id"]


def canonical_shift(raw: str | None) -> str | None:
    return _SHIFT_ALIASES.get((raw or "").strip().lower())


def assert_date_span(
    date_from: dt.date | None,
    date_to: dt.date | None,
    *,
    max_days: int = MAX_CHART_SPAN_DAYS,
) -> None:
    if date_from and date_to and date_from > date_to:
        raise ValueError("date_from must be on or before date_to")
    if date_from and date_to and (date_to - date_from).days > max_days:
        raise ValueError(f"Date range cannot exceed {max_days} days.")


def parse_shift_list(raw: str | None) -> list[str] | None:
    """None = all shifts; empty list = none selected."""
    if raw is None:
        return None
    if raw.strip() == "":
        return []
    out: list[str] = []
    for part in raw.split(","):
        key = canonical_shift(part) or part.strip()
        if key in SHIFT_IDS and key not in out:
            out.append(key)
    return out


def wall_clock_ms(milking_date: dt.date, abs_seconds: float) -> int:
    """Naive UTC epoch so Chart.js UTC ticks match parlour clock time."""
    total = int(round(abs_seconds))
    day_offset, secs = divmod(total, DAY_SECONDS)
    day = milking_date + dt.timedelta(days=day_offset)
    started = dt.datetime.combine(day, dt.time.min) + dt.timedelta(seconds=secs)
    return int((started - dt.datetime(1970, 1, 1)).total_seconds() * 1000)


def session_abs_start(
    start_s: float,
    *,
    overnight: bool,
    anchor: float,
) -> float:
    if overnight:
        return _unwrap_from_anchor(start_s, anchor)
    return start_s % DAY_SECONDS


def session_timeline(
    rows: Iterable[Any],
    *,
    farm_code: str,
    shift_id: str,
) -> tuple[bool, float, dict[int, float]]:
    """Return overnight flag, anchor, and id(row) -> absolute start seconds."""
    parsed: list[tuple[Any, float]] = []
    for row in rows:
        start_s = _row_start_seconds(row)
        if start_s is None:
            continue
        parsed.append((row, start_s))
    starts = [s for _, s in parsed]
    overnight = uses_overnight_shift_window(farm_code, shift_id) or _spans_midnight(starts)
    anchor = _overnight_anchor(starts) if overnight else 0.0
    by_id = {
        id(row): session_abs_start(start_s, overnight=overnight, anchor=anchor)
        for row, start_s in parsed
    }
    return overnight, anchor, by_id
