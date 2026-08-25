"""Rotary cycle time from successive starts at the same milking point."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from statistics import fmean, median
from typing import Any

from sqlalchemy import func, select

from services.database import MilkFlowRecord, get_session
from services.farms import FARMS_BY_CODE
from services.milking_efficiency_summary import _normalize_milking_point
from services.parlour_common import (
    SHIFT_IDS,
    assert_date_span,
    canonical_shift,
    session_timeline,
    wall_clock_ms,
)
from services.parlour_scatter import _load_milk_rows

MIN_ROTATION_SECONDS = 180
MAX_ROTATION_SECONDS = 1500
EFFICIENCY_MAX_ROTATION_SECONDS = 780
DEFAULT_MA_WINDOW = 40
MAX_POINTS = 25000


def _farm_key(farm: str | None) -> str:
    key = (farm or "ALH").upper()
    return key if key in FARMS_BY_CODE else "ALH"


def _int_point(raw: str | None) -> int | None:
    text = _normalize_milking_point(raw)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def rotation_date_bounds(farm: str | None = None) -> dict[str, str | None]:
    farm_code = _farm_key(farm) if farm else None
    with get_session() as session:
        stmt = select(func.min(MilkFlowRecord.milking_date), func.max(MilkFlowRecord.milking_date))
        if farm_code:
            stmt = stmt.where(MilkFlowRecord.farm_code == farm_code)
        mn, mx = session.execute(stmt).one()
    return {
        "date_min": mn.isoformat() if mn else None,
        "date_max": mx.isoformat() if mx else None,
    }


def _moving_average(
    points: list[tuple[int, float]],
    window: int,
) -> list[dict[str, float | int]]:
    if window < 1 or not points:
        return []
    out: list[dict[str, float | int]] = []
    vals: list[float] = []
    for x_ms, gap in points:
        vals.append(gap)
        if len(vals) > window:
            vals.pop(0)
        if len(vals) < max(3, min(window, 10)):
            continue
        out.append({"x": x_ms, "y": round(fmean(vals) / 60.0, 2)})
    return out


def _empty_series(
    farm_key: str,
    bounds: dict[str, str | None],
    window: int,
    min_s: int,
    max_s: int,
) -> dict[str, Any]:
    return {
        "farm": farm_key,
        "date_min": bounds["date_min"],
        "date_max": bounds["date_max"],
        "gap_count_raw": 0,
        "gap_count_clean": 0,
        "median_rotation_minutes": None,
        "mean_rotation_minutes": None,
        "ma_window": window,
        "min_seconds": min_s,
        "max_seconds": max_s,
        "points": [],
        "moving_average": [],
        "truncated": False,
    }


def list_rotation_series(
    *,
    farm: str,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    shifts: list[str] | None = None,
    ma_window: int = DEFAULT_MA_WINDOW,
    min_seconds: int = MIN_ROTATION_SECONDS,
    max_seconds: int = EFFICIENCY_MAX_ROTATION_SECONDS,
) -> dict[str, Any]:
    assert_date_span(date_from, date_to)
    farm_key = _farm_key(farm)
    window = max(5, min(int(ma_window or DEFAULT_MA_WINDOW), 500))
    min_s = max(60, int(min_seconds))
    max_s = max(min_s + 60, int(max_seconds))
    bounds = rotation_date_bounds(farm_key)
    if shifts is not None and len(shifts) == 0:
        return _empty_series(farm_key, bounds, window, min_s, max_s)

    rows = _load_milk_rows(farm_key, date_from, date_to, shifts)
    sessions: dict[tuple[dt.date, str], list[MilkFlowRecord]] = defaultdict(list)
    for row in rows:
        shift_id = canonical_shift(row.shift)
        if shift_id is None or shift_id not in SHIFT_IDS:
            continue
        sessions[(row.milking_date, shift_id)].append(row)

    raw_gaps: list[tuple[int, float, int]] = []
    for (milking_date, shift_id), session_rows in sorted(sessions.items()):
        _overnight, _anchor, abs_by_id = session_timeline(
            session_rows, farm_code=farm_key, shift_id=shift_id
        )
        by_point: dict[int, list[int]] = defaultdict(list)
        for row in session_rows:
            point = _int_point(row.milking_point)
            abs_s = abs_by_id.get(id(row))
            if point is None or abs_s is None:
                continue
            by_point[point].append(int(abs_s))
        for point, abs_starts in by_point.items():
            ordered = sorted(abs_starts)
            for prev, nxt in zip(ordered, ordered[1:]):
                gap = nxt - prev
                raw_gaps.append((wall_clock_ms(milking_date, nxt), float(gap), point))

    raw_gaps.sort(key=lambda item: item[0])
    gap_count_raw = len(raw_gaps)
    clean = [(x, gap, pt) for x, gap, pt in raw_gaps if min_s <= gap <= max_s]
    clean_gaps = [gap for _, gap, _ in clean]
    gap_count_clean = len(clean)

    ma_full = _moving_average([(x, gap) for x, gap, _ in clean], window)
    truncated = len(clean) > MAX_POINTS
    plot_clean = clean
    plot_ma = ma_full
    if truncated:
        step = max(1, len(clean) // MAX_POINTS)
        plot_clean = clean[::step][:MAX_POINTS]
        if len(ma_full) > MAX_POINTS:
            ma_step = max(1, len(ma_full) // MAX_POINTS)
            plot_ma = ma_full[::ma_step][:MAX_POINTS]

    return {
        "farm": farm_key,
        "date_min": bounds["date_min"],
        "date_max": bounds["date_max"],
        "gap_count_raw": gap_count_raw,
        "gap_count_clean": gap_count_clean,
        "median_rotation_minutes": (
            round(median(clean_gaps) / 60.0, 2) if clean_gaps else None
        ),
        "mean_rotation_minutes": (
            round(fmean(clean_gaps) / 60.0, 2) if clean_gaps else None
        ),
        "ma_window": window,
        "min_seconds": min_s,
        "max_seconds": max_s,
        "points": [
            {"x": x, "y": round(gap / 60.0, 2), "milking_point": pt}
            for x, gap, pt in plot_clean
        ],
        "moving_average": plot_ma,
        "truncated": truncated,
    }
