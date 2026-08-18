"""Import current herd inventory from DCEXPORT *INV.CSV files."""

from __future__ import annotations

import datetime as dt
import gc
import logging
from typing import Any

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from services.database import HerdInventory
from services.herd_import_utils import (
    FP_INVENTORY,
    bulk_insert_dataframe,
    load_source_fingerprint,
    parse_date_series,
    source_fingerprint,
    store_source_fingerprint,
)
from services.herd_onedrive import (
    KIND_INVENTORY,
    download_dcexport_file,
    files_for_kind,
    herd_import_configured,
)
from services.inventory_processor import load_inventory_csv, process_inventory_file

logger = logging.getLogger(__name__)

# Bump this when inventory processing changes so cron reimports already-fingerprinted files.
_INVENTORY_PROCESSOR = "category-months-v1"


def inventory_source_fingerprint(source_file: str, last_modified: str) -> str:
    return source_fingerprint(source_file, last_modified, processor=_INVENTORY_PROCESSOR)


def _dataframe_to_mappings(df: pd.DataFrame, import_time: dt.datetime) -> list[dict[str, Any]]:
    df = df.reset_index(drop=True)

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

    def series_int(col: str) -> pd.Series:
        if col not in df.columns:
            return _empty()
        return pd.to_numeric(df[col], errors="coerce").astype("Int64")

    def series_float(col: str) -> pd.Series:
        if col not in df.columns:
            return _empty()
        return pd.to_numeric(df[col], errors="coerce")

    out = pd.DataFrame(
        {
            "cow_id": series_str("ID"),
            "etag": series_str("ETAG"),
            "bdat": series_date("BDAT"),
            "edat": series_date("EDAT"),
            "cbrd": series_float("CBRD"),
            "sbrd": series_str("SBRD"),
            "fdat": series_date("FDAT"),
            "dim": series_float("DIM"),
            "lact": series_float("LACT"),
            "hdat": series_date("HDAT"),
            "dslh": series_float("DSLH"),
            "rc": series_float("RC"),
            "rpro": series_str("RPRO"),
            "pen": series_str("PEN"),
            "tbrd": series_int("TBRD"),
            "remark": series_str("REMARK"),
            "ewgt": series_float("EWGT"),
            "httag": series_str("HTTAG"),
            "rum": series_float("RUM"),
            "dcc": series_float("DCC"),
            "due": series_date("DUE"),
            "lsir": series_str("LSIR"),
            "sirc": series_str("SIRC"),
            "lsbrd": series_str("LSBRD"),
            "farm": series_str("Farm"),
            "category": series_str("Category"),
            "gender": series_str("Gender"),
            "aged": series_int("AGED"),
            "months_old": series_int("Months Old"),
            "expected_due": series_date("Expected Due"),
            "fiscal_year_due": series_int("Fiscal Year Due"),
            "sort_key": series_int("Sort Key"),
            "expected_month": series_str("Expected Month"),
            "value": series_float("Value"),
            "import_timestamp": import_time,
        }
    )
    return out.to_dict(orient="records")


def _import_farm_file(db: Session, entry: dict[str, Any], farm: str, import_time: dt.datetime) -> int:
    file_bytes = download_dcexport_file(entry)
    df = load_inventory_csv(file_bytes)
    del file_bytes
    df = process_inventory_file(df, farm)
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
        fingerprint = inventory_source_fingerprint(relative_path, last_modified)
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
