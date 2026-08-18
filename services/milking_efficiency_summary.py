"""7-day milking efficiency summary for the Parlours page."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Any

from services.database import (
    MilkFlowRecord,
    MilkingEfficiencyDayCache,
    ParlourImportBatch,
    RotaryEntryIdRecord,
    get_session,
    init_db,
)
from services.farms import FARMS, FARMS_BY_CODE
from services.parlour_link import (
    OVERNIGHT_NEXT_DAY_ID_BEFORE_S,
    match_milk_flow_to_entry_ids,
    parse_hms as _parse_hms,
)

logger = logging.getLogger(__name__)

DAY_SECONDS = 24 * 3600
CACHE_LOOKBACK_DATES = 21

# UI shift labels -> possible values stored from DataFlow CSVs
SHIFT_OPTIONS = (
    {"id": "Morning", "label": "Morning", "db_values": ("Morning", "morning"), "crosses_midnight": False},
    {
        "id": "Day",
        "label": "Day",
        "db_values": ("Noon", "Day", "Afternoon", "noon", "day", "afternoon"),
        "crosses_midnight": False,
    },
    {
        "id": "Night",
        "label": "Night",
        # All farms: evening/night milkings share one milking_date across midnight.
        "db_values": ("Evening", "Night", "Evening ", "evening", "night"),
        "crosses_midnight": True,
    },
)

SHIFT_BY_ID = {item["id"]: item for item in SHIFT_OPTIONS}


def _timedelta_to_seconds(value: timedelta | None) -> float | None:
    if value is None:
        return None
    return value.total_seconds()


def _format_hms(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    total = int(round(seconds)) % DAY_SECONDS
    if total < 0:
        total += DAY_SECONDS
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


def _format_hm(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m = rem // 60
    return f"{h}h {m:02d}m"


def _format_ms(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    total = int(round(seconds))
    m, s = divmod(abs(total), 60)
    return f"{m}m {s:02d}s"


def _format_number(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    if digits == 0:
        return str(int(round(value)))
    return f"{value:.{digits}f}"


def _format_pct(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}%"


def _mean(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return sum(clean) / len(clean)


def _median(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return float(median(clean))


def _normalize_shift(raw: str | None) -> str:
    return (raw or "").strip()


def resolve_shift_filter(shift_id: str) -> tuple[str, tuple[str, ...]]:
    option = SHIFT_BY_ID.get(shift_id) or SHIFT_BY_ID["Morning"]
    return option["id"], option["db_values"]


def shift_crosses_midnight(shift_id: str) -> bool:
    option = SHIFT_BY_ID.get(shift_id) or SHIFT_BY_ID["Morning"]
    return bool(option.get("crosses_midnight"))


def farm_night_crosses_midnight(farm_code: str) -> bool:
    """Whether this farm's night/evening milkings keep one date across midnight."""
    farm = FARMS_BY_CODE.get(farm_code)
    if farm is None:
        # Default on: DataFlow night reports behave this way on all current farms.
        return True
    return bool(getattr(farm, "night_shift_crosses_midnight", True))


def uses_overnight_shift_window(farm_code: str, shift_id: str) -> bool:
    """Night shift on farms that store post-midnight milkings on the same milking_date."""
    return shift_crosses_midnight(shift_id) and farm_night_crosses_midnight(farm_code)


def _spans_midnight(starts: list[float]) -> bool:
    """True when the same milking_date has both late-evening and early-morning milkings."""
    if not starts:
        return False
    clocks = [s % DAY_SECONDS for s in starts]
    has_evening = any(s >= 18 * 3600 for s in clocks)
    has_early_morning = any(s < 6 * 3600 for s in clocks)
    return has_evening and has_early_morning


# Gaps larger than this split a pen's milking times into separate blocks.
# Strays milked with another pen typically sit 30–140+ minutes off the main block.
PEN_CLUSTER_MAX_GAP_S = 15 * 60


def _normalize_milking_point(raw: str | None) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        number = float(text)
        if number.is_integer():
            return str(int(number))
        return str(number)
    except ValueError:
        return text


def _resolve_stall_count(farm: Any) -> int | None:
    """Rotary stall count. Only ALH has a rotary; other farms return None."""
    configured = getattr(farm, "stall_count", None)
    return int(configured) if configured else None


def _row_start_seconds(row: MilkFlowRecord) -> float | None:
    return _timedelta_to_seconds(_parse_hms(row.cow_milking_start_time))


def _overnight_anchor(starts: list[float]) -> float:
    """
    First milking-start of the active block on a 24h clock.

    Applies to every farm whose night/evening shift is stored on one calendar
    date but crosses midnight: the idle daytime gap is the largest circular gap
    and the shift resumes just after it.
    """
    ordered = sorted({s % DAY_SECONDS for s in starts})
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]

    best_gap = -1.0
    after_idx = 0
    for i in range(1, len(ordered)):
        gap = ordered[i] - ordered[i - 1]
        if gap > best_gap:
            best_gap = gap
            after_idx = i
    wrap_gap = ordered[0] + DAY_SECONDS - ordered[-1]
    if wrap_gap > best_gap:
        after_idx = 0
    return ordered[after_idx]


def _unwrap_from_anchor(clock_s: float, anchor: float) -> float:
    """Place a clock time on a continuous timeline that starts at anchor."""
    value = clock_s % DAY_SECONDS
    if value < anchor:
        return value + DAY_SECONDS
    return value


def _compute_shift_window(
    events: list[tuple[float, float | None]],
    *,
    crosses_midnight: bool = False,
) -> tuple[float | None, float | None, float | None]:
    """
    Shift start/end (clock seconds) and length, including overnight wraps.

    Farm-agnostic. Post-midnight milkings on a night shift keep the same
    milking_date, so a naive min/max of clock times puts start after midnight
    and end before it. When `crosses_midnight` is set (Night on any farm) or
    the starts themselves span evening→morning, times are unwrapped.
    """
    if not events:
        return None, None, None

    starts = [start for start, _ in events]
    use_overnight = crosses_midnight or _spans_midnight(starts)

    if not use_overnight:
        start_secs = [s % DAY_SECONDS for s in starts]
        end_secs = []
        for start, end in events:
            start_clock = start % DAY_SECONDS
            if end is None:
                continue
            duration = end - start
            if duration < 0:
                duration = (end % DAY_SECONDS) - start_clock
                if duration < 0:
                    duration += DAY_SECONDS
            end_secs.append(start_clock + max(duration, 0.0))
        shift_start = min(start_secs)
        shift_end_raw = max(end_secs) if end_secs else max(start_secs)
        return shift_start, shift_end_raw % DAY_SECONDS, shift_end_raw - shift_start

    anchor = _overnight_anchor(starts)

    unwrapped_starts: list[float] = []
    unwrapped_ends: list[float] = []
    for start, end in events:
        start_clock = start % DAY_SECONDS
        start_u = _unwrap_from_anchor(start_clock, anchor)
        unwrapped_starts.append(start_u)
        if end is None:
            continue
        # end may already be start + unit-on (>24h near midnight).
        duration = end - start
        if duration < 0:
            duration = (end % DAY_SECONDS) - start_clock
            if duration < 0:
                duration += DAY_SECONDS
        unwrapped_ends.append(start_u + max(duration, 0.0))

    shift_start_u = min(unwrapped_starts)
    shift_end_u = max(unwrapped_ends) if unwrapped_ends else max(unwrapped_starts)
    if shift_end_u < shift_start_u:
        return None, None, None

    return (
        shift_start_u % DAY_SECONDS,
        shift_end_u % DAY_SECONDS,
        shift_end_u - shift_start_u,
    )


def _largest_time_cluster(
    times: list[float],
    max_gap_s: float = PEN_CLUSTER_MAX_GAP_S,
) -> list[float]:
    """Largest contiguous run of times where consecutive gaps stay within max_gap_s."""
    if not times:
        return []

    # Unwrap overnight blocks so 23:xx and 00:xx form one contiguous run.
    anchor = _overnight_anchor(times)
    unwrapped = [_unwrap_from_anchor(t, anchor) for t in times]
    ordered_u = sorted(unwrapped)
    runs: list[list[float]] = [[ordered_u[0]]]
    for value in ordered_u[1:]:
        if value - runs[-1][-1] <= max_gap_s:
            runs[-1].append(value)
        else:
            runs.append([value])
    best_u = max(runs, key=lambda run: (len(run), -(run[-1] - run[0])))

    best_clock = {u % DAY_SECONDS for u in best_u}
    members = [t for t in times if (t % DAY_SECONDS) in best_clock]
    members.sort(key=lambda t: _unwrap_from_anchor(t, anchor))
    return members


def _in_time_window(start_s: float, lo: float, hi: float) -> bool:
    """True if start is inside [lo, hi], allowing overnight windows where lo > hi."""
    value = start_s % DAY_SECONDS
    lo = lo % DAY_SECONDS
    hi = hi % DAY_SECONDS
    if lo <= hi:
        return lo <= value <= hi
    return value >= lo or value <= hi


def correct_misassigned_pen_cows(
    rows: list[MilkFlowRecord],
    *,
    max_gap_s: float = PEN_CLUSTER_MAX_GAP_S,
) -> tuple[dict[str, list[MilkFlowRecord]], list[dict[str, Any]]]:
    """
    Reassign cows labelled for the wrong pen.

    Each pen's true milking block is the largest contiguous cluster of
    milking-start times (gaps > max_gap_s start a new block). Cows outside
    their labelled pen's main block are moved into the pen whose milking
    window contains their start time (typical when group numbers lag behind
    cow moves). Unmatched strays stay on the labelled pen.
    """
    by_label: dict[str, list[MilkFlowRecord]] = defaultdict(list)
    for row in rows:
        by_label[_pen_label(row.group_number)].append(row)

    windows: dict[str, tuple[float, float]] = {}
    cluster_sizes: dict[str, int] = {}
    cluster_starts: dict[str, set[float]] = {}
    for pen, pen_rows in by_label.items():
        starts = [start for row in pen_rows if (start := _row_start_seconds(row)) is not None]
        cluster = _largest_time_cluster(starts, max_gap_s=max_gap_s)
        if not cluster:
            continue
        windows[pen] = (cluster[0] % DAY_SECONDS, cluster[-1] % DAY_SECONDS)
        cluster_sizes[pen] = len(cluster)
        cluster_starts[pen] = set(cluster)

    corrected: dict[str, list[MilkFlowRecord]] = defaultdict(list)
    corrections: list[dict[str, Any]] = []

    for pen, pen_rows in by_label.items():
        in_cluster = cluster_starts.get(pen, set())
        for row in pen_rows:
            start = _row_start_seconds(row)
            if start is None or start in in_cluster:
                corrected[pen].append(row)
                continue

            candidates = [
                other
                for other, (lo, hi) in windows.items()
                if other != pen and _in_time_window(start, lo, hi)
            ]
            if not candidates:
                corrected[pen].append(row)
                continue

            def _candidate_score(other: str, start_s: float = start) -> tuple[int, float]:
                lo, hi = windows[other]
                if lo <= hi:
                    center = (lo + hi) / 2.0
                else:
                    span = (DAY_SECONDS - lo) + hi
                    center = (lo + span / 2.0) % DAY_SECONDS
                delta = abs(start_s - center)
                delta = min(delta, DAY_SECONDS - delta)
                return (cluster_sizes.get(other, 0), -delta)

            new_pen = max(candidates, key=_candidate_score)
            corrected[new_pen].append(row)
            corrections.append(
                {
                    "cow_number": str(row.cow_number),
                    "from_pen": pen,
                    "to_pen": new_pen,
                    "start_s": start,
                }
            )

    return {pen: pen_rows for pen, pen_rows in corrected.items() if pen_rows}, corrections


def _day_metrics(
    rows: list[MilkFlowRecord],
    entry_rows: list[RotaryEntryIdRecord] | None = None,
    *,
    trim_shift_outliers: bool = False,
    crosses_midnight: bool = False,
    stall_count: int | None = None,
) -> dict[str, Any]:
    if not rows:
        return {}

    yields = [r.shift_yield_l for r in rows if r.shift_yield_l is not None]
    unit_on_secs = []
    milking_events: list[tuple[float, float | None]] = []
    takeoffs = []
    flow_15 = []
    flow_30 = []
    flow_60 = []
    flow_120 = []
    peak_flows = []
    avg_flows = []
    pct_2min = []
    yield_2min = []
    bimodal_flags = []

    by_point: dict[str, list[float]] = defaultdict(list)

    for row in rows:
        unit = _parse_hms(row.unit_on_time)
        start = _parse_hms(row.cow_milking_start_time)
        unit_sec = _timedelta_to_seconds(unit)
        start_sec = _timedelta_to_seconds(start)

        if unit_sec is not None:
            unit_on_secs.append(unit_sec)
        if start_sec is not None:
            end_sec = start_sec + unit_sec if unit_sec is not None else None
            milking_events.append((start_sec, end_sec))
            point = _normalize_milking_point(row.milking_point)
            if point:
                by_point[point].append(start_sec)

        if row.flow_rate_at_removal_ml_per_min is not None:
            takeoffs.append(row.flow_rate_at_removal_ml_per_min)
        if row.flow_rate_15s_ml_per_min is not None:
            flow_15.append(row.flow_rate_15s_ml_per_min)
        if row.flow_rate_30s_ml_per_min is not None:
            flow_30.append(row.flow_rate_30s_ml_per_min)
        if row.flow_rate_60s_ml_per_min is not None:
            flow_60.append(row.flow_rate_60s_ml_per_min)
        if row.flow_rate_120s_ml_per_min is not None:
            flow_120.append(row.flow_rate_120s_ml_per_min)
        if row.peak_milk_flow_l_per_min is not None:
            peak_flows.append(row.peak_milk_flow_l_per_min)
        if row.avg_milk_flow_l_per_min is not None:
            avg_flows.append(row.avg_milk_flow_l_per_min)
        if row.percentage_yield_at_2_min is not None:
            pct_2min.append(row.percentage_yield_at_2_min)
        if row.milk_yield_at_2_min_l is not None:
            yield_2min.append(row.milk_yield_at_2_min_l)

        # Bi-modal let-down: early flow drops vs a prior interval.
        f15 = row.flow_rate_15s_ml_per_min
        f30 = row.flow_rate_30s_ml_per_min
        f60 = row.flow_rate_60s_ml_per_min
        if f15 is not None and f30 is not None and f60 is not None:
            is_bimodal = (f30 < f15) or (f60 < f30) or (f60 < f15)
            bimodal_flags.append(1.0 if is_bimodal else 0.0)
        elif f15 is not None and f30 is not None:
            bimodal_flags.append(1.0 if f30 < f15 else 0.0)
        elif f30 is not None and f60 is not None:
            bimodal_flags.append(1.0 if f60 < f30 else 0.0)
        elif f15 is not None and f60 is not None:
            bimodal_flags.append(1.0 if f60 < f15 else 0.0)

    # Lag phase: milking start − rotary identification (first ID at/before start, ≤5 min).
    # Overnight shifts also match early next-calendar-day entry IDs (caller supplies them).
    lag_secs: list[float] = []
    if entry_rows:
        for match in match_milk_flow_to_entry_ids(
            rows,
            entry_rows,
            crosses_midnight=crosses_midnight,
        ):
            if match["lag_seconds"] >= 0:
                lag_secs.append(match["lag_seconds"])

    timing_events = milking_events
    if trim_shift_outliers and milking_events:
        cluster = set(_largest_time_cluster([start for start, _ in milking_events]))
        timing_events = [(start, end) for start, end in milking_events if start in cluster]

    start_secs = [start for start, _ in timing_events]

    total_cows = len(rows)
    shift_start, shift_end, shift_length = _compute_shift_window(
        timing_events,
        crosses_midnight=crosses_midnight,
    )

    cows_per_hour = None
    cows_for_rate = len(timing_events) if trim_shift_outliers else total_cows
    if shift_length and shift_length > 0 and cows_for_rate:
        cows_per_hour = cows_for_rate / (shift_length / 3600)

    # Rotation approx: median revisit interval on the same milking point.
    # When trimming pen outliers, only use starts inside the main milking block.
    # Unwrap overnight so gaps across midnight are measured correctly.
    rotation_by_point = by_point
    if trim_shift_outliers and start_secs:
        cluster = set(start_secs)
        rotation_by_point = {
            point: [start for start in starts if start in cluster]
            for point, starts in by_point.items()
        }
    revisit_gaps = []
    for starts in rotation_by_point.values():
        if not starts:
            continue
        anchor = _overnight_anchor(starts)
        ordered = sorted(_unwrap_from_anchor(start, anchor) for start in starts)
        gaps = [ordered[i] - ordered[i - 1] for i in range(1, len(ordered))]
        revisit_gaps.extend(gaps)
    rotation = _median(revisit_gaps)

    # Potential cows/hour = stalls × rotations/hour (no empty stalls, same speed).
    parlour_efficiency_pct = None
    if (
        cows_per_hour is not None
        and rotation is not None
        and rotation > 0
        and stall_count
    ):
        potential_cows_per_hour = stall_count * (3600.0 / rotation)
        if potential_cows_per_hour > 0:
            parlour_efficiency_pct = 100.0 * cows_per_hour / potential_cows_per_hour

    high_takeoff_pct = None
    if takeoffs:
        high_takeoff_pct = 100.0 * sum(1 for v in takeoffs if v > 1800) / len(takeoffs)

    bimodal_pct = None
    if bimodal_flags:
        bimodal_pct = 100.0 * sum(bimodal_flags) / len(bimodal_flags)

    return {
        "total_yield_l": sum(yields) if yields else None,
        "avg_yield_l": _mean(yields),
        "total_cows": total_cows,
        "cows_per_hour": cows_per_hour,
        "rotation_min": (rotation / 60) if rotation is not None else None,
        "lag_phase_s": _mean(lag_secs),
        "shift_length_s": shift_length,
        "parlour_efficiency_pct": parlour_efficiency_pct,
        "median_unit_on_s": _median(unit_on_secs),
        "avg_unit_on_s": _mean(unit_on_secs),
        "high_flow_takeoff_pct": high_takeoff_pct,
        "avg_takeoff_flow": _mean(takeoffs),
        "bimodal_pct": bimodal_pct,
        "avg_15s_flow": _mean(flow_15),
        "avg_30s_flow": _mean(flow_30),
        "avg_60s_flow": _mean(flow_60),
        "avg_120s_flow": _mean(flow_120),
        "avg_peak_flow": _mean(peak_flows),
        "avg_flow": _mean(avg_flows),
        "avg_pct_2min": _mean(pct_2min),
        "avg_yield_2min_l": _mean(yield_2min),
        "shift_start_s": shift_start,
        "shift_end_s": shift_end,
        "entry_match_count": len(lag_secs),
    }


METRIC_ROWS = (
    ("total_yield_l", "Total yield (L)", "number1"),
    ("avg_yield_l", "Average Yield (L)", "number1"),
    ("total_cows", "Total Cows", "int"),
    ("cows_per_hour", "Cows / hour", "number1"),
    ("rotation_min", "Rotation (min)", "number1_highlight"),
    ("lag_phase_s", "Lag phase (s)", "number0"),
    ("shift_length_s", "Shift length", "hm"),
    ("parlour_efficiency_pct", "Parlour Efficiency", "pct1"),
    ("median_unit_on_s", "Median Unit On Time", "ms"),
    ("avg_unit_on_s", "Average Unit On Time", "ms"),
    ("high_flow_takeoff_pct", "High flow takeoffs % (>1800)", "pct1"),
    ("avg_takeoff_flow", "Average Takeoff Flow", "number0"),
    ("bimodal_pct", "Bi-modal let-down %", "pct1"),
    ("avg_15s_flow", "Avg 15s flow", "number0"),
    ("avg_30s_flow", "Avg 30s flow", "number0"),
    ("avg_60s_flow", "Avg 60s flow", "number0"),
    ("avg_120s_flow", "Avg 120s flow", "number0"),
    ("avg_peak_flow", "Average Peak Flow", "number2"),
    ("avg_flow", "Average Flow", "number2"),
    ("avg_pct_2min", "Avg % in 2 minutes", "pct1"),
    ("avg_yield_2min_l", "Avg milk yield at 2 min (L)", "number2"),
    ("shift_start_s", "Shift start time", "hms"),
    ("shift_end_s", "Shift end time", "hms"),
)

METRIC_BY_KEY = {key: {"label": label, "kind": kind} for key, label, kind in METRIC_ROWS}
ROTARY_ONLY_METRIC_KEYS = frozenset({"parlour_efficiency_pct"})


def _metric_rows_for_farm(farm: Any) -> tuple[tuple[str, str, str], ...]:
    if _resolve_stall_count(farm):
        return METRIC_ROWS
    return tuple(row for row in METRIC_ROWS if row[0] not in ROTARY_ONLY_METRIC_KEYS)


TREND_DAY_COUNT = 45
SUMMARY_DAYS_PER_SHIFT = 7
SUMMARY_DATE_CHUNK = 14
SHIFT_TREND_COLORS = {
    "Morning": "#1f7a4c",
    "Day": "#c47a12",
    "Night": "#1f3b5a",
}


def _format_metric(value: Any, kind: str) -> str:
    if kind == "int":
        return _format_number(value, 0) if value is not None else "—"
    if kind == "number0":
        return _format_number(value, 0)
    if kind == "number1" or kind == "number1_highlight":
        return _format_number(value, 1)
    if kind == "number2":
        return _format_number(value, 2)
    if kind == "pct1":
        return _format_pct(value, 1)
    if kind == "hm":
        return _format_hm(value)
    if kind == "ms":
        return _format_ms(value)
    if kind == "hms":
        return _format_hms(value)
    return "—"


def _pen_label(group_number: str | None) -> str:
    text = (group_number or "").strip()
    return text if text else "Unknown"


def _pen_sort_key(pen: str) -> tuple[int, int | str]:
    try:
        return (0, int(pen))
    except (TypeError, ValueError):
        return (1, pen)


def _build_metric_table_rows(
    column_keys: list[Any],
    metrics_by_key: dict[Any, dict[str, Any]],
    *,
    farm: Any | None = None,
) -> list[dict[str, Any]]:
    table_rows = []
    for key, label, kind in _metric_rows_for_farm(farm):
        cells = []
        for column_key in column_keys:
            raw = metrics_by_key.get(column_key, {}).get(key)
            cells.append(
                {
                    "text": _format_metric(raw, kind),
                    "highlight": kind == "number1_highlight" and raw is not None,
                }
            )
        table_rows.append({"key": key, "label": label, "cells": cells})
    return table_rows


def _filter_shift_rows(rows: list[Any], db_values_normalized: set[str]) -> list[Any]:
    return [
        row
        for row in rows
        if _normalize_shift(row.shift).lower() in db_values_normalized
    ]


def _filter_entry_rows(
    entry_rows: list[RotaryEntryIdRecord],
    db_values_normalized: set[str],
) -> list[RotaryEntryIdRecord]:
    filtered: list[RotaryEntryIdRecord] = []
    for entry in entry_rows:
        entry_shift = _normalize_shift(entry.shift).lower()
        if not entry_shift or entry_shift in db_values_normalized:
            filtered.append(entry)
    return filtered


def _early_next_day_entry_rows(
    entry_rows: list[RotaryEntryIdRecord],
) -> list[RotaryEntryIdRecord]:
    """
    Rotary Entry IDs for the overnight tail often land on the next calendar date
    while Milk Flow keeps those cows on the night milking_date. Keep only
    early-morning IDs from that next date.
    """
    kept: list[RotaryEntryIdRecord] = []
    for entry in entry_rows:
        id_td = _parse_hms(entry.identification_time)
        if id_td is None:
            continue
        if id_td.total_seconds() % DAY_SECONDS < OVERNIGHT_NEXT_DAY_ID_BEFORE_S:
            kept.append(entry)
    return kept


def _load_lag_entry_rows(
    session: Any,
    *,
    farm_code: str,
    milking_date: date,
    db_values_normalized: set[str],
    crosses_midnight: bool,
) -> list[RotaryEntryIdRecord]:
    entry_rows = (
        session.query(RotaryEntryIdRecord)
        .filter(
            RotaryEntryIdRecord.farm_code == farm_code,
            RotaryEntryIdRecord.milking_date == milking_date,
        )
        .all()
    )
    filtered = _filter_entry_rows(entry_rows, db_values_normalized)
    if not crosses_midnight:
        return filtered

    next_day_rows = (
        session.query(RotaryEntryIdRecord)
        .filter(
            RotaryEntryIdRecord.farm_code == farm_code,
            RotaryEntryIdRecord.milking_date == milking_date + timedelta(days=1),
        )
        .all()
    )
    # Next-day early IDs usually omit shift or carry the following morning label;
    # include by clock time, not shift name.
    overnight_tail = _early_next_day_entry_rows(next_day_rows)
    return list(filtered) + overnight_tail


def _shift_view_payload(
    farm: Any,
    *,
    shift_id: str,
    shift_label: str,
    dates_newest_first: list[date],
    metrics_by_date: dict[date, dict[str, Any]],
) -> dict[str, Any]:
    selected_dates = list(reversed(dates_newest_first[:SUMMARY_DAYS_PER_SHIFT]))
    return {
        "shift_id": shift_id,
        "shift_label": shift_label,
        "date_headers": [
            {"date": d.isoformat(), "label": d.strftime("%a, %b %d")}
            for d in selected_dates
        ],
        "table_rows": _build_metric_table_rows(selected_dates, metrics_by_date, farm=farm),
        "has_data": bool(selected_dates),
        "day_count": len(selected_dates),
    }


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _shift_specs_for_farm(farm: Any) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for option in SHIFT_OPTIONS:
        shift_id, db_values = resolve_shift_filter(option["id"])
        specs.append(
            {
                "id": shift_id,
                "label": option["label"],
                "db_values": {_normalize_shift(v).lower() for v in db_values},
                "overnight": uses_overnight_shift_window(farm.code, shift_id),
            }
        )
    return specs


def _latest_import_text(session: Any, farm_code: str) -> str | None:
    latest_import = (
        session.query(ParlourImportBatch)
        .filter_by(farm_code=farm_code, report_type="milk_flow")
        .order_by(ParlourImportBatch.imported_at.desc())
        .first()
    )
    if latest_import is None or latest_import.imported_at is None:
        return None
    return latest_import.imported_at.strftime("%Y-%m-%d %H:%M")


def _empty_farm_payload(farm: Any, latest_import_text: str | None = None) -> dict[str, Any]:
    return {
        "farm_code": farm.code,
        "farm_name": farm.name,
        "latest_import_at": latest_import_text,
        "shifts": {
            option["id"]: _shift_view_payload(
                farm,
                shift_id=option["id"],
                shift_label=option["label"],
                dates_newest_first=[],
                metrics_by_date={},
            )
            for option in SHIFT_OPTIONS
        },
    }


def _compute_metrics_for_dates(
    session: Any,
    farm: Any,
    dates: list[date],
) -> dict[str, dict[date, dict[str, Any]]]:
    """One-pass chunked compute: each milking date loaded once, split by shift."""
    stall_count = _resolve_stall_count(farm)
    shift_specs = _shift_specs_for_farm(farm)
    result: dict[str, dict[date, dict[str, Any]]] = {spec["id"]: {} for spec in shift_specs}
    if not dates:
        return result

    any_overnight = any(spec["overnight"] for spec in shift_specs)
    for offset in range(0, len(dates), SUMMARY_DATE_CHUNK):
        chunk = dates[offset : offset + SUMMARY_DATE_CHUNK]
        milk_rows = (
            session.query(MilkFlowRecord)
            .filter(
                MilkFlowRecord.farm_code == farm.code,
                MilkFlowRecord.milking_date.in_(chunk),
            )
            .all()
        )
        milk_by_date: dict[date, list[MilkFlowRecord]] = defaultdict(list)
        for row in milk_rows:
            if row.milking_date is not None:
                milk_by_date[row.milking_date].append(row)

        entry_dates = set(chunk)
        if any_overnight:
            entry_dates.update(day + timedelta(days=1) for day in chunk)
        entry_rows = (
            session.query(RotaryEntryIdRecord)
            .filter(
                RotaryEntryIdRecord.farm_code == farm.code,
                RotaryEntryIdRecord.milking_date.in_(entry_dates),
            )
            .all()
        )
        entry_by_date: dict[date, list[RotaryEntryIdRecord]] = defaultdict(list)
        for row in entry_rows:
            if row.milking_date is not None:
                entry_by_date[row.milking_date].append(row)

        for milking_date in chunk:
            day_rows = milk_by_date.get(milking_date, [])
            if not day_rows:
                continue
            for spec in shift_specs:
                filtered = _filter_shift_rows(day_rows, spec["db_values"])
                if not filtered:
                    continue
                entries = _filter_entry_rows(
                    entry_by_date.get(milking_date, []),
                    spec["db_values"],
                )
                if spec["overnight"]:
                    entries = list(entries) + _early_next_day_entry_rows(
                        entry_by_date.get(milking_date + timedelta(days=1), [])
                    )
                result[spec["id"]][milking_date] = _day_metrics(
                    filtered,
                    entries,
                    crosses_midnight=spec["overnight"],
                    stall_count=stall_count,
                )
    return result


def _cache_is_fresh(session: Any, farm_code: str) -> bool:
    latest_import_at = (
        session.query(ParlourImportBatch.imported_at)
        .filter_by(farm_code=farm_code, report_type="milk_flow")
        .order_by(ParlourImportBatch.imported_at.desc())
        .limit(1)
        .scalar()
    )
    latest_cache_at = (
        session.query(MilkingEfficiencyDayCache.computed_at)
        .filter_by(farm_code=farm_code)
        .order_by(MilkingEfficiencyDayCache.computed_at.desc())
        .limit(1)
        .scalar()
    )
    if latest_cache_at is None:
        return False
    if latest_import_at is not None and _as_utc(latest_cache_at) < _as_utc(latest_import_at):
        return False
    return True


def _farms_for_refresh(
    farm_code: str | None,
    farm_codes: list[str] | None,
) -> list[Any]:
    codes: list[str] = []
    if farm_code:
        codes = [farm_code.strip().upper()]
    elif farm_codes:
        codes = [code.strip().upper() for code in farm_codes if code]
    if not codes:
        return list(FARMS)
    farms = []
    for code in codes:
        farm = FARMS_BY_CODE.get(code)
        if farm:
            farms.append(farm)
    return farms


def refresh_efficiency_cache(
    *,
    farm_code: str | None = None,
    farm_codes: list[str] | None = None,
    dates: list[date] | None = None,
    days: int = CACHE_LOOKBACK_DATES,
    force: bool = False,
) -> dict[str, Any]:
    """Recompute day-level metrics and upsert the cache used by the summary page.

    Called after email import/cron so the UI is a cheap read. When `dates` is
    omitted, keeps the newest `days` milking dates per farm.
    """
    init_db()
    written: dict[str, int] = {}
    skipped_fresh: list[str] = []
    now = datetime.now(timezone.utc)

    for farm in _farms_for_refresh(farm_code, farm_codes):
        with get_session() as session:
            has_milk = (
                session.query(MilkFlowRecord.id)
                .filter_by(farm_code=farm.code)
                .first()
            )
            if not has_milk:
                continue
            if not force and dates is None and _cache_is_fresh(session, farm.code):
                skipped_fresh.append(farm.code)
                continue

            if dates is not None:
                target_dates = list(dates)
            else:
                date_rows = (
                    session.query(MilkFlowRecord.milking_date)
                    .filter(MilkFlowRecord.farm_code == farm.code)
                    .distinct()
                    .order_by(MilkFlowRecord.milking_date.desc())
                    .limit(max(1, int(days)))
                    .all()
                )
                target_dates = [row[0] for row in date_rows if row[0] is not None]

            if not target_dates:
                continue

            metrics_by_shift = _compute_metrics_for_dates(session, farm, target_dates)
            session.query(MilkingEfficiencyDayCache).filter(
                MilkingEfficiencyDayCache.farm_code == farm.code,
                MilkingEfficiencyDayCache.milking_date.in_(target_dates),
            ).delete(synchronize_session=False)

            rows_written = 0
            for shift_id, metrics_by_date in metrics_by_shift.items():
                for milking_date, metrics in metrics_by_date.items():
                    session.add(
                        MilkingEfficiencyDayCache(
                            farm_code=farm.code,
                            milking_date=milking_date,
                            shift_id=shift_id,
                            metrics_json=json.dumps(metrics),
                            computed_at=now,
                        )
                    )
                    rows_written += 1
            written[farm.code] = rows_written
            logger.info(
                "Cached milking efficiency for %s: %s date(s), %s row(s)",
                farm.code,
                len(target_dates),
                rows_written,
            )

    return {"farms": written, "skipped_fresh": skipped_fresh}


def _payload_from_cache(farm: Any, *, allow_stale: bool) -> dict[str, Any] | None:
    with get_session() as session:
        latest_import_text = _latest_import_text(session, farm.code)
        has_milk = (
            session.query(MilkFlowRecord.id)
            .filter_by(farm_code=farm.code)
            .first()
        )
        cache_rows = (
            session.query(MilkingEfficiencyDayCache)
            .filter(MilkingEfficiencyDayCache.farm_code == farm.code)
            .order_by(MilkingEfficiencyDayCache.milking_date.desc())
            .all()
        )
        if not cache_rows:
            return None if has_milk else _empty_farm_payload(farm, latest_import_text)
        if not allow_stale and not _cache_is_fresh(session, farm.code):
            return None

        by_shift: dict[str, list[MilkingEfficiencyDayCache]] = defaultdict(list)
        for row in cache_rows:
            by_shift[row.shift_id].append(row)

        shifts = {}
        for option in SHIFT_OPTIONS:
            rows = by_shift.get(option["id"], [])[:SUMMARY_DAYS_PER_SHIFT]
            metrics_by_date: dict[date, dict[str, Any]] = {}
            dates_newest_first: list[date] = []
            for row in rows:
                try:
                    metrics_by_date[row.milking_date] = json.loads(row.metrics_json)
                except json.JSONDecodeError:
                    continue
                dates_newest_first.append(row.milking_date)
            shifts[option["id"]] = _shift_view_payload(
                farm,
                shift_id=option["id"],
                shift_label=option["label"],
                dates_newest_first=dates_newest_first,
                metrics_by_date=metrics_by_date,
            )
        return {
            "farm_code": farm.code,
            "farm_name": farm.name,
            "latest_import_at": latest_import_text,
            "shifts": shifts,
        }


def build_farm_summaries(farm_code: str) -> dict[str, Any]:
    """One payload for a farm: last 7 days of metrics for every shift.

    Reads precomputed cache (filled by cron/import). Computes once on a cache
    miss so the first hit after deploy still works.
    """
    farm = FARMS_BY_CODE.get(farm_code) or FARMS_BY_CODE["ALH"]
    payload = _payload_from_cache(farm, allow_stale=False)
    if payload is not None:
        return payload

    refresh_efficiency_cache(farm_code=farm.code, days=CACHE_LOOKBACK_DATES, force=True)
    payload = _payload_from_cache(farm, allow_stale=True)
    if payload is not None:
        return payload
    return _empty_farm_payload(farm)


def build_seven_day_summary(farm_code: str, shift_id: str) -> dict[str, Any]:
    farm_payload = build_farm_summaries(farm_code)
    shifts = farm_payload["shifts"]
    shift_payload = shifts.get(shift_id) or next(iter(shifts.values()))
    return {
        "farm_code": farm_payload["farm_code"],
        "farm_name": farm_payload["farm_name"],
        "latest_import_at": farm_payload["latest_import_at"],
        **shift_payload,
    }


def build_pen_breakdown(farm_code: str, shift_id: str, milking_date: date) -> dict[str, Any]:
    """Same metrics as the day summary, split by pen (group_number) for one date/shift."""
    farm = FARMS_BY_CODE.get(farm_code) or FARMS_BY_CODE["ALH"]
    shift_label, db_values = resolve_shift_filter(shift_id)
    db_values_normalized = {_normalize_shift(v).lower() for v in db_values}
    overnight = uses_overnight_shift_window(farm.code, shift_id)

    with get_session() as session:
        day_rows = (
            session.query(MilkFlowRecord)
            .filter(
                MilkFlowRecord.farm_code == farm.code,
                MilkFlowRecord.milking_date == milking_date,
            )
            .all()
        )
        filtered = _filter_shift_rows(day_rows, db_values_normalized)
        if not filtered:
            return {
                "farm_code": farm.code,
                "shift_id": shift_label,
                "shift_label": shift_label,
                "date": milking_date.isoformat(),
                "date_label": milking_date.strftime("%a, %b %d"),
                "pen_headers": [],
                "table_rows": [],
                "has_data": False,
            }

        entry_filtered = _load_lag_entry_rows(
            session,
            farm_code=farm.code,
            milking_date=milking_date,
            db_values_normalized=db_values_normalized,
            crosses_midnight=overnight,
        )

        by_pen, corrections = correct_misassigned_pen_cows(filtered)

        pen_keys = sorted(by_pen.keys(), key=_pen_sort_key)
        stall_count = _resolve_stall_count(farm)
        metrics_by_pen = {
            pen: _day_metrics(
                by_pen[pen],
                entry_filtered,
                trim_shift_outliers=True,
                crosses_midnight=overnight,
                stall_count=stall_count,
            )
            for pen in pen_keys
        }
        table_rows = _build_metric_table_rows(pen_keys, metrics_by_pen, farm=farm)
        pen_headers = [{"id": pen, "label": f"Pen {pen}"} for pen in pen_keys]

        return {
            "farm_code": farm.code,
            "shift_id": shift_label,
            "shift_label": shift_label,
            "date": milking_date.isoformat(),
            "date_label": milking_date.strftime("%a, %b %d"),
            "pen_headers": pen_headers,
            "table_rows": table_rows,
            "has_data": bool(pen_keys),
            "corrected_assignments": len(corrections),
            "corrections": corrections,
        }


def _chart_scale_for_kind(kind: str) -> str:
    if kind in {"hm", "ms"}:
        return "duration_seconds"
    if kind == "hms":
        return "clock_seconds"
    if kind == "pct1":
        return "percent"
    return "number"


def _entry_rows_for_date_from_maps(
    by_date_entries: dict[date, list[RotaryEntryIdRecord]],
    milking_date: date,
    db_values_normalized: set[str],
    *,
    crosses_midnight: bool,
) -> list[RotaryEntryIdRecord]:
    filtered = _filter_entry_rows(by_date_entries.get(milking_date, []), db_values_normalized)
    if not crosses_midnight:
        return filtered
    next_day = by_date_entries.get(milking_date + timedelta(days=1), [])
    return list(filtered) + _early_next_day_entry_rows(next_day)


def build_metric_trend(
    farm_code: str,
    metric_key: str,
    *,
    days: int = TREND_DAY_COUNT,
    pen: str | None = None,
) -> dict[str, Any]:
    """
    45-day trend for one metric across Morning / Day / Night.

    When `pen` is set, metrics are computed from that pen only (after the same
    wrong-pen reassignment used on the pen breakdown table).
    """
    farm = FARMS_BY_CODE.get(farm_code) or FARMS_BY_CODE["ALH"]
    meta = METRIC_BY_KEY.get(metric_key)
    if meta is None:
        return {
            "error": f"Unknown metric: {metric_key}",
            "has_data": False,
        }

    days = max(1, min(int(days), 90))
    kind = meta["kind"]
    pen_id = (pen or "").strip() or None
    pen_label = f"Pen {pen_id}" if pen_id and pen_id != "Unknown" else (pen_id or None)

    empty = {
        "farm_code": farm.code,
        "metric_key": metric_key,
        "metric_label": meta["label"],
        "kind": kind,
        "chart_scale": _chart_scale_for_kind(kind),
        "days": days,
        "pen": pen_id,
        "pen_label": pen_label,
        "dates": [],
        "date_labels": [],
        "series": [],
        "has_data": False,
    }

    if metric_key in ROTARY_ONLY_METRIC_KEYS and not _resolve_stall_count(farm):
        return empty

    with get_session() as session:
        latest = (
            session.query(MilkFlowRecord.milking_date)
            .filter(MilkFlowRecord.farm_code == farm.code)
            .order_by(MilkFlowRecord.milking_date.desc())
            .limit(1)
            .scalar()
        )
        if latest is None:
            return empty

        start_date = latest - timedelta(days=days - 1)
        end_date = latest
        # Overnight lag matching may need the day after the window.
        entry_end = end_date + timedelta(days=1)

        milk_rows = (
            session.query(MilkFlowRecord)
            .filter(
                MilkFlowRecord.farm_code == farm.code,
                MilkFlowRecord.milking_date >= start_date,
                MilkFlowRecord.milking_date <= end_date,
            )
            .all()
        )
        entry_rows = (
            session.query(RotaryEntryIdRecord)
            .filter(
                RotaryEntryIdRecord.farm_code == farm.code,
                RotaryEntryIdRecord.milking_date >= start_date,
                RotaryEntryIdRecord.milking_date <= entry_end,
            )
            .all()
        )

    milk_by_date: dict[date, list[MilkFlowRecord]] = defaultdict(list)
    for row in milk_rows:
        if row.milking_date is not None:
            milk_by_date[row.milking_date].append(row)

    entry_by_date: dict[date, list[RotaryEntryIdRecord]] = defaultdict(list)
    for row in entry_rows:
        if row.milking_date is not None:
            entry_by_date[row.milking_date].append(row)

    dates = [start_date + timedelta(days=offset) for offset in range(days)]
    date_labels = [d.strftime("%d %b") for d in dates]
    series = []

    for shift in SHIFT_OPTIONS:
        shift_id = shift["id"]
        _, db_values = resolve_shift_filter(shift_id)
        db_values_normalized = {_normalize_shift(v).lower() for v in db_values}
        overnight = uses_overnight_shift_window(farm.code, shift_id)
        values: list[float | None] = []

        for milking_date in dates:
            day_milk = _filter_shift_rows(milk_by_date.get(milking_date, []), db_values_normalized)
            if not day_milk:
                values.append(None)
                continue

            day_entries = _entry_rows_for_date_from_maps(
                entry_by_date,
                milking_date,
                db_values_normalized,
                crosses_midnight=overnight,
            )
            stall_count = _resolve_stall_count(farm)

            if pen_id is not None:
                by_pen, _ = correct_misassigned_pen_cows(day_milk)
                pen_rows = by_pen.get(pen_id) or []
                if not pen_rows:
                    values.append(None)
                    continue
                metrics = _day_metrics(
                    pen_rows,
                    day_entries,
                    trim_shift_outliers=True,
                    crosses_midnight=overnight,
                    stall_count=stall_count,
                )
            else:
                metrics = _day_metrics(
                    day_milk,
                    day_entries,
                    crosses_midnight=overnight,
                    stall_count=stall_count,
                )

            raw = metrics.get(metric_key)
            values.append(float(raw) if raw is not None else None)

        series.append(
            {
                "shift_id": shift_id,
                "shift_label": shift["label"],
                "color": SHIFT_TREND_COLORS.get(shift_id, "#355f7a"),
                "values": values,
            }
        )

    has_data = any(value is not None for item in series for value in item["values"])
    return {
        "farm_code": farm.code,
        "metric_key": metric_key,
        "metric_label": meta["label"],
        "kind": kind,
        "chart_scale": _chart_scale_for_kind(kind),
        "days": days,
        "pen": pen_id,
        "pen_label": pen_label,
        "dates": [d.isoformat() for d in dates],
        "date_labels": date_labels,
        "series": series,
        "has_data": has_data,
    }
