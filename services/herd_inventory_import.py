"""Import current herd inventory from DCEXPORT *INV.CSV files."""

from __future__ import annotations

import datetime as dt
import gc
import io
import logging
from typing import Any

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from services.database import HerdInventory
from services.herd_import_utils import (
    FP_INVENTORY,
    birth_category_series,
    bulk_insert_dataframe,
    drop_unnamed_columns,
    load_source_fingerprint,
    parse_date_series,
    remove_invalid_id_rows,
    source_fingerprint,
    store_source_fingerprint,
    strip_string_columns,
)
from services.herd_onedrive import (
    KIND_INVENTORY,
    download_dcexport_file,
    files_for_kind,
    herd_import_configured,
)

logger = logging.getLogger(__name__)

_INVENTORY_ENCODING = "windows-1252"


def _clean_inventory_dataframe(df: pd.DataFrame, farm: str) -> pd.DataFrame:
    df = drop_unnamed_columns(df)
    df = strip_string_columns(df)
    if "ID" in df.columns:
        df = remove_invalid_id_rows(df)
    df["Farm"] = farm
    if "BDAT" in df.columns:
        df["BDAT"] = parse_date_series(df["BDAT"])
    if "FDAT" in df.columns:
        df["FDAT"] = parse_date_series(df["FDAT"])
    if "EDAT" in df.columns:
        df["EDAT"] = parse_date_series(df["EDAT"])
    if "CBRD" in df.columns:
        df["CBRD"] = pd.to_numeric(df["CBRD"], errors="coerce")
    if "CBRD" in df.columns and "GNDR" in df.columns:
        df["Category"] = birth_category_series(df["CBRD"], df["GNDR"])
        df["Gender"] = df["GNDR"]
    elif "GNDR" in df.columns:
        df["Category"] = None
        df["Gender"] = df["GNDR"]
    else:
        df["Category"] = None
        df["Gender"] = None
    return df


def _dataframe_to_mappings(df: pd.DataFrame, import_time: dt.datetime) -> list[dict[str, Any]]:
    def _empty() -> pd.Series:
        return pd.Series([None] * len(df), index=df.index, dtype="object")

    def series_str(col: str) -> pd.Series:
        if col not in df.columns:
            return _empty()
        s = df[col].astype("string").str.strip()
        return s.where(s.notna() & (s != ""), None)

    def series_date(col: str) -> pd.Series:
        if col not in df.columns:
            return _empty()
        return parse_date_series(df[col]).dt.date.replace({pd.NaT: None})

    def series_num(col: str) -> pd.Series:
        if col not in df.columns:
            return _empty()
        return pd.to_numeric(df[col], errors="coerce")

    out = pd.DataFrame(
        {
            "cow_id": series_str("ID"),
            "etag": series_str("ETAG"),
            "bdat": series_date("BDAT"),
            "edat": series_date("EDAT"),
            "cbrd": series_num("CBRD"),
            "sbrd": series_str("SBRD"),
            "fdat": series_date("FDAT"),
            "dim": series_num("DIM"),
            "lact": series_num("LACT"),
            "pen": series_str("PEN"),
            "remark": series_str("REMARK"),
            "farm": series_str("Farm"),
            "category": series_str("Category"),
            "gender": series_str("Gender"),
            "import_timestamp": import_time,
        }
    )
    return out.to_dict(orient="records")


def _import_farm_file(db: Session, entry: dict[str, Any], farm: str, import_time: dt.datetime) -> int:
    file_bytes = download_dcexport_file(entry)
    df = pd.read_csv(
        io.BytesIO(file_bytes),
        encoding=_INVENTORY_ENCODING,
        dayfirst=True,
        on_bad_lines="skip",
    )
    del file_bytes
    df = _clean_inventory_dataframe(df, farm)
    rows = len(df)
    if rows == 0:
        del df
        gc.collect()
        return 0
    db.execute(delete(HerdInventory).where(HerdInventory.farm == farm))
    bulk_insert_dataframe(db, HerdInventory, df, _dataframe_to_mappings, import_time)
    del df
    gc.collect()
    return rows


def import_herd_inventory(db: Session, *, force: bool = True) -> dict[str, Any]:
    if not herd_import_configured():
        raise ValueError(
            "Herd import is not configured. Set OneDrive Graph variables or LOCAL_HERD_EXPORT_DIR."
        )

    import_time = dt.datetime.now()
    sources: list[dict[str, str]] = []
    farms_imported: list[str] = []
    farms_skipped: list[str] = []
    empty_source_farms: list[str] = []
    rows_imported = 0

    entries = files_for_kind(KIND_INVENTORY)
    if not entries:
        logger.warning("No DCEXPORT *INV.csv files found on OneDrive")

    for entry in entries:
        farm = entry["farm"]
        relative_path = entry["relative_path"]
        last_modified = entry.get("last_modified") or ""
        fingerprint = source_fingerprint(relative_path, last_modified)
        sources.append(
            {
                "farm": farm,
                "source_file": relative_path,
                "last_modified": last_modified,
            }
        )
        if not force and last_modified:
            stored = load_source_fingerprint(db, FP_INVENTORY, farm)
            if stored == fingerprint:
                farms_skipped.append(farm)
                logger.info("Herd inventory %s unchanged; skipping", farm)
                continue

        rows = _import_farm_file(db, entry, farm, import_time)
        if rows == 0:
            empty_source_farms.append(farm)
            logger.error("Herd inventory %s file had 0 usable rows; leaving existing data", farm)
            continue
        rows_imported += rows
        store_source_fingerprint(db, FP_INVENTORY, farm, fingerprint)
        farms_imported.append(farm)

    farm_counts = dict(
        db.execute(select(HerdInventory.farm, func.count()).group_by(HerdInventory.farm)).all()
    )
    all_skipped = bool(farms_skipped) and not farms_imported
    return {
        "skipped": all_skipped,
        "reason": "source_unchanged" if all_skipped else None,
        "rows_imported": rows_imported if not all_skipped else sum(int(v) for v in farm_counts.values()),
        "farm_counts": farm_counts,
        "farms_imported": farms_imported,
        "farms_skipped": farms_skipped,
        "empty_source_farms": empty_source_farms,
        "imported_at": None if all_skipped else import_time.isoformat(timespec="seconds"),
        "source_files": [item["source_file"] for item in sources],
        "sources": sources,
    }
