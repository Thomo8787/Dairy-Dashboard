"""ALH daily reconcile of parlour meters, manual loads, and Müller weighbridge."""

from __future__ import annotations

import calendar
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.database import MilkMeterReading, NmlMilkResult, get_session

FARM = "ALH"
LOAD_COUNT = 5
METER_COUNT = 3
# A gap above this, load against weighbridge, is highlighted.
VARIANCE_LIMIT_PCT = 1.0

_ROW_LABELS = ("Milk", "Fat", "Protein", "SCC", "BCC", "Urea", "Sample no")


def month_key(day: dt.date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def month_label(key: str) -> str:
    year, month = (int(part) for part in key.split("-"))
    return f"{calendar.month_name[month]} {year}"


def shift_month(day: dt.date, months: int) -> dt.date:
    index = day.year * 12 + (day.month - 1) + months
    year, month_index = divmod(index, 12)
    return dt.date(year, month_index + 1, 1)


def default_month_keys(today: dt.date | None = None) -> list[str]:
    """This month first, then the previous month."""
    today = today or dt.date.today()
    current = today.replace(day=1)
    previous = shift_month(current, -1)
    return [month_key(current), month_key(previous)]


def parse_month_keys(raw: list[str] | None, today: dt.date | None = None) -> list[str]:
    """Keep valid YYYY-MM values, newest first. Empty means the default pair."""
    chosen: set[str] = set()
    for item in raw or []:
        text = (item or "").strip()
        if len(text) != 7 or text[4] != "-":
            continue
        year_text, month_text = text.split("-", 1)
        if not (year_text.isdigit() and month_text.isdigit()):
            continue
        year, month = int(year_text), int(month_text)
        if year < 2000 or year > 2100 or month < 1 or month > 12:
            continue
        chosen.add(f"{year:04d}-{month:02d}")
    if not chosen:
        return default_month_keys(today)
    return sorted(chosen, reverse=True)


def _round_pct(value: float) -> int:
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _pct(numerator: float, denominator: float) -> dict[str, Any] | None:
    if denominator == 0:
        return None
    raw = numerator / denominator * 100
    rounded = _round_pct(raw)
    return {
        "label": f"{rounded}%",
        "alert": abs(raw) > VARIANCE_LIMIT_PCT,
    }


def _num(value: float | None, digits: int = 0) -> str:
    if value is None:
        return ""
    number = float(value)
    if digits == 0:
        return str(int(round(number)))
    return f"{number:.{digits}f}"


def _litres(value: float | None) -> float | None:
    if value is None:
        return None
    number = float(value)
    if number <= 0:
        return None
    return number


def build_reconcile(
    db: Session,
    month_keys: list[str],
    *,
    today: dt.date | None = None,
) -> dict[str, Any]:
    today = today or dt.date.today()
    keys = list(month_keys)
    if not keys:
        keys = default_month_keys(today)
    starts = []
    ends = []
    for key in keys:
        year, month = (int(part) for part in key.split("-"))
        starts.append(dt.date(year, month, 1))
        ends.append(dt.date(year, month, calendar.monthrange(year, month)[1]))
    date_min, date_max = min(starts), max(ends)

    rows = db.scalars(
        select(NmlMilkResult)
        .where(
            NmlMilkResult.farm == FARM,
            NmlMilkResult.sample_date >= date_min,
            NmlMilkResult.sample_date <= date_max,
        )
        .order_by(NmlMilkResult.sample_date, NmlMilkResult.load_number, NmlMilkResult.id)
    ).all()
    meters = db.scalars(
        select(MilkMeterReading).where(
            MilkMeterReading.farm == FARM,
            MilkMeterReading.sample_date >= date_min,
            MilkMeterReading.sample_date <= date_max,
        )
    ).all()
    by_day: dict[dt.date, list[NmlMilkResult]] = {}
    for row in rows:
        if row.sample_date is None:
            continue
        by_day.setdefault(row.sample_date, []).append(row)
    meter_by_day = {row.sample_date: row for row in meters if row.sample_date is not None}

    # Always offer this month, last month, every month back to the first ALH
    # collection, and any month the user already asked to see.
    current_month = today.replace(day=1)
    option_start = min(shift_month(current_month, -1), date_min)
    earliest = db.scalar(
        select(NmlMilkResult.sample_date)
        .where(NmlMilkResult.farm == FARM, NmlMilkResult.sample_date.isnot(None))
        .order_by(NmlMilkResult.sample_date)
        .limit(1)
    )
    if isinstance(earliest, dt.date):
        option_start = min(option_start, earliest.replace(day=1))
    options = _month_options(option_start, current_month)

    months = [
        _month_table(key, by_day, meter_by_day, today=today)
        for key in keys
    ]
    return {
        "months": months,
        "selected": keys,
        "options": options,
        "row_labels": list(_ROW_LABELS),
    }


def _month_options(start: dt.date, end: dt.date) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    cursor = end
    while cursor >= start:
        key = month_key(cursor)
        options.append({"key": key, "label": month_label(key)})
        cursor = shift_month(cursor, -1)
    return options


def _month_table(
    key: str,
    by_day: dict[dt.date, list[NmlMilkResult]],
    meter_by_day: dict[dt.date, MilkMeterReading],
    *,
    today: dt.date,
) -> dict[str, Any]:
    year, month = (int(part) for part in key.split("-"))
    last_day = calendar.monthrange(year, month)[1]
    if (year, month) == (today.year, today.month):
        last_day = min(last_day, today.day)
    days = []
    for day_number in range(1, last_day + 1):
        day = dt.date(year, month, day_number)
        days.append(_day_row(day, by_day.get(day, []), meter_by_day.get(day)))
    return {"key": key, "label": month_label(key), "days": days}


def _day_row(
    day: dt.date,
    rows: list[NmlMilkResult],
    meter: MilkMeterReading | None,
) -> dict[str, Any]:
    slots: list[NmlMilkResult | None] = [None] * LOAD_COUNT
    for row in rows:
        number = row.load_number
        if number is None or number < 1 or number > LOAD_COUNT:
            continue
        current = slots[number - 1]
        if current is None or (
            _litres(current.litres_load) is None and _litres(row.litres_load) is not None
        ):
            slots[number - 1] = row

    loads = []
    weights = []
    diffs = []
    diff_pcts = []
    load_sum = 0.0
    weight_sum = 0.0
    has_load = False
    has_weight = False
    for row in slots:
        load = _litres(row.litres_load) if row is not None else None
        weight = _litres(row.litres_weighbridge) if row is not None else None
        loads.append(_load_cells(row, load))
        weights.append(_num(weight) if weight is not None else "")
        if load is not None:
            load_sum += load
            has_load = True
        if weight is not None:
            weight_sum += weight
            has_weight = True
        if load is not None and weight is not None:
            diff = weight - load
            diffs.append(_num(diff))
            diff_pcts.append(_pct(diff, load))
        else:
            diffs.append("")
            diff_pcts.append(None)

    meter_values = [None, None, None]
    if meter is not None:
        meter_values = [meter.morning_reading, meter.afternoon_reading, meter.evening_reading]
    meter_numbers = [None if value is None else float(value) for value in meter_values]
    meter_sum = sum(value for value in meter_numbers if value is not None)
    has_meter = any(value is not None for value in meter_numbers)

    return {
        "day": day.day,
        "meters": [_num(value) if value is not None else "" for value in meter_numbers],
        "loads": loads,
        "weights": weights,
        "diffs": diffs,
        "diff_pcts": diff_pcts,
        "meter_total": _num(meter_sum) if has_meter else "",
        "load_total": _num(load_sum) if has_load else "",
        "weight_total": _num(weight_sum) if has_weight else "",
        "total_pct": _pct(load_sum - weight_sum, weight_sum) if has_load and has_weight else None,
    }


def _load_cells(row: NmlMilkResult | None, load: float | None) -> dict[str, str]:
    if row is None:
        return {label: "" for label in ("milk", "fat", "protein", "scc", "bcc", "urea", "sample")}
    sample = ""
    if not row.sample_missing and row.sample_id:
        sample = str(row.sample_id)
    return {
        "milk": _num(load) if load is not None else "",
        "fat": _num(row.butterfat_pct, 2) + "%" if row.butterfat_pct is not None else "",
        "protein": _num(row.protein_pct, 2) + "%" if row.protein_pct is not None else "",
        "scc": _num(row.scc) if row.scc is not None else "",
        "bcc": _num(row.bactoscan) if row.bactoscan is not None else "",
        "urea": _num(row.urea_pct, 3) if row.urea_pct is not None else "",
        "sample": sample,
    }


def reconcile_page(month_args: list[str] | None, today: dt.date | None = None) -> dict[str, Any]:
    today = today or dt.date.today()
    keys = parse_month_keys(month_args, today)
    with get_session() as db:
        return build_reconcile(db, keys, today=today)
