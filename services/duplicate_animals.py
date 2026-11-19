"""Animals whose ear tag is on more than one DairyComp right now."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.database import HerdInventory
from services.farms import FARMS_BY_CODE, HERD_FARM_OPTIONS


def ear_tag_key(etag: str | None) -> str:
    """Match key for an ear tag. Spaces and case do not make a different animal."""
    return "".join((etag or "").split()).upper()


def _as_date(value: Any) -> dt.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def _format_date(value: dt.date | None) -> str:
    return value.strftime("%d/%m/%Y") if value else ""


def _location(row: dict[str, Any], farm: str) -> dict[str, str]:
    edat = _as_date(row.get("edat"))
    return {
        "farm": farm,
        "cow_id": str(row.get("cow_id") or "").strip(),
        "pen": str(row.get("pen") or "").strip(),
        "edat": _format_date(edat),
        "edat_iso": edat.isoformat() if edat else "",
    }


def _prefer_location(current: dict[str, str] | None, incoming: dict[str, str]) -> dict[str, str]:
    if current is None:
        return incoming
    for field in ("pen", "edat", "cow_id"):
        if not current[field] and incoming[field]:
            return incoming
    return current


def duplicate_animals_from_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return animals present on two or more DairyComps.

    ALH and BNK share one DairyComp, so an ear tag on only those two farms is
    not a duplicate. Farm 1 is the earliest entry date. Farm 2 is the next,
    and a missing entry date comes after a dated one. The list opens sorted
    by Farm 2, then by that farm's entry date.
    """
    by_tag: dict[str, dict[str, dict[str, str]]] = {}
    birth_by_tag: dict[str, dt.date] = {}
    for row in rows:
        tag = ear_tag_key(row.get("etag"))
        farm = str(row.get("farm") or "").strip()
        if not tag or farm not in FARMS_BY_CODE:
            continue
        farms = by_tag.setdefault(tag, {})
        farms[farm] = _prefer_location(farms.get(farm), _location(row, farm))
        birth = _as_date(row.get("bdat"))
        if birth is not None and tag not in birth_by_tag:
            birth_by_tag[tag] = birth

    animals: list[dict[str, Any]] = []
    for tag, farms in by_tag.items():
        groups = {FARMS_BY_CODE[farm].dairy_data_group for farm in farms}
        if len(groups) < 2:
            continue
        locations = sorted(
            farms.values(),
            key=lambda loc: (
                loc["edat_iso"] == "",
                loc["edat_iso"],
                HERD_FARM_OPTIONS.index(loc["farm"]),
            ),
        )
        birth = birth_by_tag.get(tag)
        animals.append(
            {
                "etag": tag,
                "birth_date": _format_date(birth),
                "locations": locations,
            }
        )

    def opening_order(animal: dict[str, Any]) -> tuple:
        farm2 = animal["locations"][1] if len(animal["locations"]) > 1 else None
        if farm2 is None:
            return (True, "", True, "", animal["etag"])
        return (
            False,
            farm2["farm"],
            farm2["edat_iso"] == "",
            farm2["edat_iso"],
            animal["etag"],
        )

    animals.sort(key=opening_order)
    return animals


def get_duplicate_animals_report(db: Session) -> dict[str, Any]:
    """Current inventory rows whose ear tag is on more than one DairyComp."""
    stored = db.execute(
        select(
            HerdInventory.etag,
            HerdInventory.cow_id,
            HerdInventory.farm,
            HerdInventory.pen,
            HerdInventory.bdat,
            HerdInventory.edat,
        ).where(HerdInventory.etag.isnot(None))
    ).all()
    latest_import = db.scalar(select(func.max(HerdInventory.import_timestamp)))
    animals = duplicate_animals_from_rows(
        [
            {
                "etag": etag,
                "cow_id": cow_id,
                "farm": farm,
                "pen": pen,
                "bdat": bdat,
                "edat": edat,
            }
            for etag, cow_id, farm, pen, bdat, edat in stored
        ]
    )
    slot_count = max((len(animal["locations"]) for animal in animals), default=0)
    return {
        "animals": animals,
        "count": len(animals),
        "slot_count": slot_count,
        "latest_import": latest_import.strftime("%d %b %Y %H:%M") if latest_import else None,
        "farms": list(HERD_FARM_OPTIONS),
    }
