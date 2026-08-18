"""Shared expected-due month pivot for stock inventory reports."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.database import HerdInventory
from services.farms import HERD_FARM_OPTIONS


def normalize_farms(farms: list[str] | None) -> list[str]:
    if not farms:
        return list(HERD_FARM_OPTIONS)
    return [f for f in farms if f in HERD_FARM_OPTIONS]


def _farm_zero_counts() -> dict[str, int]:
    return {code: 0 for code in HERD_FARM_OPTIONS}


def _fiscal_year_from_date(value: dt.date) -> int:
    return value.year + 1 if value.month >= 4 else value.year


def _sort_key_from_date(value: dt.date) -> int:
    month = value.month
    fiscal_year = _fiscal_year_from_date(value)
    month_adjusted = month - 3 if month >= 4 else month + 9
    return fiscal_year * 100 + month_adjusted


def _month_start(value: dt.date) -> dt.date:
    return value.replace(day=1)


def _iter_month_starts(start: dt.date, end: dt.date) -> list[dt.date]:
    current = _month_start(start)
    end_month = _month_start(end)
    months: list[dt.date] = []
    while current <= end_month:
        months.append(current)
        if current.month == 12:
            current = dt.date(current.year + 1, 1, 1)
        else:
            current = dt.date(current.year, current.month + 1, 1)
    return months


def _month_count_inclusive(due_from: dt.date, due_to: dt.date) -> int:
    return len(_iter_month_starts(due_from, due_to))


def _apply_report_filters(
    query,
    *,
    category: str | None,
    selected_farms: list[str],
    rc_values: tuple[int, ...] | None = None,
):
    query = (
        query.where(HerdInventory.gender == "Female")
        .where(HerdInventory.expected_due.isnot(None))
        .where(HerdInventory.expected_month.isnot(None))
        .where(HerdInventory.farm.in_(selected_farms))
    )
    if category is not None:
        query = query.where(HerdInventory.category == category)
    if rc_values:
        query = query.where(HerdInventory.rc.in_(list(rc_values)))
    return query


def _build_range_summary(farm_totals: dict[str, int], month_count: int) -> dict[str, Any]:
    def avg(total: int) -> float:
        return round(total / month_count, 1) if month_count else 0.0

    grand_total = sum(farm_totals.values())
    result: dict[str, Any] = {
        "total": grand_total,
        "month_count": month_count,
        "average_per_month": avg(grand_total),
    }
    for code in HERD_FARM_OPTIONS:
        total = int(farm_totals.get(code, 0))
        result[code] = {"total": total, "average_per_month": avg(total)}
    return result


def _empty_range_summary() -> dict[str, Any]:
    return _build_range_summary({code: 0 for code in HERD_FARM_OPTIONS}, 0)


def _empty_grand_total() -> dict[str, int]:
    total = {code: 0 for code in HERD_FARM_OPTIONS}
    total["total"] = 0
    return total


def _get_date_bounds(
    db: Session,
    category: str | None,
    selected_farms: list[str],
    rc_values: tuple[int, ...] | None = None,
) -> tuple[dt.date | None, dt.date | None]:
    query = _apply_report_filters(
        select(
            func.min(HerdInventory.expected_due),
            func.max(HerdInventory.expected_due),
        ),
        category=category,
        selected_farms=selected_farms,
        rc_values=rc_values,
    )
    row = db.execute(query).one()
    min_date = row[0]
    max_date = row[1]
    if min_date is None or max_date is None:
        return None, None
    if hasattr(min_date, "date"):
        min_date = min_date.date()
    if hasattr(max_date, "date"):
        max_date = max_date.date()
    return min_date, max_date


def _get_breed_options(
    db: Session,
    category: str | None,
    selected_farms: list[str],
    rc_values: tuple[int, ...] | None = None,
) -> list[str]:
    query = _apply_report_filters(
        select(HerdInventory.lsbrd),
        category=category,
        selected_farms=selected_farms,
        rc_values=rc_values,
    )
    rows = db.execute(
        query.where(HerdInventory.lsbrd.isnot(None))
        .where(HerdInventory.lsbrd != "")
        .distinct()
        .order_by(HerdInventory.lsbrd)
    ).all()
    return [str(row[0]) for row in rows if row[0]]


def _build_farm_breed_summary(
    db: Session,
    *,
    category: str | None,
    selected_farms: list[str],
    effective_from: dt.date,
    effective_to: dt.date,
    rc_values: tuple[int, ...] | None,
    breeds: list[str] | None,
    month_count: int,
) -> dict[str, Any]:
    def avg(total: int) -> float:
        return round(total / month_count, 1) if month_count else 0.0

    query = _apply_report_filters(
        select(HerdInventory.lsbrd, HerdInventory.farm, func.count()),
        category=category,
        selected_farms=selected_farms,
        rc_values=rc_values,
    )
    query = query.where(HerdInventory.expected_due >= effective_from).where(
        HerdInventory.expected_due <= effective_to
    )
    if breeds:
        query = query.where(HerdInventory.lsbrd.in_(breeds))

    rows = db.execute(
        query.group_by(HerdInventory.lsbrd, HerdInventory.farm).order_by(
            HerdInventory.farm, HerdInventory.lsbrd
        )
    ).all()

    farm_pivot: dict[str, dict[str, int]] = {}
    for lsbrd, farm, count in rows:
        if farm not in selected_farms:
            continue
        breed = str(lsbrd) if lsbrd else "Unknown"
        farm_pivot.setdefault(farm, {})
        farm_pivot[farm][breed] = farm_pivot[farm].get(breed, 0) + int(count)

    farm_sections: list[dict[str, Any]] = []
    grand_total = 0
    for farm in selected_farms:
        breed_counts = farm_pivot.get(farm, {})
        breed_list: list[dict[str, Any]] = []
        farm_total = 0
        for breed in sorted(breed_counts.keys()):
            total = breed_counts[breed]
            farm_total += total
            breed_list.append(
                {
                    "breed": breed,
                    "total": total,
                    "average_per_month": avg(total),
                }
            )
        grand_total += farm_total
        farm_sections.append(
            {
                "farm": farm,
                "breeds": breed_list,
                "total": farm_total,
                "average_per_month": avg(farm_total),
            }
        )

    breed_totals: dict[str, int] = {}
    for farm in selected_farms:
        for breed, count in farm_pivot.get(farm, {}).items():
            breed_totals[breed] = breed_totals.get(breed, 0) + count

    combined_breeds: list[dict[str, Any]] = []
    for breed in sorted(breed_totals.keys()):
        total = breed_totals[breed]
        combined_breeds.append(
            {
                "breed": breed,
                "total": total,
                "average_per_month": avg(total),
            }
        )

    return {
        "month_count": month_count,
        "farms": farm_sections,
        "breeds": combined_breeds,
        "total": grand_total,
        "average_per_month": avg(grand_total),
    }


def _empty_farm_breed_summary() -> dict[str, Any]:
    return {
        "month_count": 0,
        "farms": [],
        "breeds": [],
        "total": 0,
        "average_per_month": 0,
    }


def _zero_fill_rows(
    pivot: dict[tuple[int, str], dict[str, int]],
    due_from: dt.date,
    due_to: dt.date,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for month_start in _iter_month_starts(due_from, due_to):
        sort_key = _sort_key_from_date(month_start)
        expected_month = month_start.strftime("%b-%y")
        key = (sort_key, expected_month)
        counts = pivot.get(key, _farm_zero_counts())
        row: dict[str, Any] = {
            "expected_month": expected_month,
            "sort_key": sort_key,
        }
        total = 0
        for code in HERD_FARM_OPTIONS:
            value = int(counts.get(code, 0) or 0)
            row[code] = value
            total += value
        row["total"] = total
        rows.append(row)
    return rows


def build_expected_due_report(
    db: Session,
    *,
    category: str | None = None,
    farms: list[str] | None = None,
    breeds: list[str] | None = None,
    due_from: dt.date | None = None,
    due_to: dt.date | None = None,
    include_breed_options: bool = False,
    include_farm_breed_summary: bool = False,
    rc_values: tuple[int, ...] | None = None,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)
    latest_import = db.scalar(select(func.max(HerdInventory.import_timestamp)))

    empty_result: dict[str, Any] = {
        "rows": [],
        "grand_total": _empty_grand_total(),
        "range_summary": _empty_range_summary(),
        "latest_import": latest_import.isoformat() if latest_import else None,
    }
    if include_breed_options:
        empty_result["breed_options"] = _get_breed_options(
            db, category, selected_farms, rc_values=rc_values
        )
    if include_farm_breed_summary:
        empty_result["farm_breed_summary"] = _empty_farm_breed_summary()

    if not selected_farms:
        return empty_result

    bounds_min, bounds_max = _get_date_bounds(
        db, category, selected_farms, rc_values=rc_values
    )
    if bounds_min is None or bounds_max is None:
        empty_result["date_bounds"] = None
        if include_breed_options:
            empty_result["breed_options"] = _get_breed_options(
                db, category, selected_farms, rc_values=rc_values
            )
        return empty_result

    date_bounds = {
        "min": bounds_min.isoformat(),
        "max": bounds_max.isoformat(),
    }

    effective_from = due_from if due_from is not None else bounds_min
    effective_to = due_to if due_to is not None else bounds_max
    if effective_from > effective_to:
        effective_from, effective_to = effective_to, effective_from

    query = _apply_report_filters(
        select(
            HerdInventory.sort_key,
            HerdInventory.expected_month,
            HerdInventory.farm,
            func.count(),
        ),
        category=category,
        selected_farms=selected_farms,
        rc_values=rc_values,
    )
    query = query.where(HerdInventory.expected_due >= effective_from).where(
        HerdInventory.expected_due <= effective_to
    )

    if breeds:
        query = query.where(HerdInventory.lsbrd.in_(breeds))

    counts = db.execute(
        query.group_by(
            HerdInventory.sort_key,
            HerdInventory.expected_month,
            HerdInventory.farm,
        ).order_by(HerdInventory.sort_key)
    ).all()

    pivot: dict[tuple[int, str], dict[str, int]] = {}
    for sort_key, expected_month, farm, count in counts:
        if sort_key is None or not expected_month:
            continue
        key = (int(sort_key), str(expected_month))
        pivot.setdefault(key, _farm_zero_counts())
        if farm in pivot[key]:
            pivot[key][farm] = int(count)

    rows = _zero_fill_rows(pivot, effective_from, effective_to)

    farm_totals = {code: sum(int(row.get(code, 0) or 0) for row in rows) for code in HERD_FARM_OPTIONS}
    grand_total = sum(farm_totals.values())
    month_count = _month_count_inclusive(effective_from, effective_to)
    grand = dict(farm_totals)
    grand["total"] = grand_total

    result: dict[str, Any] = {
        "rows": rows,
        "grand_total": grand,
        "date_bounds": date_bounds,
        "range_summary": _build_range_summary(farm_totals, month_count),
        "latest_import": latest_import.isoformat() if latest_import else None,
    }
    if include_breed_options:
        result["breed_options"] = _get_breed_options(
            db, category, selected_farms, rc_values=rc_values
        )
    if include_farm_breed_summary:
        result["farm_breed_summary"] = _build_farm_breed_summary(
            db,
            category=category,
            selected_farms=selected_farms,
            effective_from=effective_from,
            effective_to=effective_to,
            rc_values=rc_values,
            breeds=breeds,
            month_count=month_count,
        )
    return result
