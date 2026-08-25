"""Import birth records from DCEXPORT CMBORN / GADBORN CSV files."""

from __future__ import annotations

import datetime as dt
import gc
import io
import logging
from typing import Any

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from services.database import HerdBirth
from services.herd_onedrive import (
    KIND_BIRTHS,
    download_dcexport_file,
    files_for_kind,
    herd_import_configured,
)
from services.herd_import_utils import (
    FP_BIRTHS,
    SHARED_HERD_SOURCE_FARM,
    SHARED_HERD_SPLIT_FARMS,
    bulk_insert_dataframe,
    birth_category_series,
    dedupe_birth_rows,
    drop_unnamed_columns,
    fiscal_year_from_dates,
    parse_date_series,
    remove_invalid_id_rows,
    shared_source_fingerprints_match,
    split_dataframe_by_bname,
    source_fingerprint,
    store_split_source_fingerprints,
)

logger = logging.getLogger(__name__)

_BIRTH_ENCODING = "windows-1252"
_BIRTH_REQUIRED_COLUMNS = ("ID", "ETAG", "BDAT", "CBRD", "GNDR")
_BIRTHS_SPLITTER = "bname-v1"


def _clean_birth_dataframe(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    df = drop_unnamed_columns(df)

    missing = [col for col in _BIRTH_REQUIRED_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns in birth data: {missing}")

    str_cols = df.select_dtypes(include="object").columns.tolist()
    if str_cols:
        df[str_cols] = (
            df[str_cols]
            .astype(str)
            .apply(lambda col: col.str.replace(r"[\s\xa0\t\r\n]+", " ", regex=True))
            .apply(lambda col: col.str.strip())
        )

    keep = list(_BIRTH_REQUIRED_COLUMNS) + ["Farm"]
    df = df[[col for col in keep if col in df.columns]].copy()
    df = remove_invalid_id_rows(df)

    bdat_as_str = df["BDAT"].astype(str).str.strip()
    valid_bdat = bdat_as_str.str.contains(r"[/-]", regex=True, na=False)
    df = df[valid_bdat].copy()

    df["BDAT"] = parse_date_series(df["BDAT"])
    df["Fiscal Year"] = fiscal_year_from_dates(df["BDAT"])
    df["CBRD"] = pd.to_numeric(df["CBRD"], errors="coerce").astype("Int64")
    df["Category"] = birth_category_series(df["CBRD"], df["GNDR"])

    return dedupe_birth_rows(df)


def _dataframe_to_mappings(df: pd.DataFrame, import_time: dt.datetime) -> list[dict[str, Any]]:
    def series_str(col: str) -> pd.Series:
        if col not in df.columns:
            return pd.Series([None] * len(df))
        s = df[col].astype("string").str.strip()
        return s.where(s.notna() & (s != ""), None)

    def series_date(col: str) -> pd.Series:
        if col not in df.columns:
            return pd.Series([None] * len(df))
        return parse_date_series(df[col]).dt.date.replace({pd.NaT: None})

    def series_int(col: str) -> pd.Series:
        if col not in df.columns:
            return pd.Series([None] * len(df))
        return pd.to_numeric(df[col], errors="coerce").astype("Int64")

    out = pd.DataFrame(
        {
            "cow_id": series_str("ID"),
            "etag": series_str("ETAG"),
            "bdat": series_date("BDAT"),
            "cbrd": series_int("CBRD"),
            "gndr": series_str("GNDR"),
            "category": series_str("Category"),
            "farm": series_str("Farm"),
            "fiscal_year": series_int("Fiscal Year"),
            "import_timestamp": import_time,
        }
    )
    return out.to_dict(orient="records")


def _import_farm_file(
    db: Session, entry: dict[str, Any], farm: str, import_time: dt.datetime
) -> tuple[dict[str, int], int]:
    file_bytes = download_dcexport_file(entry)
    df = pd.read_csv(
        io.BytesIO(file_bytes),
        encoding=_BIRTH_ENCODING,
        dayfirst=True,
        on_bad_lines="skip",
    )
    del file_bytes

    parts = split_dataframe_by_bname(df, source_farm=farm)
    del df
    split_mode = len(parts) > 1
    rows_by_farm: dict[str, int] = {}
    duplicates_dropped = 0

    for target_farm, part in parts.items():
        part["Farm"] = target_farm
        cleaned, dropped = _clean_birth_dataframe(part)
        duplicates_dropped += dropped
        if cleaned.empty:
            rows_by_farm[target_farm] = 0
            continue
        db.execute(delete(HerdBirth).where(HerdBirth.farm == target_farm))
        bulk_insert_dataframe(db, HerdBirth, cleaned, _dataframe_to_mappings, import_time)
        rows_by_farm[target_farm] = len(cleaned)
        del cleaned

    total = sum(rows_by_farm.values())
    if split_mode and total > 0:
        for target_farm in SHARED_HERD_SPLIT_FARMS:
            if rows_by_farm.get(target_farm, 0) == 0:
                db.execute(delete(HerdBirth).where(HerdBirth.farm == target_farm))
                rows_by_farm.setdefault(target_farm, 0)

    gc.collect()
    return rows_by_farm, duplicates_dropped


def import_herd_births(db: Session, *, force: bool = True) -> dict[str, Any]:
    """Download CM / GAD birth CSVs and replace those farms' herd_births rows.

    When ``force=False``, each farm is checked independently against its stored
    OneDrive last-modified fingerprint. Unchanged farms are left alone.
    """
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
    duplicate_rows_dropped = 0
    duplicate_rows_dropped_by_farm: dict[str, int] = {}

    entries = files_for_kind(KIND_BIRTHS)
    if not entries:
        logger.warning("No DCEXPORT *BORN.csv files found on OneDrive")
    for entry in entries:
        farm = entry["farm"]
        relative_path = entry["relative_path"]
        last_modified = entry.get("last_modified") or ""
        fingerprint = source_fingerprint(relative_path, last_modified, splitter=_BIRTHS_SPLITTER)
        sources.append(
            {
                "farm": farm,
                "source_file": relative_path,
                "last_modified": last_modified,
            }
        )

        if not force and last_modified and shared_source_fingerprints_match(
            db, FP_BIRTHS, fingerprint, farm
        ):
            skipped = (
                list(SHARED_HERD_SPLIT_FARMS)
                if farm == SHARED_HERD_SOURCE_FARM
                else [farm]
            )
            farms_skipped.extend(skipped)
            logger.info("Herd births %s unchanged; skipping", "+".join(skipped))
            continue

        rows_by_farm, dropped = _import_farm_file(db, entry, farm, import_time)
        rows = sum(rows_by_farm.values())
        if rows == 0:
            empty_source_farms.append(farm)
            logger.error(
                "Herd births %s file had 0 usable rows; leaving existing data",
                farm,
            )
            continue
        rows_imported += rows
        duplicate_rows_dropped += dropped
        imported_farms = list(rows_by_farm) or [farm]
        if dropped:
            for code, count in rows_by_farm.items():
                if count:
                    duplicate_rows_dropped_by_farm[code] = dropped
        store_split_source_fingerprints(db, FP_BIRTHS, fingerprint, imported_farms)
        farms_imported.extend(imported_farms)
        logger.info(
            "Herd births imported %s",
            ", ".join(f"{code}={count}" for code, count in sorted(rows_by_farm.items())),
        )

    farm_counts = dict(
        db.execute(select(HerdBirth.farm, func.count()).group_by(HerdBirth.farm)).all()
    )
    latest_birth = db.scalar(select(func.max(HerdBirth.bdat)))
    source_files = [item["source_file"] for item in sources]
    all_skipped = bool(farms_skipped) and not farms_imported

    if all_skipped:
        return {
            "skipped": True,
            "reason": "source_unchanged",
            "rows_imported": sum(int(v) for v in farm_counts.values()),
            "duplicate_rows_dropped": 0,
            "duplicate_rows_dropped_by_farm": {},
            "farm_counts": farm_counts,
            "farms_imported": [],
            "farms_skipped": farms_skipped,
            "empty_source_farms": empty_source_farms,
            "latest_birth_date": latest_birth.isoformat() if latest_birth else None,
            "imported_at": None,
            "source_files": source_files,
            "sources": sources,
        }

    return {
        "skipped": False,
        "rows_imported": rows_imported,
        "duplicate_rows_dropped": duplicate_rows_dropped,
        "duplicate_rows_dropped_by_farm": duplicate_rows_dropped_by_farm,
        "farm_counts": farm_counts,
        "farms_imported": farms_imported,
        "farms_skipped": farms_skipped,
        "empty_source_farms": empty_source_farms,
        "latest_birth_date": latest_birth.isoformat() if latest_birth else None,
        "imported_at": import_time.isoformat(timespec="seconds"),
        "source_files": source_files,
        "sources": sources,
    }
