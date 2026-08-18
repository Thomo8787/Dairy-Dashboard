"""Birth records report from herd_births (CMBORN / GADBORN)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import and_, case, extract, func, literal, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from services.database import HerdBirth
from services.farms import HERD_FARM_OPTIONS
from services.events_common import (
    _build_range_summary,
    _clamp_date,
    _empty_range_summary,
    _farm_zero_counts,
    _fiscal_year_calendar_bounds,
    _iter_month_starts,
    _month_count_inclusive,
    _row_from_farm_counts,
    _sort_key_from_date,
    normalize_farms,
)
from services.herd_import_utils import BEEF_CBREED_MIN, CATEGORY_BEEF, CATEGORY_DAIRY

BIRTH_CATEGORY_ORDER: tuple[str, ...] = (CATEGORY_DAIRY, CATEGORY_BEEF)


def normalize_birth_categories(categories: list[str] | None) -> list[str]:
    if not categories:
        return list(BIRTH_CATEGORY_ORDER)
    selected: list[str] = []
    for value in categories:
        normalized = value.strip().title()
        if normalized == CATEGORY_DAIRY and CATEGORY_DAIRY not in selected:
            selected.append(CATEGORY_DAIRY)
        elif normalized == CATEGORY_BEEF and CATEGORY_BEEF not in selected:
            selected.append(CATEGORY_BEEF)
    return selected or list(BIRTH_CATEGORY_ORDER)


def _effective_category_expression() -> ColumnElement:
    computed_dairy = and_(
        HerdBirth.cbrd.isnot(None),
        HerdBirth.cbrd < BEEF_CBREED_MIN,
        func.upper(func.coalesce(HerdBirth.gndr, "")) == "F",
    )
    return case(
        (HerdBirth.category == CATEGORY_DAIRY, literal(CATEGORY_DAIRY)),
        (HerdBirth.category == CATEGORY_BEEF, literal(CATEGORY_BEEF)),
        (computed_dairy, literal(CATEGORY_DAIRY)),
        else_=literal(CATEGORY_BEEF),
    )


def _empty_category_range_summary() -> dict[str, Any]:
    return {
        "total": 0,
        "month_count": 0,
        "average_per_month": 0,
        CATEGORY_DAIRY: {"total": 0, "average_per_month": 0},
        CATEGORY_BEEF: {"total": 0, "average_per_month": 0},
    }


def _build_category_range_summary(
    grand_dairy: int,
    grand_beef: int,
    month_count: int,
) -> dict[str, Any]:
    def avg(total: int) -> float:
        return round(total / month_count, 1) if month_count else 0.0

    grand_total = grand_dairy + grand_beef
    return {
        "total": grand_total,
        "month_count": month_count,
        "average_per_month": avg(grand_total),
        CATEGORY_DAIRY: {"total": grand_dairy, "average_per_month": avg(grand_dairy)},
        CATEGORY_BEEF: {"total": grand_beef, "average_per_month": avg(grand_beef)},
    }


def _get_birth_date_bounds(db: Session) -> tuple[dt.date | None, dt.date | None]:
    row = db.execute(
        select(func.min(HerdBirth.bdat), func.max(HerdBirth.bdat))
        .where(HerdBirth.bdat.isnot(None))
        .where(HerdBirth.farm.in_(HERD_FARM_OPTIONS))
    ).one()
    min_date, max_date = row[0], row[1]
    if min_date is None or max_date is None:
        return None, None
    if hasattr(min_date, "date"):
        min_date = min_date.date()
    if hasattr(max_date, "date"):
        max_date = max_date.date()
    return min_date, max_date


def _get_birth_fiscal_year_options(db: Session) -> list[int]:
    rows = db.execute(
        select(HerdBirth.fiscal_year)
        .where(HerdBirth.bdat.isnot(None))
        .where(HerdBirth.farm.in_(HERD_FARM_OPTIONS))
        .where(HerdBirth.fiscal_year.isnot(None))
        .distinct()
        .order_by(HerdBirth.fiscal_year.desc())
    ).all()
    return [int(row[0]) for row in rows if row[0] is not None]


def _birth_base_filters(
    selected_farms: list[str],
    selected_categories: list[str],
    effective_from: dt.date,
    effective_to: dt.date,
    fiscal_year: int | None,
):
    category_expr = _effective_category_expression()
    filters = [
        HerdBirth.bdat.isnot(None),
        HerdBirth.farm.in_(selected_farms),
        HerdBirth.bdat >= effective_from,
        HerdBirth.bdat <= effective_to,
        category_expr.in_(selected_categories),
    ]
    if fiscal_year is not None:
        filters.append(HerdBirth.fiscal_year == fiscal_year)
    return filters


def _month_label_from_parts(year: int, month: int) -> str:
    return dt.date(year, month, 1).strftime("%b-%y")


def _build_farm_pivot(
    db: Session,
    *,
    selected_farms: list[str],
    selected_categories: list[str],
    effective_from: dt.date,
    effective_to: dt.date,
    fiscal_year: int | None,
) -> dict[str, dict[str, int]]:
    year_expr = extract("year", HerdBirth.bdat)
    month_expr = extract("month", HerdBirth.bdat)
    counts = db.execute(
        select(year_expr, month_expr, HerdBirth.farm, func.count())
        .where(
            *_birth_base_filters(
                selected_farms,
                selected_categories,
                effective_from,
                effective_to,
                fiscal_year,
            )
        )
        .group_by(year_expr, month_expr, HerdBirth.farm)
        .order_by(year_expr, month_expr)
    ).all()

    pivot: dict[str, dict[str, int]] = {}
    for year, month, farm, count in counts:
        if year is None or month is None or farm not in HERD_FARM_OPTIONS:
            continue
        event_month = _month_label_from_parts(int(year), int(month))
        pivot.setdefault(event_month, _farm_zero_counts())
        if farm in pivot[event_month]:
            pivot[event_month][farm] = int(count)
    return pivot


def _build_category_pivot(
    db: Session,
    *,
    selected_farms: list[str],
    selected_categories: list[str],
    effective_from: dt.date,
    effective_to: dt.date,
    fiscal_year: int | None,
) -> dict[str, dict[str, int]]:
    year_expr = extract("year", HerdBirth.bdat)
    month_expr = extract("month", HerdBirth.bdat)
    category_expr = _effective_category_expression()
    counts = db.execute(
        select(year_expr, month_expr, category_expr, func.count())
        .where(
            *_birth_base_filters(
                selected_farms,
                selected_categories,
                effective_from,
                effective_to,
                fiscal_year,
            )
        )
        .group_by(year_expr, month_expr, category_expr)
        .order_by(year_expr, month_expr)
    ).all()

    pivot: dict[str, dict[str, int]] = {}
    for year, month, category, count in counts:
        if year is None or month is None or category not in BIRTH_CATEGORY_ORDER:
            continue
        event_month = _month_label_from_parts(int(year), int(month))
        pivot.setdefault(event_month, {CATEGORY_DAIRY: 0, CATEGORY_BEEF: 0})
        pivot[event_month][category] = int(count)
    return pivot


def _build_farm_category_pivot(
    db: Session,
    *,
    selected_farms: list[str],
    selected_categories: list[str],
    effective_from: dt.date,
    effective_to: dt.date,
    fiscal_year: int | None,
) -> dict[str, dict[str, dict[str, int]]]:
    year_expr = extract("year", HerdBirth.bdat)
    month_expr = extract("month", HerdBirth.bdat)
    category_expr = _effective_category_expression()
    counts = db.execute(
        select(year_expr, month_expr, HerdBirth.farm, category_expr, func.count())
        .where(
            *_birth_base_filters(
                selected_farms,
                selected_categories,
                effective_from,
                effective_to,
                fiscal_year,
            )
        )
        .group_by(year_expr, month_expr, HerdBirth.farm, category_expr)
        .order_by(year_expr, month_expr)
    ).all()

    pivot: dict[str, dict[str, dict[str, int]]] = {}
    for year, month, farm, category, count in counts:
        if year is None or month is None or farm not in selected_farms:
            continue
        if category not in selected_categories:
            continue
        event_month = _month_label_from_parts(int(year), int(month))
        pivot.setdefault(event_month, {})
        pivot[event_month].setdefault(farm, {cat: 0 for cat in selected_categories})
        pivot[event_month][farm][category] = int(count)
    return pivot


def _empty_farm_category_counts(selected_categories: list[str]) -> dict[str, int]:
    return {cat: 0 for cat in selected_categories}


def _zero_fill_detail_table_rows(
    pivot: dict[str, dict[str, dict[str, int]]],
    event_from: dt.date,
    event_to: dt.date,
    selected_farms: list[str],
    selected_categories: list[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for month_start in _iter_month_starts(event_from, event_to):
        event_month = month_start.strftime("%b-%y")
        month_counts = pivot.get(event_month, {})
        row: dict[str, Any] = {
            "event_month": event_month,
            "sort_key": _sort_key_from_date(month_start),
        }
        for farm in selected_farms:
            farm_counts = month_counts.get(farm, _empty_farm_category_counts(selected_categories))
            row[farm] = {cat: farm_counts.get(cat, 0) for cat in selected_categories}
        rows.append(row)
    return rows


def _build_table_grand_total(
    detail_rows: list[dict[str, Any]],
    selected_farms: list[str],
    selected_categories: list[str],
) -> dict[str, Any]:
    by_farm: dict[str, dict[str, int]] = {
        farm: {cat: 0 for cat in selected_categories} for farm in selected_farms
    }
    combined: dict[str, int] = {cat: 0 for cat in selected_categories}

    for row in detail_rows:
        for farm in selected_farms:
            farm_data = row.get(farm, {})
            for cat in selected_categories:
                value = int(farm_data.get(cat, 0))
                by_farm[farm][cat] += value
                combined[cat] += value

    result: dict[str, Any] = {"farms": {}, "combined": combined}
    for farm in selected_farms:
        farm_cats = by_farm[farm]
        result["farms"][farm] = {
            **farm_cats,
            "total": sum(farm_cats.values()),
        }
    result["combined_total"] = sum(combined.values())
    return result


def _zero_fill_farm_rows(
    pivot: dict[str, dict[str, int]],
    event_from: dt.date,
    event_to: dt.date,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for month_start in _iter_month_starts(event_from, event_to):
        event_month = month_start.strftime("%b-%y")
        counts = pivot.get(event_month, _farm_zero_counts())
        rows.append(
            _row_from_farm_counts(
                counts,
                {
                    "event_month": event_month,
                    "sort_key": _sort_key_from_date(month_start),
                },
            )
        )
    return rows


def _zero_fill_category_rows(
    pivot: dict[str, dict[str, int]],
    event_from: dt.date,
    event_to: dt.date,
    selected_categories: list[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for month_start in _iter_month_starts(event_from, event_to):
        event_month = month_start.strftime("%b-%y")
        counts = pivot.get(event_month, {CATEGORY_DAIRY: 0, CATEGORY_BEEF: 0})
        dairy = counts.get(CATEGORY_DAIRY, 0) if CATEGORY_DAIRY in selected_categories else 0
        beef = counts.get(CATEGORY_BEEF, 0) if CATEGORY_BEEF in selected_categories else 0
        rows.append(
            {
                "event_month": event_month,
                "sort_key": _sort_key_from_date(month_start),
                CATEGORY_DAIRY: dairy,
                CATEGORY_BEEF: beef,
                "total": dairy + beef,
            }
        )
    return rows


def build_births_report(
    db: Session,
    *,
    farms: list[str] | None = None,
    categories: list[str] | None = None,
    event_from: dt.date | None = None,
    event_to: dt.date | None = None,
    fiscal_year: int | None = None,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)
    selected_categories = normalize_birth_categories(categories)
    latest_import = db.scalar(select(func.max(HerdBirth.import_timestamp)))

    empty_result: dict[str, Any] = {
        "rows": [],
        "detail_table_rows": [],
        "grand_total": {**_farm_zero_counts(), "total": 0},
        "table_grand_total": {"farms": {}, "combined": {}, "combined_total": 0},
        "category_grand_total": {CATEGORY_DAIRY: 0, CATEGORY_BEEF: 0, "total": 0},
        "range_summary": _empty_range_summary(),
        "category_range_summary": _empty_category_range_summary(),
        "fiscal_year_options": [],
        "selected_categories": selected_categories,
        "category_options": [
            {"id": CATEGORY_DAIRY, "label": CATEGORY_DAIRY},
            {"id": CATEGORY_BEEF, "label": CATEGORY_BEEF},
        ],
        "latest_import": latest_import.isoformat() if latest_import else None,
    }

    if not selected_farms:
        return empty_result

    empty_result["fiscal_year_options"] = _get_birth_fiscal_year_options(db)

    bounds_min, bounds_max = _get_birth_date_bounds(db)
    if bounds_min is None or bounds_max is None:
        empty_result["date_bounds"] = None
        return empty_result

    if fiscal_year is not None:
        slider_min, slider_max = _fiscal_year_calendar_bounds(fiscal_year)
    else:
        slider_min, slider_max = bounds_min, bounds_max

    date_bounds = {
        "min": slider_min.isoformat(),
        "max": slider_max.isoformat(),
    }

    effective_from = event_from if event_from is not None else slider_min
    effective_to = event_to if event_to is not None else slider_max
    effective_from = _clamp_date(effective_from, slider_min, slider_max)
    effective_to = _clamp_date(effective_to, slider_min, slider_max)
    if effective_from > effective_to:
        effective_from, effective_to = effective_to, effective_from

    farm_pivot = _build_farm_pivot(
        db,
        selected_farms=selected_farms,
        selected_categories=selected_categories,
        effective_from=effective_from,
        effective_to=effective_to,
        fiscal_year=fiscal_year,
    )
    farm_category_pivot = _build_farm_category_pivot(
        db,
        selected_farms=selected_farms,
        selected_categories=selected_categories,
        effective_from=effective_from,
        effective_to=effective_to,
        fiscal_year=fiscal_year,
    )
    category_pivot = _build_category_pivot(
        db,
        selected_farms=selected_farms,
        selected_categories=selected_categories,
        effective_from=effective_from,
        effective_to=effective_to,
        fiscal_year=fiscal_year,
    )

    rows = _zero_fill_farm_rows(farm_pivot, effective_from, effective_to)
    detail_table_rows = _zero_fill_detail_table_rows(
        farm_category_pivot,
        effective_from,
        effective_to,
        selected_farms,
        selected_categories,
    )
    category_rows = _zero_fill_category_rows(
        category_pivot, effective_from, effective_to, selected_categories
    )

    farm_totals = {
        code: sum(int(row.get(code, 0) or 0) for row in rows) for code in HERD_FARM_OPTIONS
    }
    grand_dairy = sum(row[CATEGORY_DAIRY] for row in category_rows)
    grand_beef = sum(row[CATEGORY_BEEF] for row in category_rows)
    month_count = _month_count_inclusive(effective_from, effective_to)
    table_grand_total = _build_table_grand_total(
        detail_table_rows, selected_farms, selected_categories
    )

    return {
        "rows": rows,
        "detail_table_rows": detail_table_rows,
        "table_grand_total": table_grand_total,
        "grand_total": {**farm_totals, "total": sum(farm_totals.values())},
        "category_grand_total": {
            CATEGORY_DAIRY: grand_dairy,
            CATEGORY_BEEF: grand_beef,
            "total": grand_dairy + grand_beef,
        },
        "date_bounds": date_bounds,
        "range_summary": _build_range_summary(farm_totals, month_count),
        "category_range_summary": _build_category_range_summary(
            grand_dairy, grand_beef, month_count
        ),
        "fiscal_year_options": empty_result["fiscal_year_options"],
        "selected_categories": selected_categories,
        "category_options": empty_result["category_options"],
        "latest_import": latest_import.isoformat() if latest_import else None,
    }
