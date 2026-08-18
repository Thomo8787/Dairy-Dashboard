"""Beef inventory report from herd_inventory, with optional JV filter."""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.database import CowEvent, HerdInventory
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

JV_MODE_ALL = "all"
JV_MODE_EXCLUDE = "exclude"
JV_MODE_ONLY = "only"
JV_MODES = frozenset({JV_MODE_ALL, JV_MODE_EXCLUDE, JV_MODE_ONLY})
JvMode = Literal["all", "exclude", "only"]

_JV_EVENTS = ("GAME", "PATHWAY")


def animal_key(farm: str, etag: str | None, cow_id: str | None) -> tuple[str, str]:
    ident = (etag or "").strip() or (cow_id or "").strip()
    return (str(farm), ident)


def normalize_jv_mode(jv_mode: str | None) -> JvMode:
    mode = (jv_mode or JV_MODE_ALL).strip().lower()
    if mode not in JV_MODES:
        raise ValueError("jv_mode must be all, exclude, or only")
    return mode  # type: ignore[return-value]


def _jv_animal_keys(db: Session, farms: list[str]) -> set[tuple[str, str]]:
    rows = db.execute(
        select(CowEvent.farm, CowEvent.etag, CowEvent.cow_id)
        .where(CowEvent.farm.in_(farms))
        .where(CowEvent.event.in_(_JV_EVENTS))
    ).all()
    return {animal_key(farm, etag, cow_id) for farm, etag, cow_id in rows}


def _jv_label(jv_mode: JvMode) -> str:
    if jv_mode == JV_MODE_EXCLUDE:
        return "No JV"
    if jv_mode == JV_MODE_ONLY:
        return "JV only"
    return "All (incl. JV)"


def get_beef_inventory_report(
    db: Session,
    farms: list[str] | None = None,
    min_age: int | None = None,
    max_age: int | None = None,
    jv_mode: str | None = JV_MODE_ALL,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)
    mode = normalize_jv_mode(jv_mode)

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
            "jv_mode": mode,
            "jv_label": _jv_label(mode),
            "latest_import": latest_iso,
        }

    animal_rows = db.execute(
        select(
            HerdInventory.months_old,
            HerdInventory.farm,
            HerdInventory.etag,
            HerdInventory.cow_id,
        )
        .where(HerdInventory.category == "Beef")
        .where(HerdInventory.farm.in_(selected_farms))
        .where(HerdInventory.months_old >= effective_min)
        .where(HerdInventory.months_old <= effective_max)
    ).all()

    jv_keys: set[tuple[str, str]] | None = None
    if mode != JV_MODE_ALL:
        jv_keys = _jv_animal_keys(db, selected_farms)

    pivot: dict[int, dict[str, int]] = {}
    for months_old, farm, etag, cow_id in animal_rows:
        if jv_keys is not None:
            is_jv = animal_key(farm, etag, cow_id) in jv_keys
            if mode == JV_MODE_EXCLUDE and is_jv:
                continue
            if mode == JV_MODE_ONLY and not is_jv:
                continue
        age = int(months_old)
        pivot.setdefault(age, {code: 0 for code in HERD_FARM_OPTIONS})
        if farm in pivot[age]:
            pivot[age][farm] += 1

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
        "jv_mode": mode,
        "jv_label": _jv_label(mode),
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
        extra_meta=[f"JV: {report.get('jv_label') or _jv_label(JV_MODE_ALL)}"],
    )
