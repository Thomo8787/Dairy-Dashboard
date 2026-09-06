"""Urine pH report from DairyComp cow events."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.database import CowEvent
from services.events_common import normalize_farms
from services.farms import HERD_FARM_OPTIONS

# DairyComp item names used for urine pH (8-char item, stored in Event).
UPH_EVENTS: tuple[str, ...] = ("UPH", "PH", "URINE", "URINEPH")

UPH_CATEGORY_EXCELLENT = "excellent"
UPH_CATEGORY_GOOD = "good"
UPH_CATEGORY_FAIR = "fair"
UPH_CATEGORY_POOR = "poor"
UPH_CATEGORY_UNKNOWN = "unknown"

UPH_CATEGORY_ORDER: tuple[str, ...] = (
    UPH_CATEGORY_EXCELLENT,
    UPH_CATEGORY_GOOD,
    UPH_CATEGORY_FAIR,
    UPH_CATEGORY_POOR,
)

UPH_CATEGORY_LABELS: dict[str, str] = {
    UPH_CATEGORY_EXCELLENT: "Excellent",
    UPH_CATEGORY_GOOD: "Good",
    UPH_CATEGORY_FAIR: "Fair",
    UPH_CATEGORY_POOR: "Poor",
    UPH_CATEGORY_UNKNOWN: "Unknown",
}

# Exclusive at the shared edges: 5.75 and 6.0 sit in the tighter band.
UPH_TARGETS: dict[str, float] = {
    "excellent_min_pct": 40.0,
    "good_or_better_min_pct": 70.0,
    "fair_or_better_min_pct": 90.0,
    "poor_max_pct": 10.0,
}

_PH_VALUE_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _parse_ph_value(*fields: str | None) -> float | None:
    for raw in fields:
        if not raw:
            continue
        text = str(raw).strip()
        if not text:
            continue
        try:
            value = float(text)
        except ValueError:
            match = _PH_VALUE_RE.search(text)
            if not match:
                continue
            value = float(match.group(1))
        if 5.0 <= value <= 9.5:
            return value
    return None


def _classify_ph(value: float | None) -> str:
    if value is None:
        return UPH_CATEGORY_UNKNOWN
    if value < 5.5 or value > 6.5:
        return UPH_CATEGORY_POOR
    if 5.75 <= value <= 6.0:
        return UPH_CATEGORY_EXCELLENT
    if value < 5.75:
        return UPH_CATEGORY_GOOD
    return UPH_CATEGORY_FAIR


def _empty_counts() -> dict[str, int]:
    return {key: 0 for key in UPH_CATEGORY_ORDER + (UPH_CATEGORY_UNKNOWN,)}


def _uph_filters():
    return (
        CowEvent.event.in_(list(UPH_EVENTS)),
        CowEvent.event_date.isnot(None),
    )


def _base_uph_query():
    return select(CowEvent).where(*_uph_filters())


def _build_performance(counts: dict[str, int]) -> dict[str, Any]:
    excellent = counts.get(UPH_CATEGORY_EXCELLENT, 0)
    good = counts.get(UPH_CATEGORY_GOOD, 0)
    fair = counts.get(UPH_CATEGORY_FAIR, 0)
    poor = counts.get(UPH_CATEGORY_POOR, 0)
    unknown = counts.get(UPH_CATEGORY_UNKNOWN, 0)
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
            "excellent": excellent_pct >= UPH_TARGETS["excellent_min_pct"],
            "good_or_better": good_or_better_pct >= UPH_TARGETS["good_or_better_min_pct"],
            "fair_or_better": fair_or_better_pct >= UPH_TARGETS["fair_or_better_min_pct"],
            "poor": poor_pct < UPH_TARGETS["poor_max_pct"],
        },
    }


def _farm_block(counts: dict[str, int]) -> dict[str, Any]:
    performance = _build_performance(counts)
    return {
        "counts": {key: counts.get(key, 0) for key in UPH_CATEGORY_ORDER},
        "unknown": counts.get(UPH_CATEGORY_UNKNOWN, 0),
        "total": performance["total"],
        "performance": performance,
    }


def build_urine_ph_report(
    db: Session,
    *,
    farms: list[str] | None = None,
    event_from: dt.date | None = None,
    event_to: dt.date | None = None,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)

    bounds_query = select(func.min(CowEvent.event_date), func.max(CowEvent.event_date)).where(
        *_uph_filters()
    )
    bounds_min, bounds_max = db.execute(bounds_query).one()

    query = _base_uph_query()
    if event_from is not None:
        query = query.where(CowEvent.event_date >= event_from)
    if event_to is not None:
        query = query.where(CowEvent.event_date <= event_to)
    if selected_farms:
        query = query.where(CowEvent.farm.in_(selected_farms))

    rows = list(db.scalars(query).all())

    by_farm: dict[str, dict[str, int]] = {farm: _empty_counts() for farm in HERD_FARM_OPTIONS}
    total = _empty_counts()

    for row in rows:
        farm = row.farm if row.farm in HERD_FARM_OPTIONS else None
        if farm is None:
            continue
        category = _classify_ph(_parse_ph_value(row.remark, row.r, row.t))
        by_farm[farm][category] += 1
        if farm in selected_farms:
            total[category] += 1

    latest_import = db.scalar(
        select(func.max(CowEvent.import_timestamp)).where(CowEvent.event.in_(list(UPH_EVENTS)))
    )
    if latest_import is None:
        latest_import = db.scalar(select(func.max(CowEvent.import_timestamp)))

    return {
        "latest_import": latest_import.isoformat() if latest_import else None,
        "event_bounds": {
            "min": bounds_min.isoformat() if bounds_min else None,
            "max": bounds_max.isoformat() if bounds_max else None,
        },
        "selected_farms": selected_farms,
        "farms": {farm: _farm_block(by_farm[farm]) for farm in HERD_FARM_OPTIONS},
        "total": _farm_block(total),
        "categories": [
            {"id": key, "label": UPH_CATEGORY_LABELS[key]} for key in UPH_CATEGORY_ORDER
        ],
        "targets": UPH_TARGETS,
        "event_names": list(UPH_EVENTS),
        "bands": {
            "excellent": "5.75–6.0",
            "good": "5.5–5.75",
            "fair": "6.0–6.5",
            "poor": "< 5.5 or > 6.5",
        },
    }
