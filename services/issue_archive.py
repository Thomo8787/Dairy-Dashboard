"""Issues parked off the current Home list while a fix works through.

A parked issue stays hidden only while that same farm, measure, and name are
still being raised. Once it clears, the note is dropped, so a later return
shows on the current list again.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.database import AppSetting

_SETTING_KEY = "home.issues.archive"
_MAX_KEYS = 200


def issue_key(farm: str, metric: str, label: str) -> str:
    return "|".join([farm.strip(), metric.strip(), label.strip()])


def _clean_key(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 300 or text.count("|") != 2:
        return None
    farm, metric, label = text.split("|")
    if not farm or not metric:
        return None
    return issue_key(farm, metric, label)


def apply_archive(
    keys: list[str],
    *,
    key: str | None = None,
    archived: bool | None = None,
    remove: list[str] | None = None,
) -> list[str]:
    """Return the archive after one add, one restore, and any cleared keys removed."""
    kept: list[str] = []
    seen: set[str] = set()
    for item in keys:
        cleaned = _clean_key(item)
        if cleaned is None or cleaned in seen:
            continue
        seen.add(cleaned)
        kept.append(cleaned)
    if remove:
        drop = {cleaned for item in remove if (cleaned := _clean_key(item))}
        kept = [item for item in kept if item not in drop]
        seen = set(kept)
    cleaned_key = _clean_key(key) if key else None
    if cleaned_key is not None and archived is True and cleaned_key not in seen:
        kept.append(cleaned_key)
    elif cleaned_key is not None and archived is False:
        kept = [item for item in kept if item != cleaned_key]
    return kept[:_MAX_KEYS]


def _load(db: Session) -> tuple[AppSetting | None, list[str]]:
    query = select(AppSetting).where(AppSetting.key == _SETTING_KEY)
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    row = db.scalar(query)
    if row is None or not (row.value or "").strip():
        return row, []
    try:
        parsed = json.loads(row.value)
    except json.JSONDecodeError:
        return row, []
    if not isinstance(parsed, list):
        return row, []
    return row, [item for item in parsed if isinstance(item, str)]


def _save(db: Session, row: AppSetting | None, keys: list[str]) -> None:
    text = json.dumps(keys)
    if row is None:
        db.add(AppSetting(key=_SETTING_KEY, value=text))
    else:
        row.value = text
    db.flush()


def load_issue_archive(db: Session) -> list[str]:
    _row, keys = _load(db)
    return apply_archive(keys)


def update_issue_archive(
    db: Session,
    *,
    key: str | None = None,
    archived: bool | None = None,
    remove: list[str] | None = None,
) -> list[str]:
    row, keys = _load(db)
    updated = apply_archive(keys, key=key, archived=archived, remove=remove)
    _save(db, row, updated)
    return updated
