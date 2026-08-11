"""7-day milking efficiency summary for the Parlours page."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from statistics import median
from typing import Any

from services.database import MilkFlowRecord, ParlourImportBatch, RotaryEntryIdRecord, get_session
from services.farms import FARMS_BY_CODE
from services.parlour_link import match_milk_flow_to_entry_ids, parse_hms as _parse_hms

# UI shift labels -> possible values stored from DataFlow CSVs
SHIFT_OPTIONS = (
    {"id": "Morning", "label": "Morning", "db_values": ("Morning", "morning")},
    {"id": "Day", "label": "Day", "db_values": ("Noon", "Day", "noon", "day")},
    {"id": "Night", "label": "Night", "db_values": ("Evening", "Night", "Evening ", "evening", "night")},
)

SHIFT_BY_ID = {item["id"]: item for item in SHIFT_OPTIONS}


def _timedelta_to_seconds(value: timedelta | None) -> float | None:
    if value is None:
        return None
    return value.total_seconds()


def _format_hms(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    total = int(round(seconds))
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


def _day_metrics(rows: list[MilkFlowRecord], entry_rows: list[RotaryEntryIdRecord] | None = None) -> dict[str, Any]:
    if not rows:
        return {}

    yields = [r.shift_yield_l for r in rows if r.shift_yield_l is not None]
    unit_on_secs = []
    start_secs = []
    end_secs = []
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
            start_secs.append(start_sec)
            if unit_sec is not None:
                end_secs.append(start_sec + unit_sec)
            if row.milking_point:
                by_point[str(row.milking_point)].append(start_sec)

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

    # Lag phase: milking start − rotary identification (matched cow/date/shift/±5 min).
    lag_secs: list[float] = []
    if entry_rows:
        for match in match_milk_flow_to_entry_ids(rows, entry_rows):
            lag_secs.append(match["lag_seconds"])

    total_cows = len(rows)
    shift_start = min(start_secs) if start_secs else None
    shift_end = max(end_secs) if end_secs else (max(start_secs) if start_secs else None)
    shift_length = None
    if shift_start is not None and shift_end is not None and shift_end >= shift_start:
        shift_length = shift_end - shift_start
        # Handle overnight wrap roughly: if length > 16h, treat as wrap.
        if shift_length > 16 * 3600 and shift_start > shift_end:
            shift_length = (24 * 3600 - shift_start) + shift_end

    cows_per_hour = None
    if shift_length and shift_length > 0:
        cows_per_hour = total_cows / (shift_length / 3600)

    # Rotation approx: median revisit interval on the same milking point.
    revisit_gaps = []
    for starts in by_point.values():
        ordered = sorted(starts)
        gaps = [ordered[i] - ordered[i - 1] for i in range(1, len(ordered)) if ordered[i] >= ordered[i - 1]]
        revisit_gaps.extend(gaps)
    rotation = _median(revisit_gaps)

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


def build_seven_day_summary(farm_code: str, shift_id: str) -> dict[str, Any]:
    farm = FARMS_BY_CODE.get(farm_code) or FARMS_BY_CODE["ALH"]
    shift_label, db_values = resolve_shift_filter(shift_id)
    db_values_normalized = {_normalize_shift(v).lower() for v in db_values}

    with get_session() as session:
        latest_import = (
            session.query(ParlourImportBatch)
            .filter_by(farm_code=farm.code, report_type="milk_flow")
            .order_by(ParlourImportBatch.imported_at.desc())
            .first()
        )
        latest_import_at = latest_import.imported_at if latest_import else None

        # Candidate dates for this farm/shift (most recent first)
        date_rows = (
            session.query(MilkFlowRecord.milking_date)
            .filter(MilkFlowRecord.farm_code == farm.code)
            .distinct()
            .order_by(MilkFlowRecord.milking_date.desc())
            .all()
        )
        all_dates = [row[0] for row in date_rows if row[0] is not None]

        selected_dates: list[date] = []
        metrics_by_date: dict[date, dict[str, Any]] = {}

        for milking_date in all_dates:
            day_rows = (
                session.query(MilkFlowRecord)
                .filter(
                    MilkFlowRecord.farm_code == farm.code,
                    MilkFlowRecord.milking_date == milking_date,
                )
                .all()
            )
            filtered = [
                row
                for row in day_rows
                if _normalize_shift(row.shift).lower() in db_values_normalized
            ]
            if not filtered:
                continue

            entry_rows = (
                session.query(RotaryEntryIdRecord)
                .filter(
                    RotaryEntryIdRecord.farm_code == farm.code,
                    RotaryEntryIdRecord.milking_date == milking_date,
                )
                .all()
            )
            # If entry rows carry a shift, keep ones that match this UI shift filter.
            entry_filtered = []
            for entry in entry_rows:
                entry_shift = _normalize_shift(entry.shift).lower()
                if not entry_shift or entry_shift in db_values_normalized:
                    entry_filtered.append(entry)

            selected_dates.append(milking_date)
            metrics_by_date[milking_date] = _day_metrics(filtered, entry_filtered)
            if len(selected_dates) >= 7:
                break

        # Display oldest -> newest across columns (like the screenshot)
        selected_dates = list(reversed(selected_dates))

        table_rows = []
        for key, label, kind in METRIC_ROWS:
            cells = []
            for milking_date in selected_dates:
                raw = metrics_by_date.get(milking_date, {}).get(key)
                cells.append(
                    {
                        "text": _format_metric(raw, kind),
                        "highlight": kind == "number1_highlight" and raw is not None,
                    }
                )
            table_rows.append({"key": key, "label": label, "cells": cells})

        date_headers = [
            {
                "date": d,
                "label": d.strftime("%a, %b %d"),
            }
            for d in selected_dates
        ]

        return {
            "farm_code": farm.code,
            "farm_name": farm.name,
            "shift_id": shift_label,
            "shift_label": shift_label,
            "date_headers": date_headers,
            "table_rows": table_rows,
            "has_data": bool(selected_dates),
            "latest_import_at": latest_import_at,
            "day_count": len(selected_dates),
        }
