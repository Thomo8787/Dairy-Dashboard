"""Stall Issues: milking-point outliers across shifts (Cwrt Malle rules)."""

from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select

from services.database import MilkFlowRecord, get_session
from services.farms import FARMS_BY_CODE
from services.milking_efficiency_summary import (
    SHIFT_OPTIONS,
    _day_metrics,
    _normalize_milking_point,
)

OUTLIER_SD = 2.0
OUTLIER_MIN_N = 5
MAX_STALL_ISSUES_SPAN_DAYS = 31
MAX_STALL_DETAIL_SPAN_DAYS = 4
STALL_DETAIL_SHIFTS = ("Morning", "Day", "Night")
SHIFT_SEQUENCE = STALL_DETAIL_SHIFTS

METRIC_OUTLIER_RULES: list[tuple[str, str]] = [
    ("avg_yield_kg", "low"),
    ("high_flow_takeoff_pct", "high"),
    ("bimodal_pct", "high"),
    ("median_milking_duration_seconds", "high"),
    ("avg_milking_duration_seconds", "high"),
    ("avg_flow_15s", "low"),
    ("avg_flow_30s", "low"),
    ("avg_flow_60s", "low"),
    ("avg_flow_120s", "low"),
    ("avg_peak_flow", "low"),
    ("avg_average_flow", "low"),
    ("avg_pct_2_minutes", "low"),
    ("avg_milk_yield_2_minutes", "low"),
    ("avg_flow_rate_at_removal", "high"),
]

STALL_DETAIL_METRIC_KEYS = (
    "yield_kg",
    "avg_yield_kg",
    "cow_count",
    "high_flow_takeoff_pct",
    "avg_flow_rate_at_removal",
    "bimodal_pct",
    "median_milking_duration_seconds",
    "avg_milking_duration_seconds",
    "avg_flow_15s",
    "avg_flow_30s",
    "avg_flow_60s",
    "avg_flow_120s",
    "avg_peak_flow",
    "avg_average_flow",
    "avg_pct_2_minutes",
    "avg_milk_yield_2_minutes",
)

_SHIFT_ALIASES: dict[str, str] = {}
for _opt in SHIFT_OPTIONS:
    for _raw in _opt["db_values"]:
        _SHIFT_ALIASES[_raw.strip().lower()] = _opt["id"]


def _canonical_shift(raw: str | None) -> str | None:
    return _SHIFT_ALIASES.get((raw or "").strip().lower())


def _point_id(raw: str | None) -> int | str | None:
    text = _normalize_milking_point(raw)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return text


def _point_sort_key(point_id: Any) -> tuple[int, int | str]:
    if isinstance(point_id, int):
        return (0, point_id)
    try:
        return (0, int(point_id))
    except (TypeError, ValueError):
        return (1, str(point_id or ""))


def _sample_stats(values: list[float]) -> tuple[float, float] | None:
    if len(values) < OUTLIER_MIN_N:
        return None
    mean = statistics.fmean(values)
    if len(values) < 2:
        return None
    sd = statistics.stdev(values)
    if not sd or sd <= 0:
        return None
    return mean, float(sd)


def _is_outlier_bad(
    value: float | None,
    stats: tuple[float, float] | None,
    bad_direction: str,
) -> bool:
    if value is None or stats is None:
        return False
    mean, sd = stats
    z = (float(value) - mean) / sd
    if bad_direction == "high":
        return z >= OUTLIER_SD
    if bad_direction == "low":
        return z <= -OUTLIER_SD
    return False


def _previous_shift(milking_date: dt.date, shift: str) -> tuple[dt.date, str]:
    if shift in SHIFT_SEQUENCE:
        idx = SHIFT_SEQUENCE.index(shift)
    else:
        idx = 0
    if idx > 0:
        return milking_date, SHIFT_SEQUENCE[idx - 1]
    return milking_date - dt.timedelta(days=1), SHIFT_SEQUENCE[-1]


def _previous_existing_shift(
    milking_date: dt.date,
    shift: str,
    existing: set[tuple[dt.date, str]],
) -> tuple[dt.date, str] | None:
    date_cur, shift_cur = milking_date, shift
    for _ in range(len(SHIFT_SEQUENCE) * 3):
        date_cur, shift_cur = _previous_shift(date_cur, shift_cur)
        if (date_cur, shift_cur) in existing:
            return date_cur, shift_cur
    return None


def _alert_metrics_for_points(points: list[dict[str, Any]]) -> dict[Any, set[str]]:
    alerts: dict[Any, set[str]] = defaultdict(set)
    if len(points) < OUTLIER_MIN_N:
        return alerts
    for metric, bad_direction in METRIC_OUTLIER_RULES:
        values = [float(p[metric]) for p in points if p.get(metric) is not None]
        stats = _sample_stats(values)
        if stats is None:
            continue
        for p in points:
            raw = p.get(metric)
            if raw is None:
                continue
            if _is_outlier_bad(float(raw), stats, bad_direction):
                alerts[p.get("milking_point")].add(metric)
    return alerts


def _annotate_milking_point_outliers(
    by_shift: dict[tuple[dt.date, str], list[dict[str, Any]]],
) -> dict[tuple[dt.date, str], int]:
    """alert = ≥2 SD bad this shift; problem = same metric also alerted last milked shift."""
    alert_by_shift: dict[tuple[dt.date, str], dict[Any, set[str]]] = {}
    for key, points in by_shift.items():
        alert_by_shift[key] = _alert_metrics_for_points(points)

    existing_keys = set(by_shift.keys())
    problem_counts: dict[tuple[dt.date, str], int] = {}
    for key, points in by_shift.items():
        milking_date, shift = key
        prev_key = _previous_existing_shift(milking_date, shift, existing_keys)
        prev_alerts = alert_by_shift.get(prev_key, {}) if prev_key else {}
        cur_alerts = alert_by_shift.get(key, {})
        problem_points: set[Any] = set()
        for p in points:
            point_id = p.get("milking_point")
            flags: dict[str, str] = {}
            for metric in cur_alerts.get(point_id, set()):
                if metric in prev_alerts.get(point_id, set()):
                    flags[metric] = "problem"
                    problem_points.add(point_id)
                else:
                    flags[metric] = "alert"
            p["outlier_flags"] = flags
        problem_counts[key] = len(problem_points)
    return problem_counts


def _stall_point_from_metrics(point_id: Any, metrics: dict[str, Any]) -> dict[str, Any]:
    cows = metrics.get("total_cows")
    median_s = metrics.get("median_unit_on_s")
    avg_s = metrics.get("avg_unit_on_s")
    return {
        "milking_point": point_id,
        "yield_kg": metrics.get("total_yield_l"),
        "avg_yield_kg": metrics.get("avg_yield_l"),
        "cow_count": int(cows) if cows is not None else None,
        "high_flow_takeoff_pct": metrics.get("high_flow_takeoff_pct"),
        "avg_flow_rate_at_removal": metrics.get("avg_takeoff_flow"),
        "bimodal_pct": metrics.get("bimodal_pct"),
        "median_milking_duration_seconds": median_s,
        "avg_milking_duration_seconds": avg_s,
        "avg_flow_15s": metrics.get("avg_15s_flow"),
        "avg_flow_30s": metrics.get("avg_30s_flow"),
        "avg_flow_60s": metrics.get("avg_60s_flow"),
        "avg_flow_120s": metrics.get("avg_120s_flow"),
        "avg_peak_flow": metrics.get("avg_peak_flow"),
        "avg_average_flow": metrics.get("avg_flow"),
        "avg_pct_2_minutes": metrics.get("avg_pct_2min"),
        "avg_milk_yield_2_minutes": metrics.get("avg_yield_2min_l"),
    }


def latest_milking_date(farm_code: str | None = None) -> dt.date | None:
    with get_session() as session:
        query = select(func.max(MilkFlowRecord.milking_date))
        if farm_code:
            query = query.where(MilkFlowRecord.farm_code == farm_code)
        return session.scalar(query)


def _resolve_stall_issue_dates(
    *,
    farm_key: str,
    date_from: dt.date | None,
    date_to: dt.date | None,
) -> tuple[dt.date, dt.date] | None:
    if date_to is None:
        date_to = latest_milking_date(farm_key)
    if date_to is None:
        return None
    if date_from is None:
        date_from = date_to - dt.timedelta(days=3)
    if date_from > date_to:
        raise ValueError("date_from must be on or before date_to")
    return date_from, date_to


def _annotated_stall_shifts(
    *,
    farm_key: str,
    date_from: dt.date,
    date_to: dt.date,
) -> dict[tuple[dt.date, str], list[dict[str, Any]]]:
    """Milking-point summaries per shift, with alert/problem flags.

    Loads one extra calendar day so Morning can compare to the previous Night.
    """
    query_from = date_from - dt.timedelta(days=1)
    with get_session() as session:
        rows = (
            session.query(MilkFlowRecord)
            .filter(
                MilkFlowRecord.farm_code == farm_key,
                MilkFlowRecord.milking_date >= query_from,
                MilkFlowRecord.milking_date <= date_to,
            )
            .all()
        )

    grouped: dict[tuple[dt.date, str], dict[Any, list[MilkFlowRecord]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in rows:
        shift_id = _canonical_shift(row.shift)
        if shift_id is None or shift_id not in STALL_DETAIL_SHIFTS:
            continue
        point_id = _point_id(row.milking_point)
        if point_id is None:
            continue
        grouped[(row.milking_date, shift_id)][point_id].append(row)

    by_shift: dict[tuple[dt.date, str], list[dict[str, Any]]] = {}
    for key, by_point in grouped.items():
        points: list[dict[str, Any]] = []
        for point_id, stall_rows in by_point.items():
            metrics = _day_metrics(stall_rows)
            if not metrics:
                continue
            points.append(_stall_point_from_metrics(point_id, metrics))
        if points:
            by_shift[key] = points

    _annotate_milking_point_outliers(by_shift)
    return by_shift


def list_stall_issues(
    *,
    farm: str,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
) -> dict[str, Any]:
    """Matrix of milking points × days: count of shifts each stall was a problem."""
    farm_key = farm.upper()
    if farm_key not in FARMS_BY_CODE:
        farm_key = "ALH"
    resolved = _resolve_stall_issue_dates(
        farm_key=farm_key, date_from=date_from, date_to=date_to
    )
    if resolved is None:
        return {
            "farm": farm_key,
            "date_from": None,
            "date_to": None,
            "dates": [],
            "rows": [],
        }
    date_from, date_to = resolved
    if (date_to - date_from).days > MAX_STALL_ISSUES_SPAN_DAYS:
        raise ValueError("Stall Issues date range cannot exceed 31 days.")

    by_shift = _annotated_stall_shifts(
        farm_key=farm_key, date_from=date_from, date_to=date_to
    )

    dates = [
        date_from + dt.timedelta(days=offset)
        for offset in range((date_to - date_from).days + 1)
    ]
    date_isos = [d.isoformat() for d in dates]

    counts: dict[Any, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    points_seen: set[Any] = set()

    for (milking_date, _shift), points in by_shift.items():
        if milking_date < date_from or milking_date > date_to:
            continue
        date_iso = milking_date.isoformat()
        for point in points:
            point_id = point.get("milking_point")
            if point_id is None:
                continue
            points_seen.add(point_id)
            flags = point.get("outlier_flags") or {}
            if any(flag == "problem" for flag in flags.values()):
                counts[point_id][date_iso] += 1

    rows: list[dict[str, Any]] = []
    for point_id in sorted(points_seen, key=_point_sort_key):
        by_date = {d: int(counts[point_id].get(d, 0)) for d in date_isos}
        rows.append(
            {
                "milking_point": point_id,
                "by_date": by_date,
                "total": sum(by_date.values()),
            }
        )

    return {
        "farm": farm_key,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "dates": date_isos,
        "rows": rows,
    }


def _mean_sd(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "sd": None}
    mean = float(statistics.fmean(values))
    sd = float(statistics.stdev(values)) if len(values) >= 2 else None
    return {"mean": mean, "sd": sd}


def list_stall_metric_history(
    *,
    farm: str,
    milking_point: int | str,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
) -> dict[str, Any]:
    """Per-shift metrics and outlier flags for one stall over a short window."""
    farm_key = farm.upper()
    if farm_key not in FARMS_BY_CODE:
        farm_key = "ALH"
    try:
        point_id: int | str = int(milking_point)
    except (TypeError, ValueError):
        point_id = milking_point

    resolved = _resolve_stall_issue_dates(
        farm_key=farm_key, date_from=date_from, date_to=date_to
    )
    if resolved is None:
        return {
            "farm": farm_key,
            "milking_point": point_id,
            "date_from": None,
            "date_to": None,
            "dates": [],
            "shifts": list(STALL_DETAIL_SHIFTS),
            "cells": {},
            "stats": {},
        }
    date_from, date_to = resolved
    if (date_to - date_from).days > MAX_STALL_DETAIL_SPAN_DAYS - 1:
        raise ValueError(
            f"Stall detail date range cannot exceed {MAX_STALL_DETAIL_SPAN_DAYS} days."
        )

    by_shift = _annotated_stall_shifts(
        farm_key=farm_key, date_from=date_from, date_to=date_to
    )

    dates = [
        date_from + dt.timedelta(days=offset)
        for offset in range((date_to - date_from).days + 1)
    ]
    date_isos = [d.isoformat() for d in dates]
    cells: dict[str, dict[str, dict[str, Any]]] = {d: {} for d in date_isos}
    values_by_metric: dict[str, list[float]] = defaultdict(list)

    for (milking_date, shift_name), points in by_shift.items():
        if milking_date < date_from or milking_date > date_to:
            continue
        if shift_name not in STALL_DETAIL_SHIFTS:
            continue
        for point in points:
            for metric in STALL_DETAIL_METRIC_KEYS:
                raw = point.get(metric)
                if raw is None:
                    continue
                try:
                    values_by_metric[metric].append(float(raw))
                except (TypeError, ValueError):
                    continue
        match = next((p for p in points if p.get("milking_point") == point_id), None)
        if match is None:
            continue
        cells[milking_date.isoformat()][shift_name] = match

    stats = {
        metric: _mean_sd(values_by_metric.get(metric, []))
        for metric in STALL_DETAIL_METRIC_KEYS
    }

    return {
        "farm": farm_key,
        "milking_point": point_id,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "dates": date_isos,
        "shifts": list(STALL_DETAIL_SHIFTS),
        "cells": cells,
        "stats": stats,
    }
