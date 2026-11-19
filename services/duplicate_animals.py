"""Animals whose ear tag is on more than one DairyComp right now."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.database import HerdInventory
from services.farms import FARMS_BY_CODE, HERD_FARM_OPTIONS


def ear_tag_key(etag: str | None) -> str:
    """Match key for an ear tag. Spaces and case do not make a different animal."""
    return "".join((etag or "").split()).upper()


def duplicate_animals_from_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return animals present on two or more DairyComps.

    ALH and BNK share one DairyComp, so an ear tag on only those two farms is
    not a duplicate. Each row needs etag, cow_id, farm, and pen.
    """
    by_tag: dict[str, dict[str, dict[str, str]]] = {}
    for row in rows:
        tag = ear_tag_key(row.get("etag"))
        farm = str(row.get("farm") or "").strip()
        if not tag or farm not in FARMS_BY_CODE:
            continue
        farms = by_tag.setdefault(tag, {})
        current = farms.get(farm)
        incoming = {
            "cow_id": str(row.get("cow_id") or "").strip(),
            "pen": str(row.get("pen") or "").strip(),
        }
        if current is None or (not current["pen"] and incoming["pen"]):
            farms[farm] = incoming

    animals: list[dict[str, Any]] = []
    for tag, farms in by_tag.items():
        groups = {
            FARMS_BY_CODE[farm].dairy_data_group
            for farm in farms
        }
        if len(groups) < 2:
            continue
        animals.append({"etag": tag, "farms": farms})
    animals.sort(key=lambda animal: animal["etag"])
    return animals


def get_duplicate_animals_report(db: Session) -> dict[str, Any]:
    """Current inventory rows whose ear tag is on more than one DairyComp."""
    stored = db.execute(
        select(
            HerdInventory.etag,
            HerdInventory.cow_id,
            HerdInventory.farm,
            HerdInventory.pen,
        ).where(HerdInventory.etag.isnot(None))
    ).all()
    latest_import = db.scalar(select(func.max(HerdInventory.import_timestamp)))
    animals = duplicate_animals_from_rows(
        [
            {"etag": etag, "cow_id": cow_id, "farm": farm, "pen": pen}
            for etag, cow_id, farm, pen in stored
        ]
    )
    return {
        "animals": animals,
        "count": len(animals),
        "latest_import": latest_import.strftime("%d %b %Y %H:%M") if latest_import else None,
        "farms": list(HERD_FARM_OPTIONS),
    }
