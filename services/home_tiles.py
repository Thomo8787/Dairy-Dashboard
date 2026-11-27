"""Stored Home tile figures.

The page reads these snapshots instead of rebuilding them on every visit.
The 08:00 UTC job saves every tile after the herd file, Müller, and the
morning NML mail. A herd import saves the herd tiles again. An NML import
saves the milk tiles again, including the 19:00 UTC run.

Each group is its own setting, so a milk save cannot wipe a herd save.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.database import AppSetting
from services.events_common import build_dairy_semen_30d, build_home_herd_summary
from services.production_summary import get_home_cows_in_milk, get_production_summary

logger = logging.getLogger(__name__)

_KEYS = {
    "production": "home.tiles.production",
    "cows_in_milk": "home.tiles.cows_in_milk",
    "dairy_semen": "home.tiles.dairy_semen",
    "herd_summary": "home.tiles.herd_summary",
}


def _read(db: Session, name: str) -> dict[str, Any] | None:
    row = db.scalar(select(AppSetting).where(AppSetting.key == _KEYS[name]))
    if row is None or not (row.value or "").strip():
        return None
    try:
        parsed = json.loads(row.value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _write(db: Session, name: str, payload: dict[str, Any]) -> None:
    payload = dict(payload)
    payload["saved_at"] = datetime.now(timezone.utc).isoformat()
    text = json.dumps(payload)
    row = db.scalar(select(AppSetting).where(AppSetting.key == _KEYS[name]))
    now = datetime.now(timezone.utc)
    if row is None:
        db.add(AppSetting(key=_KEYS[name], value=text, updated_at=now))
    else:
        row.value = text
        row.updated_at = now
    db.flush()


def refresh_home_tiles(db: Session, *, parts: str = "all") -> None:
    """Recompute stored Home tiles. ``parts`` is ``all``, ``herd``, or ``milk``."""
    if parts in ("all", "milk"):
        _write(db, "production", get_production_summary())
    if parts in ("all", "herd"):
        _write(db, "cows_in_milk", get_home_cows_in_milk())
        _write(db, "dairy_semen", build_dairy_semen_30d(db))
        _write(db, "herd_summary", build_home_herd_summary(db))
    logger.info("Saved Home tiles (%s)", parts)


def _compute(db: Session, name: str) -> dict[str, Any]:
    if name == "production":
        return get_production_summary()
    if name == "cows_in_milk":
        return get_home_cows_in_milk()
    if name == "dairy_semen":
        return build_dairy_semen_30d(db)
    if name == "herd_summary":
        return build_home_herd_summary(db)
    raise KeyError(name)


def _ensure(db: Session, name: str) -> dict[str, Any]:
    found = _read(db, name)
    if found is not None:
        return found
    _write(db, name, _compute(db, name))
    found = _read(db, name)
    if found is None:
        raise RuntimeError(f"Home tile {name} was not saved")
    return found


def _public(payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload)
    data.pop("saved_at", None)
    return data


def stored_production_summary(db: Session) -> dict[str, Any]:
    found = _read(db, "production")
    # Snapshots saved before the Issues tab have no comment list. Rebuild once.
    if found is None or "issues" not in found or found.get("collection_issues") != "swing-and-unmatched":
        _write(db, "production", get_production_summary())
        found = _read(db, "production")
    if found is None:
        raise RuntimeError("Home tile production was not saved")
    return _public(found)


def stored_cows_in_milk(db: Session) -> dict[str, Any]:
    return _public(_ensure(db, "cows_in_milk"))


def stored_dairy_semen(db: Session) -> dict[str, Any]:
    return _public(_ensure(db, "dairy_semen"))


def stored_herd_summary(db: Session) -> dict[str, Any]:
    return _public(_ensure(db, "herd_summary"))
