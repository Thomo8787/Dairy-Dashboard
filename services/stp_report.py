"""Serum Total Protein (STP) report for calf events."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.database import CowEvent
from services.farms import HERD_FARM_OPTIONS
from services.events_common import normalize_farms

STP_EVENT = "TP"
STP_MAX_AGE_DAYS = 7

STP_BREED_BEEF = "beef"
STP_BREED_DAIRY = "dairy"
STP_BREED_UNKNOWN = "unknown"
STP_BREED_OPTIONS: tuple[str, ...] = (STP_BREED_BEEF, STP_BREED_DAIRY)
STP_DEFAULT_BREED_TYPES: tuple[str, ...] = (STP_BREED_DAIRY,)
STP_BEEF_CBREED_MIN = 102  # Dairy CBRD < 102; beef CBRD >= 102

STP_CATEGORY_EXCELLENT = "excellent"
STP_CATEGORY_GOOD = "good"
STP_CATEGORY_FAIR = "fair"
STP_CATEGORY_POOR = "poor"
STP_CATEGORY_UNKNOWN = "unknown"

STP_CATEGORY_ORDER: tuple[str, ...] = (
    STP_CATEGORY_EXCELLENT,
    STP_CATEGORY_GOOD,
    STP_CATEGORY_FAIR,
    STP_CATEGORY_POOR,
)

STP_CATEGORY_LABELS: dict[str, str] = {
    STP_CATEGORY_EXCELLENT: "Excellent",
    STP_CATEGORY_GOOD: "Good",
    STP_CATEGORY_FAIR: "Fair",
    STP_CATEGORY_POOR: "Poor",
    STP_CATEGORY_UNKNOWN: "Unknown",
}

STP_TARGETS: dict[str, float] = {
    "excellent_min_pct": 40.0,
    "good_or_better_min_pct": 70.0,
    "fair_or_better_min_pct": 90.0,
    "poor_max_pct": 10.0,
}

_PROTEIN_VALUE_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _parse_protein_value(*fields: str | None) -> float | None:
    for raw in fields:
        if not raw:
            continue
        text = str(raw).strip()
        if not text:
            continue
        try:
            value = float(text)
        except ValueError:
            match = _PROTEIN_VALUE_RE.search(text)
            if not match:
                continue
            value = float(match.group(1))
        if 0 < value < 20:
            return value
    return None


def _classify_protein(value: float | None) -> str:
    if value is None:
        return STP_CATEGORY_UNKNOWN
    if value >= 6.2:
        return STP_CATEGORY_EXCELLENT
    if value >= 5.8:
        return STP_CATEGORY_GOOD
    if value >= 5.1:
        return STP_CATEGORY_FAIR
    return STP_CATEGORY_POOR


def _empty_counts() -> dict[str, int]:
    return {key: 0 for key in STP_CATEGORY_ORDER + (STP_CATEGORY_UNKNOWN,)}


def normalize_breed_types(breed_types: list[str] | None) -> list[str]:
    if not breed_types:
        return list(STP_DEFAULT_BREED_TYPES)
    selected = [value.lower() for value in breed_types if value.lower() in STP_BREED_OPTIONS]
    return selected or list(STP_DEFAULT_BREED_TYPES)


def classify_stp_breed(cbrd: int | None) -> str:
    """Dairy CBRD codes are below 102; beef codes are 102 and above."""
    if cbrd is None:
        return STP_BREED_UNKNOWN
    if cbrd < STP_BEEF_CBREED_MIN:
        return STP_BREED_DAIRY
    return STP_BREED_BEEF


def _build_performance(counts: dict[str, int]) -> dict[str, Any]:
    excellent = counts.get(STP_CATEGORY_EXCELLENT, 0)
    good = counts.get(STP_CATEGORY_GOOD, 0)
    fair = counts.get(STP_CATEGORY_FAIR, 0)
    poor = counts.get(STP_CATEGORY_POOR, 0)
    unknown = counts.get(STP_CATEGORY_UNKNOWN, 0)
    total = excellent + good + fair + poor

    if total == 0:
        return {
            "total": 0,
            "unknown": unknown,
            "excellent": {"count": 0, "pct": 0.0},
            "good_or_better": {"count": 0, "pct": 0.0},
            "fair_or_better": {"count": 0, "pct": 0.0},
            "poor": {"count": 0, "pct": 0.0},
            "targets_met": {
                "excellent": False,
                "good_or_better": False,
                "fair_or_better": False,
                "poor": False,
            },
        }

    excellent_pct = round(excellent / total * 100, 1)
    good_or_better_count = excellent + good
    good_or_better_pct = round(good_or_better_count / total * 100, 1)
    fair_or_better_count = excellent + good + fair
    fair_or_better_pct = round(fair_or_better_count / total * 100, 1)
    poor_pct = round(poor / total * 100, 1)

    return {
        "total": total,
        "unknown": unknown,
        "excellent": {"count": excellent, "pct": excellent_pct},
        "good_or_better": {"count": good_or_better_count, "pct": good_or_better_pct},
        "fair_or_better": {"count": fair_or_better_count, "pct": fair_or_better_pct},
        "poor": {"count": poor, "pct": poor_pct},
        "targets_met": {
            "excellent": excellent_pct >= STP_TARGETS["excellent_min_pct"],
            "good_or_better": good_or_better_pct >= STP_TARGETS["good_or_better_min_pct"],
            "fair_or_better": fair_or_better_pct >= STP_TARGETS["fair_or_better_min_pct"],
            "poor": poor_pct < STP_TARGETS["poor_max_pct"],
        },
    }


def _stp_filters():
    age_days = CowEvent.event_date - CowEvent.bdat
    return (
        CowEvent.event == STP_EVENT,
        CowEvent.lact == 0,
        CowEvent.bdat.isnot(None),
        CowEvent.event_date.isnot(None),
        age_days >= 0,
        age_days <= STP_MAX_AGE_DAYS,
    )


def _base_stp_query():
    return select(CowEvent).where(*_stp_filters())


def _farm_block(counts: dict[str, int]) -> dict[str, Any]:
    performance = _build_performance(counts)
    return {
        "counts": {key: counts.get(key, 0) for key in STP_CATEGORY_ORDER},
        "unknown": counts.get(STP_CATEGORY_UNKNOWN, 0),
        "total": performance["total"],
        "performance": performance,
    }


def build_stp_report(
    db: Session,
    *,
    farms: list[str] | None = None,
    breed_types: list[str] | None = None,
    birth_from: dt.date | None = None,
    birth_to: dt.date | None = None,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)
    selected_breeds = normalize_breed_types(breed_types)

    bounds_query = select(func.min(CowEvent.bdat), func.max(CowEvent.bdat)).where(*_stp_filters())
    bounds_min, bounds_max = db.execute(bounds_query).one()

    query = _base_stp_query()
    if birth_from is not None:
        query = query.where(CowEvent.bdat >= birth_from)
    if birth_to is not None:
        query = query.where(CowEvent.bdat <= birth_to)
    if selected_farms:
        query = query.where(CowEvent.farm.in_(selected_farms))

    rows = list(db.scalars(query).all())

    by_farm: dict[str, dict[str, int]] = {
        farm: _empty_counts() for farm in HERD_FARM_OPTIONS
    }
    total = _empty_counts()

    for row in rows:
        farm = row.farm if row.farm in HERD_FARM_OPTIONS else None
        if farm is None:
            continue
        breed = classify_stp_breed(row.cbrd)
        if breed not in selected_breeds:
            continue
        category = _classify_protein(_parse_protein_value(row.remark, row.r, row.t))
        by_farm[farm][category] += 1
        if farm in selected_farms:
            total[category] += 1

    latest_import = db.scalar(
        select(func.max(CowEvent.import_timestamp)).where(CowEvent.event == STP_EVENT)
    )

    return {
        "latest_import": latest_import.isoformat() if latest_import else None,
        "birth_bounds": {
            "min": bounds_min.isoformat() if bounds_min else None,
            "max": bounds_max.isoformat() if bounds_max else None,
        },
        "selected_farms": selected_farms,
        "selected_breeds": selected_breeds,
        "farms": {farm: _farm_block(by_farm[farm]) for farm in HERD_FARM_OPTIONS},
        "total": _farm_block(total),
        "categories": [
            {"id": key, "label": STP_CATEGORY_LABELS[key]} for key in STP_CATEGORY_ORDER
        ],
        "targets": STP_TARGETS,
        "max_age_days": STP_MAX_AGE_DAYS,
        "breed_options": [
            {"id": STP_BREED_BEEF, "label": "Beef"},
            {"id": STP_BREED_DAIRY, "label": "Dairy"},
        ],
        "bands": {
            "excellent": "≥ 6.2",
            "good": "≥ 5.8 and < 6.2",
            "fair": "≥ 5.1 and < 5.8",
            "poor": "< 5.1",
        },
    }
