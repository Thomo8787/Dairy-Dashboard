"""Upload a farm bull workbook with Custom Index (CI) values.

Custom indexes and bulls that are only in the workbook are kept in AppSetting so
an AHDB refresh can rebuild the marketed lists and then put those bulls back
when they are still missing.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import re
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.database import AhdbBull, AppSetting, _clean_bull_name, _float_or_none, _str_or_none

logger = logging.getLogger(__name__)

BULL_UPLOAD_KEY = "genetics.bull_upload"
MAX_UPLOAD_BULLS = 20000

_HBN_ALIASES = ("HBN", "Herd Book Number", "Herd book number", "Bull HBN")
_CI_ALIASES = ("CI", "Custom Index", "Custom index", "£CI")
_NAME_ALIASES = ("Name", "Bull Name", "Bull name", "Bull Name Full")
_SUPPLIER_ALIASES = ("AIco GB", "AIco", "Supplier", "Supplier GB")
_SIRE_ALIASES = ("Sire Name", "Sire")
_MGS_ALIASES = ("MGS Name", "MGS", "Grandsire Name")

_TRAIT_ALIASES: dict[str, tuple[str, ...]] = {
    "rank": ("Rank",),
    "pli": ("PLI", "£PLI"),
    "milk_kg": ("Milk",),
    "fat_kg": ("Fat Kg", "Fat kg"),
    "protein_kg": ("Prot Kg", "Protein Kg", "Prot kg"),
    "fat_pct": ("Fat Pc", "Fat %", "Fat%"),
    "protein_pct": ("Prot Pc", "Prot %", "Protein %", "Prot%"),
    "fertility_index": ("Fertility Index", "FI"),
    "lifespan_days": ("Lifespan Days", "Lifespan", "LS"),
    "mastitis": ("Mastitis",),
    "lameness": ("Lameness", "Lame"),
    "envirocow": ("EnviroCow",),
    "healthycow": ("HealthyCow",),
    "calf_survival": ("CalfSurvival", "Calf Surv"),
    "scc": ("SCC",),
    "digital_dermatitis": ("DigitalDermatitis", "DD"),
    "gestation_length": ("GestationLength", "GL"),
    "feed_advantage": ("FeedAdvantage", "Feed Adv"),
    "maintenance": ("Maint", "Maintenance"),
    "direct_ce": ("dCE",),
    "maternal_ce": ("mCE",),
    "tb_advantage": ("TB Adv", "TB"),
    "legs": ("L&F", "Legs"),
    "udder": ("Mammary", "Udder"),
    "type_merit": ("Type Merit", "TM"),
}


def _col_key(name: str) -> str:
    text = str(name).replace("\ufeff", "").strip().lower()
    text = text.replace("£", "").replace("€", "")
    return re.sub(r"[^a-z0-9%]+", "", text)


def _column_lookup(columns: list[Any]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for col in columns:
        key = _col_key(col)
        if key and key not in lookup:
            lookup[key] = str(col)
    return lookup


def _resolve_column(lookup: dict[str, str], aliases: tuple[str, ...]) -> str | None:
    for alias in aliases:
        key = _col_key(alias)
        if key in lookup:
            return lookup[key]
    return None


def normalise_hbn(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value:
            return None
        if value.is_integer():
            return str(int(value))
        text = str(value).strip()
    else:
        text = str(value).strip()
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    return digits or text


def load_bull_upload(db: Session) -> dict[str, Any]:
    row = db.scalar(select(AppSetting).where(AppSetting.key == BULL_UPLOAD_KEY))
    if row is None or not (row.value or "").strip():
        return {"filename": "", "uploaded_at": None, "indexes": {}, "extras": {}}
    try:
        parsed = json.loads(row.value)
    except json.JSONDecodeError:
        return {"filename": "", "uploaded_at": None, "indexes": {}, "extras": {}}
    if not isinstance(parsed, dict):
        return {"filename": "", "uploaded_at": None, "indexes": {}, "extras": {}}
    indexes_raw = parsed.get("indexes") or {}
    extras_raw = parsed.get("extras") or {}
    indexes: dict[str, float] = {}
    if isinstance(indexes_raw, dict):
        for key, value in indexes_raw.items():
            hbn = normalise_hbn(key)
            number = _float_or_none(value)
            if hbn and number is not None:
                indexes[hbn] = number
    extras: dict[str, dict[str, Any]] = {}
    if isinstance(extras_raw, dict):
        for key, value in extras_raw.items():
            hbn = normalise_hbn(key)
            if hbn and isinstance(value, dict):
                extras[hbn] = value
    return {
        "filename": str(parsed.get("filename") or ""),
        "uploaded_at": parsed.get("uploaded_at"),
        "indexes": indexes,
        "extras": extras,
    }


def _store_bull_upload(db: Session, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False)
    row = db.scalar(select(AppSetting).where(AppSetting.key == BULL_UPLOAD_KEY))
    if row is None:
        db.add(AppSetting(key=BULL_UPLOAD_KEY, value=body))
    else:
        row.value = body
    db.flush()


def _read_workbook(file_bytes: bytes, filename: str) -> pd.DataFrame:
    suffix = Path(filename or "").suffix.lower()
    buffer = io.BytesIO(file_bytes)
    if suffix in {".xlsx", ".xls", ".xlsm"}:
        frame = pd.read_excel(buffer)
    elif suffix == ".csv":
        try:
            frame = pd.read_csv(buffer, encoding="utf-8-sig")
        except UnicodeDecodeError:
            buffer.seek(0)
            frame = pd.read_csv(buffer, encoding="latin-1")
    else:
        raise ValueError("Upload an Excel or CSV bull file.")
    frame = frame.dropna(how="all")
    if frame.empty:
        raise ValueError("That file has no rows.")
    return frame


def _cell(row: pd.Series, column: str | None) -> Any:
    if column is None or column not in row.index:
        return None
    value = row[column]
    if isinstance(value, float) and pd.isna(value):
        return None
    return value


def _snapshot_from_row(row: pd.Series, lookup: dict[str, str], hbn: str) -> dict[str, Any]:
    name_col = _resolve_column(lookup, _NAME_ALIASES)
    supplier_col = _resolve_column(lookup, _SUPPLIER_ALIASES)
    sire_col = _resolve_column(lookup, _SIRE_ALIASES)
    mgs_col = _resolve_column(lookup, _MGS_ALIASES)
    name = _clean_bull_name(_str_or_none(_cell(row, name_col))) or hbn
    payload: dict[str, Any] = {
        "list_type": "genomic",
        "hbn": hbn,
        "bull_name": name,
        "bull_name_full": _str_or_none(_cell(row, name_col)) or name,
        "supplier_gb": _str_or_none(_cell(row, supplier_col)),
        "sire_name": _str_or_none(_cell(row, sire_col)),
        "grandsire_name": _str_or_none(_cell(row, mgs_col)),
        "genomic_indicator": "G",
    }
    for field, aliases in _TRAIT_ALIASES.items():
        column = _resolve_column(lookup, aliases)
        value = _float_or_none(_cell(row, column))
        if field == "rank" and value is not None:
            payload[field] = int(value)
        elif value is not None:
            payload[field] = value
    return payload


def _bull_from_snapshot(snapshot: dict[str, Any], fetched_at: dt.datetime) -> AhdbBull:
    return AhdbBull(
        list_type=snapshot.get("list_type") or "genomic",
        hbn=str(snapshot["hbn"]),
        rank=snapshot.get("rank"),
        bull_name=_clean_bull_name(snapshot.get("bull_name")),
        bull_name_full=_str_or_none(snapshot.get("bull_name_full")),
        breed_code=_str_or_none(snapshot.get("breed_code")),
        pli=_float_or_none(snapshot.get("pli")),
        milk_kg=_float_or_none(snapshot.get("milk_kg")),
        fat_kg=_float_or_none(snapshot.get("fat_kg")),
        protein_kg=_float_or_none(snapshot.get("protein_kg")),
        fat_pct=_float_or_none(snapshot.get("fat_pct")),
        protein_pct=_float_or_none(snapshot.get("protein_pct")),
        healthycow=_float_or_none(snapshot.get("healthycow")),
        envirocow=_float_or_none(snapshot.get("envirocow")),
        fertility_index=_float_or_none(snapshot.get("fertility_index")),
        calf_survival=_float_or_none(snapshot.get("calf_survival")),
        lifespan_days=_float_or_none(snapshot.get("lifespan_days")),
        scc=_float_or_none(snapshot.get("scc")),
        mastitis=_float_or_none(snapshot.get("mastitis")),
        lameness=_float_or_none(snapshot.get("lameness")),
        digital_dermatitis=_float_or_none(snapshot.get("digital_dermatitis")),
        gestation_length=_float_or_none(snapshot.get("gestation_length")),
        maintenance=_float_or_none(snapshot.get("maintenance")),
        feed_advantage=_float_or_none(snapshot.get("feed_advantage")),
        direct_ce=_float_or_none(snapshot.get("direct_ce")),
        maternal_ce=_float_or_none(snapshot.get("maternal_ce")),
        tb_advantage=_float_or_none(snapshot.get("tb_advantage")),
        legs=_float_or_none(snapshot.get("legs")),
        udder=_float_or_none(snapshot.get("udder")),
        type_merit=_float_or_none(snapshot.get("type_merit")),
        supplier_gb=_str_or_none(snapshot.get("supplier_gb")),
        genomic_indicator=_str_or_none(snapshot.get("genomic_indicator")) or "G",
        sire_name=_str_or_none(snapshot.get("sire_name")),
        grandsire_name=_str_or_none(snapshot.get("grandsire_name")),
        fetched_at=fetched_at.replace(tzinfo=None) if fetched_at.tzinfo else fetched_at,
    )


def apply_upload_to_bulls(db: Session) -> int:
    """Add any uploaded-only bulls that are still missing after an AHDB rebuild."""
    upload = load_bull_upload(db)
    extras = upload.get("extras") or {}
    if not extras:
        return 0
    known = {hbn for hbn in db.scalars(select(AhdbBull.hbn)) if hbn}
    added = 0
    now = dt.datetime.now(dt.timezone.utc)
    kept: dict[str, dict[str, Any]] = {}
    for hbn, snapshot in extras.items():
        if hbn in known:
            continue
        kept[hbn] = snapshot
        db.add(_bull_from_snapshot(snapshot, now))
        known.add(hbn)
        added += 1
    if kept != extras:
        upload["extras"] = kept
        _store_bull_upload(db, upload)
    return added


def attach_custom_index(payload: dict[str, Any], indexes: dict[str, float]) -> dict[str, Any]:
    hbn = normalise_hbn(payload.get("hbn"))
    if hbn and hbn in indexes:
        payload["custom_index"] = indexes[hbn]
    else:
        payload["custom_index"] = None
    return payload


def import_bull_workbook(db: Session, file_bytes: bytes, filename: str) -> dict[str, Any]:
    """Replace the stored Custom Index upload and add any bulls missing from AHDB."""
    frame = _read_workbook(file_bytes, filename)
    lookup = _column_lookup(list(frame.columns))
    hbn_col = _resolve_column(lookup, _HBN_ALIASES)
    if hbn_col is None:
        raise ValueError("The bull file needs an HBN column.")
    ci_col = _resolve_column(lookup, _CI_ALIASES)
    if ci_col is None:
        raise ValueError("The bull file needs a CI (Custom Index) column.")

    known = {hbn for hbn in db.scalars(select(AhdbBull.hbn)) if hbn}
    indexes: dict[str, float] = {}
    extras: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    matched = 0
    added = 0
    skipped = 0
    now = dt.datetime.now(dt.timezone.utc)

    for _, row in frame.iterrows():
        hbn = normalise_hbn(_cell(row, hbn_col))
        if not hbn:
            skipped += 1
            continue
        if hbn in seen:
            continue
        seen.add(hbn)
        if len(seen) > MAX_UPLOAD_BULLS:
            raise ValueError(f"That file has more than {MAX_UPLOAD_BULLS:,} bulls.")
        ci = _float_or_none(_cell(row, ci_col))
        if ci is not None:
            indexes[hbn] = ci
        if hbn in known:
            matched += 1
            continue
        snapshot = _snapshot_from_row(row, lookup, hbn)
        extras[hbn] = snapshot
        db.add(_bull_from_snapshot(snapshot, now))
        known.add(hbn)
        added += 1

    if not seen:
        raise ValueError("No rows with an HBN were found in that file.")

    payload = {
        "filename": Path(filename).name,
        "uploaded_at": now.isoformat(),
        "indexes": indexes,
        "extras": extras,
    }
    _store_bull_upload(db, payload)
    logger.info(
        "Imported bull custom indexes from %s: %s indexes, %s matched, %s added",
        filename,
        len(indexes),
        matched,
        added,
    )
    return {
        "filename": payload["filename"],
        "rows_read": len(seen),
        "indexes": len(indexes),
        "matched": matched,
        "added": added,
        "skipped": skipped,
    }
