"""Beef inventory report from herd_inventory table."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.database import HerdInventory
from services.expected_due_common import (
    _build_range_summary,
    _empty_grand_total,
    _empty_range_summary,
    normalize_farms,
)
from services.farms import HERD_FARM_OPTIONS
from services.stock_inventory_export import (
    build_age_inventory_csv,
    build_age_inventory_pdf,
)


def get_beef_inventory_report(
    db: Session,
    farms: list[str] | None = None,
    min_age: int | None = None,
    max_age: int | None = None,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)

    bounds_row = db.execute(
        select(
            func.min(HerdInventory.months_old),
            func.max(HerdInventory.months_old),
        )
        .where(HerdInventory.category == "Beef")
        .where(HerdInventory.farm.in_(selected_farms))
        .where(HerdInventory.months_old.isnot(None))
    ).one()

    data_min = int(bounds_row[0]) if bounds_row[0] is not None else 0
    data_max = int(bounds_row[1]) if bounds_row[1] is not None else 0

    effective_min = data_min if min_age is None else max(min_age, data_min)
    effective_max = data_max if max_age is None else min(max_age, data_max)
    latest_import = db.scalar(select(func.max(HerdInventory.import_timestamp)))
    latest_iso = latest_import.isoformat() if latest_import else None

    if effective_min > effective_max:
        return {
            "rows": [],
            "grand_total": _empty_grand_total(),
            "range_summary": _empty_range_summary(),
            "age_bounds": {"min": data_min, "max": data_max},
            "latest_import": latest_iso,
        }

    counts = db.execute(
        select(
            HerdInventory.months_old,
            HerdInventory.farm,
            func.count(),
        )
        .where(HerdInventory.category == "Beef")
        .where(HerdInventory.farm.in_(selected_farms))
        .where(HerdInventory.months_old >= effective_min)
        .where(HerdInventory.months_old <= effective_max)
        .group_by(HerdInventory.months_old, HerdInventory.farm)
        .order_by(HerdInventory.months_old)
    ).all()

    pivot: dict[int, dict[str, int]] = {}
    for months_old, farm, count in counts:
        age = int(months_old)
        pivot.setdefault(age, {code: 0 for code in HERD_FARM_OPTIONS})
        if farm in pivot[age]:
            pivot[age][farm] = int(count)

    rows: list[dict[str, Any]] = []
    farm_totals = {code: 0 for code in HERD_FARM_OPTIONS}
    for age in range(effective_min, effective_max + 1):
        counts_for_age = pivot.get(age, {})
        row: dict[str, Any] = {"months_old": age}
        total = 0
        for code in HERD_FARM_OPTIONS:
            value = int(counts_for_age.get(code, 0) or 0)
            row[code] = value
            farm_totals[code] += value
            total += value
        row["total"] = total
        rows.append(row)

    grand = dict(farm_totals)
    grand["total"] = sum(farm_totals.values())
    age_month_count = effective_max - effective_min + 1
    return {
        "rows": rows,
        "grand_total": grand,
        "range_summary": _build_range_summary(farm_totals, age_month_count),
        "age_bounds": {"min": data_min, "max": data_max},
        "latest_import": latest_iso,
    }


def build_beef_inventory_csv(report: dict[str, Any], selected_farms: list[str]) -> str:
    return build_age_inventory_csv(report, selected_farms)


def build_beef_inventory_pdf(report: dict[str, Any], selected_farms: list[str]) -> bytes:
    return build_age_inventory_pdf(
        report,
        selected_farms,
        title="Beef Inventory",
        empty_message="No beef cattle match the selected filters.",
    )
