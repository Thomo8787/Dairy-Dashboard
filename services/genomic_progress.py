"""Genomic Progress scatter report (trait vs age in months)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.database import GenomicResult, HerdInventory
from services.events_common import normalize_farms
from services.events_pages import farm_titles
from services.farms import FARM_CHART_COLORS, HERD_FARM_OPTIONS
from services.genomic_import import eartag_match_key

GENOMIC_TRAITS: tuple[dict[str, str], ...] = (
    {"key": "milk_kg", "label": "Milk KG", "field": "milk_kg"},
    {"key": "fat_kg", "label": "Fat KG", "field": "fat_kg"},
    {"key": "protein_kg", "label": "Protein KG", "field": "protein_kg"},
    {"key": "fat_pct", "label": "Fat %", "field": "fat_pct"},
    {"key": "protein_pct", "label": "Protein %", "field": "protein_pct"},
    {"key": "pli", "label": "PLI", "field": "pli"},
    {"key": "cci", "label": "ACI", "field": "cci"},
    {"key": "fertility_index", "label": "Fertility Index", "field": "fertility_index"},
    {"key": "scc", "label": "SCC", "field": "scc"},
    {"key": "life_span", "label": "Life Span", "field": "life_span"},
    {"key": "mastitis", "label": "Mastitis", "field": "mastitis"},
    {"key": "milking_speed", "label": "Milking Speed", "field": "milking_speed"},
    {"key": "type_merit", "label": "Type", "field": "type_merit"},
    {"key": "mammary", "label": "Mammary", "field": "mammary"},
    {"key": "legs_and_feet", "label": "Legs and Feet", "field": "legs_and_feet"},
    {"key": "stature", "label": "Stature", "field": "stature"},
    {"key": "chest_width", "label": "Chest Width", "field": "chest_width"},
    {"key": "body_depth", "label": "Body Depth", "field": "body_depth"},
    {"key": "mature_weight", "label": "Mature Weight", "field": "mature_weight"},
)

_TRAIT_BY_KEY = {t["key"]: t for t in GENOMIC_TRAITS}


def list_traits() -> list[dict[str, str]]:
    return [{"key": t["key"], "label": t["label"]} for t in GENOMIC_TRAITS]


def genetics_template_extras() -> dict[str, Any]:
    return {
        "page_heading": "Genomic Progress",
        "farm_options": list(HERD_FARM_OPTIONS),
        "farm_colors": FARM_CHART_COLORS,
        "farm_titles": farm_titles(),
    }


def _age_days(bdat: dt.date, today: dt.date) -> int:
    return (today - bdat).days


def _inventory_genomic_rows(
    db: Session, selected_farms: list[str]
) -> list[tuple[str, str, dt.date | None, GenomicResult]]:
    """Return (farm, etag, bdat, genomic) for inventory animals with genomic data."""
    genomic_by_key = {row.hbn: row for row in db.scalars(select(GenomicResult)).all()}
    out: list[tuple[str, str, dt.date | None, GenomicResult]] = []
    inventory_rows = db.execute(
        select(HerdInventory.farm, HerdInventory.etag, HerdInventory.bdat).where(
            HerdInventory.farm.in_(selected_farms)
        )
    ).all()
    for farm, etag, bdat in inventory_rows:
        if farm not in selected_farms or not etag:
            continue
        match_key = eartag_match_key(etag)
        if not match_key:
            continue
        genomic = genomic_by_key.get(match_key)
        if genomic is None:
            continue
        out.append((farm, etag, bdat, genomic))
    return out


def build_genomic_progress(
    db: Session,
    *,
    trait: str,
    farms: list[str] | None = None,
) -> dict[str, Any]:
    trait_meta = _TRAIT_BY_KEY.get(trait)
    if trait_meta is None:
        raise ValueError(f"Unknown trait: {trait}")

    selected_farms = normalize_farms(farms)
    if not selected_farms:
        return {
            "trait": trait,
            "trait_label": trait_meta["label"],
            "points": {},
            "y_min": None,
            "y_max": None,
            "count": 0,
        }

    today = dt.date.today()
    field = trait_meta["field"]
    points: dict[str, list[dict[str, Any]]] = {farm: [] for farm in selected_farms}
    y_values: list[float] = []

    for farm, etag, bdat, genomic in _inventory_genomic_rows(db, selected_farms):
        if bdat is None:
            continue
        trait_value = getattr(genomic, field, None)
        if trait_value is None:
            continue
        age_days = _age_days(bdat, today)
        if age_days < 0:
            continue
        points[farm].append(
            {
                "x": age_days,
                "y": float(trait_value),
                "etag": (etag or "").strip(),
                "cow_id": genomic.hbn,
            }
        )
        y_values.append(float(trait_value))

    count = sum(len(values) for values in points.values())
    return {
        "trait": trait,
        "trait_label": trait_meta["label"],
        "points": points,
        "y_min": min(y_values) if y_values else None,
        "y_max": max(y_values) if y_values else None,
        "count": count,
    }


def build_genomic_scatter(
    db: Session,
    *,
    x_trait: str,
    y_trait: str,
    farms: list[str] | None = None,
) -> dict[str, Any]:
    """Scatter of one genomic trait (x) against another (y) for inventory animals."""
    x_meta = _TRAIT_BY_KEY.get(x_trait)
    y_meta = _TRAIT_BY_KEY.get(y_trait)
    if x_meta is None:
        raise ValueError(f"Unknown trait: {x_trait}")
    if y_meta is None:
        raise ValueError(f"Unknown trait: {y_trait}")

    selected_farms = normalize_farms(farms)
    base = {
        "x_trait": x_trait,
        "y_trait": y_trait,
        "x_label": x_meta["label"],
        "y_label": y_meta["label"],
        "points": {},
        "x_min": None,
        "x_max": None,
        "y_min": None,
        "y_max": None,
        "count": 0,
    }
    if not selected_farms:
        return base

    x_field = x_meta["field"]
    y_field = y_meta["field"]
    points: dict[str, list[dict[str, Any]]] = {farm: [] for farm in selected_farms}
    x_values: list[float] = []
    y_values: list[float] = []

    for farm, etag, _bdat, genomic in _inventory_genomic_rows(db, selected_farms):
        x_value = getattr(genomic, x_field, None)
        y_value = getattr(genomic, y_field, None)
        if x_value is None or y_value is None:
            continue
        points[farm].append(
            {
                "x": float(x_value),
                "y": float(y_value),
                "etag": (etag or "").strip(),
                "cow_id": genomic.hbn,
            }
        )
        x_values.append(float(x_value))
        y_values.append(float(y_value))

    base["points"] = points
    base["count"] = sum(len(values) for values in points.values())
    base["x_min"] = min(x_values) if x_values else None
    base["x_max"] = max(x_values) if x_values else None
    base["y_min"] = min(y_values) if y_values else None
    base["y_max"] = max(y_values) if y_values else None
    return base
