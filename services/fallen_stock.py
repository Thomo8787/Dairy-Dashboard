"""On-farm deaths whose passports still need to go back to BCMS.

OFS deaths are left off. A remark of OFS, or a DEST of OFS, Cneild, or CSarg,
is fallen stock collected off the list. TB and other on-farm deaths stay.
Checking a row stores the date and moves it to the archive.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from services.database import CowEvent, FallenStockCheck
from services.events_common import ofs_sales_clause
from services.farms import HERD_FARM_OPTIONS


def _normalize(value: str | None) -> str:
    return (value or "").strip()


def _farms(farms: list[str] | None) -> list[str]:
    if not farms:
        return list(HERD_FARM_OPTIONS)
    return [farm for farm in farms if farm in HERD_FARM_OPTIONS]


def _age_months(bdat: dt.date | None, event_date: dt.date | None) -> int | None:
    if bdat is None or event_date is None:
        return None
    days = (event_date - bdat).days
    if days < 0:
        return None
    return days // 30


def _on_farm_death_clause():
    return and_(
        CowEvent.event == "DIED",
        CowEvent.event_date.isnot(None),
        ~ofs_sales_clause(),
    )


def _animal_key(cow_id: str, etag: str) -> str:
    return etag or cow_id


def _row_key(farm: str, cow_id: str, etag: str, event_date: dt.date) -> tuple[str, str, str, dt.date]:
    return (farm, cow_id, etag, event_date)


def _event_row(event: CowEvent, checked_on: dt.date | None) -> dict[str, Any]:
    cow_id = _normalize(event.cow_id)
    etag = _normalize(event.etag)
    event_date = event.event_date
    return {
        "farm": event.farm,
        "cow_id": cow_id,
        "etag": etag,
        "event_date": event_date.isoformat() if event_date else "",
        "dest": _normalize(event.dest),
        "remark": _normalize(event.remark),
        "age_months": _age_months(event.bdat, event_date),
        "checked_on": checked_on.isoformat() if checked_on else None,
    }


def _check_row(check: FallenStockCheck, event: CowEvent | None) -> dict[str, Any]:
    if event is not None:
        return _event_row(event, check.checked_on)
    return {
        "farm": check.farm,
        "cow_id": _normalize(check.cow_id),
        "etag": _normalize(check.etag),
        "event_date": check.event_date.isoformat(),
        "dest": "",
        "remark": "",
        "age_months": None,
        "checked_on": check.checked_on.isoformat(),
    }


def _deduped_deaths(events: list[CowEvent]) -> list[CowEvent]:
    """One row per farm, animal, and death date. Keep the latest stored event."""
    chosen: dict[tuple[str, str, dt.date], CowEvent] = {}
    for event in events:
        if event.event_date is None:
            continue
        key = (event.farm, _animal_key(_normalize(event.cow_id), _normalize(event.etag)), event.event_date)
        current = chosen.get(key)
        if current is None or (event.id or 0) > (current.id or 0):
            chosen[key] = event
    return list(chosen.values())


def _load_deaths(db: Session, farms: list[str]) -> list[CowEvent]:
    if not farms:
        return []
    events = db.scalars(
        select(CowEvent).where(_on_farm_death_clause(), CowEvent.farm.in_(farms))
    ).all()
    return _deduped_deaths(list(events))


def _load_checks(db: Session, farms: list[str]) -> dict[tuple[str, str, str, dt.date], FallenStockCheck]:
    if not farms:
        return {}
    rows = db.scalars(select(FallenStockCheck).where(FallenStockCheck.farm.in_(farms))).all()
    found: dict[tuple[str, str, str, dt.date], FallenStockCheck] = {}
    for row in rows:
        found[_row_key(row.farm, _normalize(row.cow_id), _normalize(row.etag), row.event_date)] = row
    return found


def list_fallen_stock(
    db: Session,
    *,
    status: str = "active",
    farms: list[str] | None = None,
) -> dict[str, Any]:
    selected = _farms(farms)
    view = "archived" if status == "archived" else "active"
    deaths = _load_deaths(db, selected)
    checks = _load_checks(db, selected)
    death_by_key = {
        _row_key(event.farm, _normalize(event.cow_id), _normalize(event.etag), event.event_date): event
        for event in deaths
        if event.event_date is not None
    }

    if view == "archived":
        rows = [
            _check_row(check, death_by_key.get(key))
            for key, check in checks.items()
        ]
        rows.sort(key=lambda row: (row["checked_on"] or "", row["event_date"]), reverse=True)
    else:
        rows = [
            _event_row(event, None)
            for key, event in death_by_key.items()
            if key not in checks
        ]
        rows.sort(key=lambda row: (row["event_date"], row["cow_id"], row["etag"]), reverse=True)

    return {"rows": rows, "total": len(rows), "status": view}


def _parse_item(item: dict[str, Any]) -> tuple[str, str, str, dt.date] | None:
    if not isinstance(item, dict):
        return None
    farm = _normalize(item.get("farm")).upper()
    if farm not in HERD_FARM_OPTIONS:
        return None
    event_date = item.get("event_date")
    if isinstance(event_date, dt.datetime):
        event_date = event_date.date()
    elif isinstance(event_date, str):
        try:
            event_date = dt.date.fromisoformat(event_date)
        except ValueError:
            return None
    if not isinstance(event_date, dt.date):
        return None
    return farm, _normalize(item.get("cow_id")), _normalize(item.get("etag")), event_date


def check_off_fallen_stock(
    db: Session,
    items: list[dict[str, Any]],
    user: Any,
) -> dict[str, Any]:
    """Move on-farm deaths to the archive and store today's date."""
    today = dt.datetime.now(dt.UTC).date()
    checked = 0
    user_id = getattr(user, "id", None)
    for item in items:
        parsed = _parse_item(item)
        if parsed is None:
            continue
        farm, cow_id, etag, event_date = parsed
        event = db.scalars(
            select(CowEvent).where(
                _on_farm_death_clause(),
                CowEvent.farm == farm,
                CowEvent.event_date == event_date,
                func.trim(func.coalesce(CowEvent.cow_id, "")) == cow_id,
                func.trim(func.coalesce(CowEvent.etag, "")) == etag,
            )
        ).first()
        if event is None:
            continue
        existing = db.scalars(
            select(FallenStockCheck).where(
                FallenStockCheck.farm == farm,
                FallenStockCheck.cow_id == cow_id,
                FallenStockCheck.etag == etag,
                FallenStockCheck.event_date == event_date,
            )
        ).first()
        if existing is not None:
            checked += 1
            continue
        db.add(
            FallenStockCheck(
                farm=farm,
                cow_id=cow_id,
                etag=etag,
                event_date=event_date,
                checked_on=today,
                checked_by_user_id=user_id,
            )
        )
        checked += 1
    return {"checked": checked, "checked_on": today.isoformat()}


def unarchive_fallen_stock(
    db: Session,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Put checked deaths back on the active list."""
    returned = 0
    for item in items:
        parsed = _parse_item(item)
        if parsed is None:
            continue
        farm, cow_id, etag, event_date = parsed
        existing = db.scalars(
            select(FallenStockCheck).where(
                FallenStockCheck.farm == farm,
                FallenStockCheck.cow_id == cow_id,
                FallenStockCheck.etag == etag,
                FallenStockCheck.event_date == event_date,
            )
        ).first()
        if existing is None:
            continue
        db.delete(existing)
        returned += 1
    return {"returned": returned}
