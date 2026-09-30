"""Whether each farm's DairyComp OneDrive export was updated today.

The daily import stores the OneDrive last-modified time on the events and
inventory files. A farm is current only when both of those files were
modified today (Europe/London). BNK has no folder of its own; it uses the
ALH file when a BNK fingerprint has not been stored yet.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.database import AppSetting
from services.farms import FARMS
from services.herd_import_utils import (
    FP_EVENTS,
    FP_INVENTORY,
    SHARED_HERD_SOURCE_FARM,
    fingerprint_setting_key,
)

_UK = ZoneInfo("Europe/London")
_FILE_LABELS = (
    (FP_EVENTS, "events"),
    (FP_INVENTORY, "inventory"),
)


def _parse_last_modified(raw: str | None) -> dt.datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(_UK)


def _fingerprint_modified(fingerprint: str | None) -> dt.datetime | None:
    if not fingerprint:
        return None
    try:
        payload = json.loads(fingerprint)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return _parse_last_modified(str(payload.get("last_modified") or ""))


def _format_uk_date(moment: dt.datetime | None) -> str | None:
    if moment is None:
        return None
    return f"{moment.day} {moment.strftime('%b %Y')}"


def _fingerprint_value(
    stored: dict[str, str | None], prefix: str, farm_code: str
) -> str | None:
    fingerprint = stored.get(fingerprint_setting_key(prefix, farm_code))
    if fingerprint or farm_code != "BNK":
        return fingerprint
    return stored.get(fingerprint_setting_key(prefix, SHARED_HERD_SOURCE_FARM))


def herd_file_status_by_farm(
    db: Session, *, today: dt.date | None = None
) -> dict[str, dict[str, Any]]:
    """Return per-farm tick/cross data for the homepage."""
    uk_today = today or dt.datetime.now(_UK).date()
    keys = [
        fingerprint_setting_key(prefix, farm.code)
        for farm in FARMS
        for prefix, _label in _FILE_LABELS
    ]
    rows = db.scalars(select(AppSetting).where(AppSetting.key.in_(keys))).all()
    stored = {row.key: row.value for row in rows}

    status: dict[str, dict[str, Any]] = {}
    for farm in FARMS:
        moments = {
            label: _fingerprint_modified(_fingerprint_value(stored, prefix, farm.code))
            for prefix, label in _FILE_LABELS
        }
        dates = {label: _format_uk_date(moment) for label, moment in moments.items()}
        ok = all(moment is not None and moment.date() == uk_today for moment in moments.values())
        present = [dates[label] for _prefix, label in _FILE_LABELS if dates[label]]
        if ok:
            title = f"New herd file today ({present[0]})"
            aria = f"{farm.code} herd file updated today"
        elif not present:
            title = "No herd file imported"
            aria = f"{farm.code} has no herd file"
        elif len(set(present)) == 1:
            title = f"No new herd file today. Last file {present[0]}"
            aria = f"{farm.code} herd file not updated today. Last file {present[0]}"
        else:
            detail = ", ".join(
                f"{label} {dates[label] or 'missing'}" for _prefix, label in _FILE_LABELS
            )
            title = f"No new herd file today. Last {detail}"
            aria = f"{farm.code} herd file not updated today. {detail}"
        status[farm.code] = {
            "ok": ok,
            "title": title,
            "aria": aria,
            "events_on": dates.get("events"),
            "inventory_on": dates.get("inventory"),
        }
    return status
