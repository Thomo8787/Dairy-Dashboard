"""Breeding sire classification (beef vs dairy semen) from remark suffixes and overrides."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.database import BreedingSireClassification, CowEvent

VALID_SEMEN_TYPES: frozenset[str] = frozenset({"beef", "dairy"})


def normalize_sire_code(remark: str | None) -> str | None:
    if not remark:
        return None
    code = remark.strip()
    if not code:
        return None
    lower = code.lower()
    if lower.endswith(".b"):
        base = code[:-2].strip()
        return base or None
    if lower.endswith(".s"):
        base = code[:-2].strip()
        return base or None
    return code


def _suffix_semen_type(remark: str) -> str | None:
    lower = remark.strip().lower()
    if lower.endswith(".b"):
        return "beef"
    if lower.endswith(".s"):
        return "dairy"
    return None


def classify_semen_type(remark: str | None, overrides: dict[str, str]) -> str:
    if not remark:
        return "unknown"
    stripped = remark.strip()
    if not stripped:
        return "unknown"
    suffix_type = _suffix_semen_type(stripped)
    if suffix_type:
        return suffix_type
    sire_code = normalize_sire_code(stripped)
    if sire_code and sire_code in overrides:
        return overrides[sire_code]
    return "unknown"


def load_sire_overrides(db: Session) -> dict[str, str]:
    rows = db.scalars(select(BreedingSireClassification)).all()
    return {row.sire_code: row.semen_type for row in rows}


def set_sire_classification(db: Session, sire_code: str, semen_type: str) -> BreedingSireClassification:
    code = normalize_sire_code(sire_code) or sire_code.strip()
    if not code:
        raise ValueError("Sire code is required")
    semen_type = semen_type.strip().lower()
    if semen_type not in VALID_SEMEN_TYPES:
        raise ValueError("semen_type must be beef or dairy")

    existing = db.scalar(
        select(BreedingSireClassification).where(BreedingSireClassification.sire_code == code)
    )
    if existing:
        existing.semen_type = semen_type
        db.commit()
        db.refresh(existing)
        return existing

    row = BreedingSireClassification(sire_code=code, semen_type=semen_type)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def delete_sire_classification(db: Session, sire_code: str) -> bool:
    code = normalize_sire_code(sire_code) or sire_code.strip()
    if not code:
        return False
    row = db.scalar(
        select(BreedingSireClassification).where(BreedingSireClassification.sire_code == code)
    )
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def _sire_source(remark: str, overrides: dict[str, str], semen_type: str) -> str:
    if _suffix_semen_type(remark.strip()):
        return "auto"
    sire_code = normalize_sire_code(remark)
    if sire_code and sire_code in overrides and semen_type in VALID_SEMEN_TYPES:
        return "override"
    return "unknown"


def list_all_sires(db: Session) -> dict[str, list[dict[str, Any]]]:
    overrides = load_sire_overrides(db)
    rows = db.execute(
        select(CowEvent.remark, func.count())
        .where(CowEvent.event == "BRED")
        .where(CowEvent.remark.isnot(None))
        .where(CowEvent.remark != "")
        .group_by(CowEvent.remark)
    ).all()

    grouped: dict[str, dict[str, dict[str, Any]]] = {
        "beef": {},
        "dairy": {},
        "unknown": {},
    }

    for remark, count in rows:
        if not remark:
            continue
        semen_type = classify_semen_type(remark, overrides)
        sire_code = normalize_sire_code(remark)
        if not sire_code:
            continue
        bucket = grouped[semen_type]
        if sire_code not in bucket:
            bucket[sire_code] = {
                "sire_code": sire_code,
                "count": 0,
                "source": _sire_source(remark, overrides, semen_type),
            }
        bucket[sire_code]["count"] += int(count)
        if semen_type in VALID_SEMEN_TYPES and _suffix_semen_type(remark.strip()):
            bucket[sire_code]["source"] = "auto"
        elif semen_type in VALID_SEMEN_TYPES and sire_code in overrides:
            bucket[sire_code]["source"] = "override"

    result: dict[str, list[dict[str, Any]]] = {}
    for key in ("beef", "dairy", "unknown"):
        items = sorted(grouped[key].values(), key=lambda item: (-item["count"], item["sire_code"]))
        if key == "unknown":
            for item in items:
                item.pop("source", None)
        result[key] = items
    return result
