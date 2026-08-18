"""Import AHDB genomic evaluations from OneDrive Genetics/animals_ahdb."""

from __future__ import annotations

import datetime as dt
import gc
import io
import logging
import re
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from services.database import AppSetting, GenomicResult
from services.herd_import_utils import (
    bulk_insert_dataframe,
    source_fingerprint,
)
from services.herd_onedrive import (
    _download_graph_item,
    herd_import_config_error,
    local_herd_export_dir,
)
from services.graph_onedrive import GraphOneDriveService

logger = logging.getLogger(__name__)

GENETICS_FOLDER = "Genetics"
ANIMALS_AHDB_STEM = "animals_ahdb"
GENOMIC_SOURCE_SETTING_KEY = "genomic_results.source_fingerprint"
EARTAG_MATCH_DIGITS = 12

# GenomicResult field -> candidate source column names (matched after _col_key).
TRAIT_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "milk_kg": ("Milk",),
    "fat_kg": ("Fat",),
    "protein_kg": ("Protein",),
    "fat_pct": ("Fat %", "Fat%"),
    "protein_pct": ("Protein %", "Protein%"),
    "pli": ("PLI", "£PLI"),
    "cci": ("CCI", "ACI"),
    "fertility_index": ("Fertility Index", "FI"),
    "scc": ("Somatic Cell Count", "SCC"),
    "life_span": ("Lifespan", "Life Span"),
    "mastitis": ("Mastitis",),
    "milking_speed": ("Ease of Milk", "Milking Speed"),
    "type_merit": ("Type Merit", "Type"),
    "mammary": ("Mammary",),
    "legs_and_feet": ("Legs and Feet",),
    "stature": ("Stature",),
    "chest_width": ("Chest Width",),
    "body_depth": ("Body Depth",),
    "mature_weight": ("Mature Weight",),
}

EARTAG_ALIASES = ("Eartag", "EarTag", "Ear Tag", "EarTag Number", "Ear Tag Number")
SIRE_NAME_ALIASES = ("Sire full name", "Sire")
SIRE_REG_ALIASES = ("Sire reg number", "Sire Reg No ID", "Sire NAAB code")


def _col_key(name: str) -> str:
    text = str(name).replace("\ufeff", "").strip().lower()
    text = text.replace("£", "").replace("€", "")
    return re.sub(r"[^a-z0-9%]+", "", text)


def eartag_match_key(value: Any) -> str | None:
    """Whitespace-strip, then last 12 digits of Eartag / inventory etag."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            text = str(int(value))
        except (TypeError, ValueError, OverflowError):
            text = str(value).strip()
    else:
        text = str(value).strip()
    digits = re.sub(r"\D", "", text)
    if not digits:
        return None
    return digits[-EARTAG_MATCH_DIGITS:]


def _column_lookup(df: pd.DataFrame) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for col in df.columns:
        key = _col_key(col)
        if key and key not in lookup:
            lookup[key] = col
    return lookup


def _resolve_column(lookup: dict[str, str], aliases: tuple[str, ...]) -> str | None:
    for alias in aliases:
        key = _col_key(alias)
        if key in lookup:
            return lookup[key]
    return None


def _load_stored_fingerprint(db: Session) -> str | None:
    row = db.scalar(select(AppSetting).where(AppSetting.key == GENOMIC_SOURCE_SETTING_KEY))
    value = (row.value if row else None) or ""
    return value.strip() or None


def _store_fingerprint(db: Session, fingerprint: str) -> None:
    row = db.scalar(select(AppSetting).where(AppSetting.key == GENOMIC_SOURCE_SETTING_KEY))
    if row is None:
        db.add(AppSetting(key=GENOMIC_SOURCE_SETTING_KEY, value=fingerprint))
    else:
        row.value = fingerprint


def _local_mtime_iso(path: Path) -> str:
    mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
    return mtime.isoformat().replace("+00:00", "Z")


def _find_animals_ahdb_file(folder: Path) -> Path | None:
    matches = [
        path
        for path in folder.iterdir()
        if path.is_file() and ANIMALS_AHDB_STEM in path.stem.lower()
    ]
    if not matches:
        return None
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


def discover_animals_ahdb() -> dict[str, Any]:
    """Locate Genetics/animals_ahdb on local disk or OneDrive."""
    local_root = local_herd_export_dir()
    if local_root is not None:
        folder = None
        for child in local_root.iterdir():
            if child.is_dir() and child.name.lower() == GENETICS_FOLDER.lower():
                folder = child
                break
        if folder is None:
            raise FileNotFoundError(
                f"No {GENETICS_FOLDER} folder under LOCAL_HERD_EXPORT_DIR ({local_root})."
            )
        path = _find_animals_ahdb_file(folder)
        if path is None:
            raise FileNotFoundError(
                f"No {ANIMALS_AHDB_STEM} file in {folder}."
            )
        return {
            "relative_path": f"{folder.name}/{path.name}",
            "last_modified": _local_mtime_iso(path),
            "local_path": path,
        }

    service = GraphOneDriveService()
    genetics = None
    for item in service._list_folder_page(service._root_children_url()):
        name = item.get("name") or ""
        if item.get("folder") and name.lower() == GENETICS_FOLDER.lower():
            genetics = item
            break
    if genetics is None:
        raise FileNotFoundError(
            f"No {GENETICS_FOLDER} folder on Parlours OneDrive."
        )

    parent = genetics.get("parentReference") or {}
    drive_id = parent.get("driveId") or service._drive_id
    folder_id = genetics.get("id")
    if not drive_id or not folder_id:
        raise FileNotFoundError(f"Could not open OneDrive {GENETICS_FOLDER} folder.")

    children = service._list_folder_page(service._item_children_url(drive_id, folder_id))
    matches = [
        child
        for child in children
        if child.get("file") and ANIMALS_AHDB_STEM in (child.get("name") or "").lower()
    ]
    if not matches:
        raise FileNotFoundError(
            f"No {ANIMALS_AHDB_STEM} file in OneDrive {GENETICS_FOLDER}."
        )
    matches.sort(key=lambda item: item.get("lastModifiedDateTime") or "", reverse=True)
    file_item = matches[0]
    name = file_item.get("name") or ANIMALS_AHDB_STEM
    return {
        "relative_path": f"{genetics.get('name') or GENETICS_FOLDER}/{name}",
        "last_modified": file_item.get("lastModifiedDateTime") or "",
        "graph_item": file_item,
        "service": service,
    }


def download_animals_ahdb(entry: dict[str, Any]) -> bytes:
    local_path = entry.get("local_path")
    if local_path is not None:
        return Path(local_path).read_bytes()
    item = entry.get("graph_item")
    service = entry.get("service") or GraphOneDriveService()
    if not item:
        raise FileNotFoundError(f"No download source for {entry.get('relative_path')}")
    return _download_graph_item(service, item)


def _read_animals_frame(file_bytes: bytes, source_name: str) -> pd.DataFrame:
    suffix = Path(source_name).suffix.lower()
    buffer = io.BytesIO(file_bytes)
    if suffix in {".xlsx", ".xls", ".xlsm"}:
        return pd.read_excel(buffer)
    try:
        return pd.read_csv(buffer, encoding="utf-8-sig")
    except UnicodeDecodeError:
        buffer.seek(0)
        return pd.read_csv(buffer, encoding="latin-1")


def _dataframe_to_mappings(df: pd.DataFrame, import_time: dt.datetime) -> list[dict[str, Any]]:
    df = df.reset_index(drop=True)
    lookup = _column_lookup(df)
    eartag_col = _resolve_column(lookup, EARTAG_ALIASES)
    if eartag_col is None:
        raise ValueError(
            "animals_ahdb is missing an Eartag column. Found: "
            + ", ".join(str(c) for c in df.columns[:20])
        )

    def _empty() -> pd.Series:
        return pd.Series([None] * len(df), index=df.index, dtype="object")

    def series_str(aliases: tuple[str, ...]) -> pd.Series:
        col = _resolve_column(lookup, aliases)
        if col is None:
            return _empty()
        s = df[col].astype("string").str.strip()
        return s.where(s.notna() & (s != ""), None)

    def series_float(aliases: tuple[str, ...]) -> pd.Series:
        col = _resolve_column(lookup, aliases)
        if col is None:
            return _empty()
        return pd.to_numeric(df[col], errors="coerce")

    out = pd.DataFrame(
        {
            "hbn": df[eartag_col].map(eartag_match_key),
            "eartag": series_str(EARTAG_ALIASES),
            "sire_name": series_str(SIRE_NAME_ALIASES),
            "sire_reg": series_str(SIRE_REG_ALIASES),
            "updated_at": pd.Series([import_time] * len(df), index=df.index),
        }
    )
    for field, aliases in TRAIT_COLUMN_ALIASES.items():
        out[field] = series_float(aliases)

    out = out[out["hbn"].notna() & (out["hbn"] != "")]
    out = out.drop_duplicates(subset=["hbn"], keep="last")
    return out.to_dict(orient="records")


def import_genomic_results(db: Session, *, force: bool = False) -> dict[str, Any]:
    """Download Genetics/animals_ahdb and replace genomic_results."""
    config_error = herd_import_config_error()
    if config_error:
        raise ValueError(config_error)

    entry = discover_animals_ahdb()
    source_file = entry["relative_path"]
    last_modified = entry.get("last_modified") or ""
    fingerprint = source_fingerprint(source_file, last_modified)

    if not force and last_modified:
        stored = _load_stored_fingerprint(db)
        if stored == fingerprint:
            row_count = db.scalar(select(func.count()).select_from(GenomicResult)) or 0
            logger.info("Genomic results unchanged; skipping (%s rows)", row_count)
            return {
                "skipped": True,
                "reason": "source_unchanged",
                "rows_imported": int(row_count),
                "imported_at": None,
                "source_file": source_file,
                "last_modified": last_modified,
            }

    file_bytes = download_animals_ahdb(entry)
    df = _read_animals_frame(file_bytes, source_file)
    del file_bytes
    df.columns = [str(col).replace("\ufeff", "").strip() for col in df.columns]
    lookup = _column_lookup(df)
    eartag_col = _resolve_column(lookup, EARTAG_ALIASES)
    if eartag_col is None:
        raise ValueError(
            "animals_ahdb is missing an Eartag column. Found: "
            + ", ".join(str(c) for c in list(df.columns)[:25])
        )
    usable = int(df[eartag_col].map(eartag_match_key).notna().sum())
    if usable == 0:
        raise ValueError("animals_ahdb has no usable Eartag values after stripping last 12 digits.")

    import_time = dt.datetime.now(dt.timezone.utc)
    db.execute(delete(GenomicResult))
    bulk_insert_dataframe(db, GenomicResult, df, _dataframe_to_mappings, import_time)
    rows_imported = db.scalar(select(func.count()).select_from(GenomicResult)) or 0
    del df
    gc.collect()
    _store_fingerprint(db, fingerprint)

    logger.info("Imported %s genomic results from %s", rows_imported, source_file)
    return {
        "skipped": False,
        "rows_imported": int(rows_imported),
        "imported_at": import_time.isoformat(timespec="seconds"),
        "source_file": source_file,
        "last_modified": last_modified,
    }
