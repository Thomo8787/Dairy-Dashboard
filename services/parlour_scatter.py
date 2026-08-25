"""Cow-level scatter, attachment bins, and lag-phase XY for Parlours."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from statistics import fmean
from typing import Any

from sqlalchemy import func, select

from services.database import MilkFlowRecord, RotaryEntryIdRecord, get_session
from services.farms import FARMS_BY_CODE
from services.milking_efficiency_summary import (
    SHIFT_BY_ID,
    _early_next_day_entry_rows,
    _filter_entry_rows,
    _normalize_milking_point,
    _timedelta_to_seconds,
    uses_overnight_shift_window,
)
from services.parlour_common import (
    SHIFT_IDS,
    SHIFT_SORT,
    assert_date_span,
    canonical_shift,
    session_timeline,
    wall_clock_ms,
)
from services.parlour_link import match_milk_flow_to_entry_ids, parse_hms

ATTACHMENT_METRIC_KEY = "attachments"
_ATTACHMENT_BIN_SECONDS = 300
_ATTACHMENT_GAP_SECONDS = 300
LAG_PHASE_X_METRIC = "lag_phase_seconds"

SCATTER_METRICS: dict[str, dict[str, Any]] = {
    "yield_l": {"label": "Yield (L)", "unit": "L", "digits": 2, "column": "shift_yield_l"},
    "duration_seconds": {
        "label": "Unit On Time (min)",
        "unit": "min",
        "digits": 1,
        "scale": 1 / 60.0,
    },
    "lag_phase_seconds": {"label": "Lag phase (s)", "unit": "s", "digits": 0},
    "average_flow": {
        "label": "Average Flow",
        "unit": "",
        "digits": 2,
        "column": "avg_milk_flow_l_per_min",
    },
    "peak_flow": {
        "label": "Peak Flow",
        "unit": "",
        "digits": 2,
        "column": "peak_milk_flow_l_per_min",
    },
    "flow_15s": {"label": "15s flow", "unit": "", "digits": 1, "column": "flow_rate_15s_ml_per_min"},
    "flow_30s": {"label": "30s flow", "unit": "", "digits": 1, "column": "flow_rate_30s_ml_per_min"},
    "flow_60s": {"label": "60s flow", "unit": "", "digits": 1, "column": "flow_rate_60s_ml_per_min"},
    "flow_120s": {
        "label": "120s flow",
        "unit": "",
        "digits": 1,
        "column": "flow_rate_120s_ml_per_min",
    },
    "pct_2_minutes": {
        "label": "% in 2 minutes",
        "unit": "%",
        "digits": 1,
        "column": "percentage_yield_at_2_min",
    },
    "milk_yield_2_minutes": {
        "label": "Milk yield at 2 min (L)",
        "unit": "L",
        "digits": 2,
        "column": "milk_yield_at_2_min_l",
    },
    "flow_rate_at_removal": {
        "label": "Takeoff Flow",
        "unit": "",
        "digits": 1,
        "column": "flow_rate_at_removal_ml_per_min",
    },
}

SCATTER_METRIC_KEYS = frozenset(SCATTER_METRICS)
LAG_PHASE_XY_METRICS: dict[str, str] = {
    "lag_vs_pct_2_minutes": "pct_2_minutes",
    "lag_vs_peak_flow": "peak_flow",
    "lag_vs_duration_seconds": "duration_seconds",
    "lag_vs_flow_rate_at_removal": "flow_rate_at_removal",
}
LAG_PHASE_XY_METRIC_KEYS = frozenset(LAG_PHASE_XY_METRICS)
_FLOW_RATE_METRICS = {
    "average_flow",
    "peak_flow",
    "flow_15s",
    "flow_30s",
    "flow_60s",
    "flow_120s",
    "pct_2_minutes",
    "milk_yield_2_minutes",
    "flow_rate_at_removal",
}


def list_scatter_metrics() -> list[dict[str, str]]:
    lag_items = [
        {
            "key": key,
            "label": f"Lag phase vs {SCATTER_METRICS[y_key]['label']}",
            "chart": "lag_xy",
        }
        for key, y_key in LAG_PHASE_XY_METRICS.items()
    ]
    return [
        {"key": ATTACHMENT_METRIC_KEY, "label": "Attachments (5-min bins)", "chart": "bars"},
        *[
            {"key": key, "label": meta["label"], "chart": "scatter"}
            for key, meta in SCATTER_METRICS.items()
        ],
        *lag_items,
    ]


def _farm_key(farm: str | None) -> str:
    key = (farm or "ALH").upper()
    return key if key in FARMS_BY_CODE else "ALH"


def scatter_date_bounds(farm: str | None = None) -> dict[str, str | None]:
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


def _metric_raw(row: MilkFlowRecord, metric: str) -> float | None:
    if metric == "duration_seconds":
        seconds = _timedelta_to_seconds(parse_hms(row.unit_on_time))
        if seconds is None or seconds <= 0:
            return None
        return seconds
    column = SCATTER_METRICS[metric].get("column")
    if not column:
        return None
    raw = getattr(row, column, None)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if metric in _FLOW_RATE_METRICS and (row.shift_yield_l is None or row.shift_yield_l <= 0):
        return None
    return value


def _point_id(row: MilkFlowRecord) -> int | str | None:
    text = _normalize_milking_point(row.milking_point)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return text


def _load_milk_rows(
    farm_key: str,
    date_from: dt.date | None,
    date_to: dt.date | None,
    shifts: list[str] | None,
) -> list[MilkFlowRecord]:
    with get_session() as session:
        query = session.query(MilkFlowRecord).filter(MilkFlowRecord.farm_code == farm_key)
        if date_from:
            query = query.filter(MilkFlowRecord.milking_date >= date_from)
        if date_to:
            query = query.filter(MilkFlowRecord.milking_date <= date_to)
        rows = query.all()
        session.expunge_all()
    wanted = set(shifts) if shifts is not None else set(SHIFT_IDS)
    return [row for row in rows if canonical_shift(row.shift) in wanted]


def _group_sessions(
    rows: list[MilkFlowRecord],
) -> dict[tuple[dt.date, str], list[MilkFlowRecord]]:
    grouped: dict[tuple[dt.date, str], list[MilkFlowRecord]] = defaultdict(list)
    for row in rows:
        shift_id = canonical_shift(row.shift)
        if shift_id is None:
            continue
        grouped[(row.milking_date, shift_id)].append(row)
    return grouped


def _load_lag_by_row(
    farm_key: str,
    sessions: dict[tuple[dt.date, str], list[MilkFlowRecord]],
) -> dict[int, float]:
    if not sessions:
        return {}
    dates = {d for d, _ in sessions}
    extra = {
        d + dt.timedelta(days=1)
        for d, shift_id in sessions
        if uses_overnight_shift_window(farm_key, shift_id)
    }
    with get_session() as session:
        entries = (
            session.query(RotaryEntryIdRecord)
            .filter(
                RotaryEntryIdRecord.farm_code == farm_key,
                RotaryEntryIdRecord.milking_date.in_(dates | extra),
            )
            .all()
        )
        session.expunge_all()
    by_date: dict[dt.date, list[RotaryEntryIdRecord]] = defaultdict(list)
    for entry in entries:
        by_date[entry.milking_date].append(entry)

    lags: dict[int, float] = {}
    for (milking_date, shift_id), milk_rows in sessions.items():
        option = SHIFT_BY_ID.get(shift_id)
        if option is None:
            continue
        db_values = {raw.strip().lower() for raw in option["db_values"]}
        overnight = uses_overnight_shift_window(farm_key, shift_id)
        filtered = _filter_entry_rows(by_date.get(milking_date, []), db_values)
        if overnight:
            filtered = list(filtered) + _early_next_day_entry_rows(
                by_date.get(milking_date + dt.timedelta(days=1), [])
            )
        for match in match_milk_flow_to_entry_ids(
            milk_rows, filtered, crosses_midnight=overnight
        ):
            lag = match["lag_seconds"]
            if lag is None or lag < 0:
                continue
            lags[id(match["milk"])] = float(lag)
    return lags


def _empty_scatter(
    farm_key: str, metric: str, meta: dict[str, Any], bounds: dict[str, str | None]
) -> dict[str, Any]:
    return {
        "farm": farm_key,
        "metric": metric,
        "metric_label": meta["label"],
        "unit": meta["unit"],
        "digits": meta["digits"],
        "date_min": bounds["date_min"],
        "date_max": bounds["date_max"],
        "point_count": 0,
        "truncated": False,
        "points": [],
        "shift_day_averages": [],
    }


def list_scatter_points(
    *,
    farm: str,
    metric: str,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    shifts: list[str] | None = None,
) -> dict[str, Any]:
    if metric not in SCATTER_METRIC_KEYS:
        raise ValueError(f"Unsupported metric: {metric}")
    assert_date_span(date_from, date_to)
    farm_key = _farm_key(farm)
    meta = SCATTER_METRICS[metric]
    scale = float(meta.get("scale") or 1.0)
    digits = int(meta["digits"])
    bounds = scatter_date_bounds(farm_key)
    if shifts is not None and len(shifts) == 0:
        return _empty_scatter(farm_key, metric, meta, bounds)

    rows = _load_milk_rows(farm_key, date_from, date_to, shifts)
    sessions = _group_sessions(rows)
    lags = _load_lag_by_row(farm_key, sessions) if metric == LAG_PHASE_X_METRIC else {}

    points: list[dict[str, Any]] = []
    buckets: dict[tuple[dt.date, str], list[tuple[float, float]]] = defaultdict(list)

    for (milking_date, shift_id), session_rows in sessions.items():
        _overnight, _anchor, abs_by_id = session_timeline(
            session_rows, farm_code=farm_key, shift_id=shift_id
        )
        for row in session_rows:
            abs_s = abs_by_id.get(id(row))
            if abs_s is None:
                continue
            if metric == LAG_PHASE_X_METRIC:
                raw = lags.get(id(row))
            else:
                raw = _metric_raw(row, metric)
            if raw is None:
                continue
            y = float(raw) * scale
            buckets[(milking_date, shift_id)].append((abs_s, y))
            points.append(
                {
                    "x": wall_clock_ms(milking_date, abs_s),
                    "y": round(y, digits + 2),
                    "shift": shift_id,
                    "milking_date": milking_date.isoformat(),
                    "start_seconds": int(abs_s),
                    "cow_id": row.cow_number,
                    "milking_point": _point_id(row),
                }
            )

    shift_day_averages: list[dict[str, Any]] = []
    for milking_date, shift_id in sorted(
        buckets.keys(),
        key=lambda key: (key[0], SHIFT_SORT.get(key[1], 99), key[1]),
    ):
        vals = buckets[(milking_date, shift_id)]
        if not vals:
            continue
        mean_start = float(fmean(s for s, _ in vals))
        mean_y = fmean(y for _, y in vals)
        shift_day_averages.append(
            {
                "x": wall_clock_ms(milking_date, mean_start),
                "y": round(mean_y, digits + 2),
                "shift": shift_id,
                "milking_date": milking_date.isoformat(),
                "n": len(vals),
            }
        )

    return {
        "farm": farm_key,
        "metric": metric,
        "metric_label": meta["label"],
        "unit": meta["unit"],
        "digits": digits,
        "date_min": bounds["date_min"],
        "date_max": bounds["date_max"],
        "point_count": len(points),
        "truncated": False,
        "points": points,
        "shift_day_averages": shift_day_averages,
    }


def list_lag_phase_xy_points(
    *,
    farm: str,
    metric: str,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    shifts: list[str] | None = None,
) -> dict[str, Any]:
    if metric not in LAG_PHASE_XY_METRICS:
        raise ValueError(f"Unsupported lag-phase metric: {metric}")
    assert_date_span(date_from, date_to)
    y_key = LAG_PHASE_XY_METRICS[metric]
    farm_key = _farm_key(farm)
    x_meta = SCATTER_METRICS[LAG_PHASE_X_METRIC]
    y_meta = SCATTER_METRICS[y_key]
    x_scale = float(x_meta.get("scale") or 1.0)
    y_scale = float(y_meta.get("scale") or 1.0)
    x_digits = int(x_meta["digits"])
    y_digits = int(y_meta["digits"])
    bounds = scatter_date_bounds(farm_key)
    metric_label = f"Lag phase vs {y_meta['label']}"
    empty = {
        "farm": farm_key,
        "metric": metric,
        "metric_label": metric_label,
        "chart": "lag_xy",
        "x_metric": LAG_PHASE_X_METRIC,
        "x_label": x_meta["label"],
        "x_unit": x_meta["unit"],
        "y_metric": y_key,
        "y_label": y_meta["label"],
        "unit": y_meta["unit"],
        "digits": y_digits,
        "date_min": bounds["date_min"],
        "date_max": bounds["date_max"],
        "point_count": 0,
        "points": [],
    }
    if shifts is not None and len(shifts) == 0:
        return empty

    rows = _load_milk_rows(farm_key, date_from, date_to, shifts)
    sessions = _group_sessions(rows)
    lags = _load_lag_by_row(farm_key, sessions)
    points: list[dict[str, Any]] = []
    for (milking_date, shift_id), session_rows in sessions.items():
        for row in session_rows:
            raw_x = lags.get(id(row))
            if raw_x is None:
                continue
            raw_y = _metric_raw(row, y_key)
            if raw_y is None:
                continue
            points.append(
                {
                    "x": round(float(raw_x) * x_scale, x_digits + 2),
                    "y": round(float(raw_y) * y_scale, y_digits + 2),
                    "shift": shift_id,
                    "cow_id": row.cow_number,
                    "milking_point": _point_id(row),
                    "milking_date": milking_date.isoformat(),
                }
            )
    return {**empty, "point_count": len(points), "points": points}


def list_attachment_time_bins(
    *,
    farm: str,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    shifts: list[str] | None = None,
    bin_seconds: int = _ATTACHMENT_BIN_SECONDS,
) -> dict[str, Any]:
    assert_date_span(date_from, date_to)
    farm_key = _farm_key(farm)
    bin_width = bin_seconds if bin_seconds and bin_seconds > 0 else _ATTACHMENT_BIN_SECONDS
    bounds = scatter_date_bounds(farm_key)
    empty = {
        "farm": farm_key,
        "metric": ATTACHMENT_METRIC_KEY,
        "metric_label": "Attachments (5-min bins)",
        "chart": "bars",
        "bin_seconds": bin_width,
        "date_min": bounds["date_min"],
        "date_max": bounds["date_max"],
        "attachment_count": 0,
        "gap_count": 0,
        "bins": [],
        "gaps": [],
    }
    if shifts is not None and len(shifts) == 0:
        return empty

    rows = _load_milk_rows(farm_key, date_from, date_to, shifts)
    sessions = _group_sessions(rows)
    counts: dict[tuple[dt.date, int, str], int] = defaultdict(int)
    by_session: dict[tuple[dt.date, str], list[float]] = defaultdict(list)

    for (milking_date, shift_id), session_rows in sessions.items():
        _overnight, _anchor, abs_by_id = session_timeline(
            session_rows, farm_code=farm_key, shift_id=shift_id
        )
        for row in session_rows:
            abs_s = abs_by_id.get(id(row))
            if abs_s is None:
                continue
            start_s = int(abs_s)
            bin_start = (start_s // bin_width) * bin_width
            counts[(milking_date, bin_start, shift_id)] += 1
            by_session[(milking_date, shift_id)].append(float(start_s))

    bins: list[dict[str, Any]] = []
    total = 0
    for milking_date, bin_start, shift_id in sorted(
        counts.keys(),
        key=lambda key: (key[0], key[1], SHIFT_SORT.get(key[2], 99), key[2]),
    ):
        count = counts[(milking_date, bin_start, shift_id)]
        total += count
        bins.append(
            {
                "x": wall_clock_ms(milking_date, bin_start),
                "y": count,
                "shift": shift_id,
                "milking_date": milking_date.isoformat(),
                "bin_seconds": bin_start,
            }
        )

    gaps: list[dict[str, Any]] = []
    for milking_date, shift_id in sorted(
        by_session.keys(),
        key=lambda key: (key[0], SHIFT_SORT.get(key[1], 99), key[1]),
    ):
        starts = sorted(by_session[(milking_date, shift_id)])
        for earlier, later in zip(starts, starts[1:]):
            delta = later - earlier
            if delta <= _ATTACHMENT_GAP_SECONDS:
                continue
            mid = (earlier + later) / 2
            gaps.append(
                {
                    "x": wall_clock_ms(milking_date, mid),
                    "gap_minutes": int(round(delta / 60.0)),
                    "gap_seconds": int(round(delta)),
                    "shift": shift_id,
                    "milking_date": milking_date.isoformat(),
                }
            )

    return {
        **empty,
        "attachment_count": total,
        "gap_count": len(gaps),
        "bins": bins,
        "gaps": gaps,
    }
