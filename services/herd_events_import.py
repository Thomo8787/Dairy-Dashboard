"""Import cow events from DCEXPORT CMEVENTS / GADEVENTS CSV files."""

from __future__ import annotations

import datetime as dt
import gc
import io
import logging
from typing import Any

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from services.database import CowEvent
from services.herd_onedrive import (
    KIND_EVENTS,
    download_dcexport_file,
    files_for_kind,
    herd_import_configured,
)
from services.herd_import_utils import (
    FP_EVENTS,
    dedupe_exit_event_rows,
    dedupe_fresh_event_rows,
    load_source_fingerprint,
    parse_date_series,
    source_fingerprint,
    store_source_fingerprint,
)

logger = logging.getLogger(__name__)

_BATCH_SIZE = 2000
_CSV_CHUNK_SIZE = 25_000
_EVENT_DATE_COLUMNS = ("BDAT", "FDAT", "EDAT", "Date")
# Bump this when the date parser changes so cron reimports already-fingerprinted files.
_EVENTS_DATE_PARSER = "yy-yyyy"


def events_source_fingerprint(source_file: str, last_modified: str) -> str:
    return source_fingerprint(source_file, last_modified, parser=_EVENTS_DATE_PARSER)


def _clean_events_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    str_cols = df.select_dtypes(include="object").columns
    df[str_cols] = df[str_cols].apply(lambda col: col.str.strip())

    for col in _EVENT_DATE_COLUMNS:
        if col in df.columns:
            df[col] = parse_date_series(df[col])

    if "CBRD" in df.columns:
        df["CBRD"] = pd.to_numeric(df["CBRD"], errors="coerce").fillna(0).astype("Int64")

    if "GNDR" in df.columns:
        df["GNDR"] = df["GNDR"].replace(["", None], "F")

    if "Date" in df.columns:
        df["mmm-yy"] = df["Date"].dt.strftime("%b-%y").where(df["Date"].notna(), "")
        month = df["Date"].dt.month
        year = df["Date"].dt.year
        df["Fiscal Year"] = year.where(month < 4, year + 1).astype("Int64")
        adjusted_month = (month - 4).where(month >= 4, month + 9)
        valid = df["Date"].notna() & df["Fiscal Year"].notna()
        df["Sort Key"] = (df["Fiscal Year"] * 100 + adjusted_month).astype("Int64")
        df.loc[~valid, "Sort Key"] = pd.NA
    else:
        df["mmm-yy"] = ""
        df["Fiscal Year"] = pd.Series([pd.NA] * len(df), dtype="Int64")
        df["Sort Key"] = pd.Series([pd.NA] * len(df), dtype="Int64")

    if "LACT" in df.columns:
        df["Parity"] = None
        df.loc[df["LACT"] == 0, "Parity"] = "Primiparous"
        df.loc[df["LACT"].notna() & (df["LACT"] != 0), "Parity"] = "Multiparous"
    else:
        df["Parity"] = None

    event = df["Event"] if "Event" in df.columns else pd.Series([""] * len(df), index=df.index)
    # Keep original DairyComp SOLD/DIED. Sales reporting treats DIED+TB/OFS
    # (remark or DEST Cneild/CSarg) as sales; BCMS needs the true DIED type.

    if {"FDAT", "Date", "LACT"}.issubset(df.columns):
        fresh_mask = (
            event.str.upper().eq("ABORT")
            & df["FDAT"].notna()
            & df["Date"].notna()
            & (df["FDAT"] == df["Date"])
            & (df["LACT"] == 1)
        )
        df.loc[fresh_mask, "Event"] = "FRESH"

    if {"Date", "EDAT"}.issubset(df.columns):
        invalid_edat = df["Date"].notna() & df["EDAT"].notna() & (df["Date"] < df["EDAT"])
        df = df.loc[~invalid_edat]

    df, _ = dedupe_fresh_event_rows(df)
    df, _ = dedupe_exit_event_rows(df)
    return df


def _remove_duplicate_cow_events(
    db: Session, event: str, farms: list[str] | None = None
) -> int:
    """Delete duplicate dated rows; keep the lowest id per farm/animal/date/lact.

    Rows with a null event date are left alone. They are not duplicates of dated
    rows, and deleting them used to wipe calvings/sales/deaths after a bad date parse.
    """
    animal_key = func.coalesce(CowEvent.etag, CowEvent.cow_id)
    keep_ids = (
        select(func.min(CowEvent.id))
        .where(CowEvent.event == event)
        .where(CowEvent.event_date.isnot(None))
        .group_by(CowEvent.farm, animal_key, CowEvent.event_date, CowEvent.lact)
    )
    if farms:
        keep_ids = keep_ids.where(CowEvent.farm.in_(farms))
    stmt = delete(CowEvent).where(
        CowEvent.event == event,
        CowEvent.event_date.isnot(None),
        ~CowEvent.id.in_(keep_ids),
    )
    if farms:
        stmt = stmt.where(CowEvent.farm.in_(farms))
    result = db.execute(stmt)
    return int(result.rowcount or 0)


def remove_duplicate_fresh_cow_events(
    db: Session, farms: list[str] | None = None
) -> int:
    """Delete duplicate FRESH rows in cow_events; keep the lowest id per animal/date/lact."""
    return _remove_duplicate_cow_events(db, "FRESH", farms=farms)


def remove_duplicate_exit_cow_events(
    db: Session, farms: list[str] | None = None
) -> int:
    """Delete duplicate SOLD/DIED rows in cow_events."""
    return _remove_duplicate_cow_events(db, "SOLD", farms=farms) + _remove_duplicate_cow_events(
        db, "DIED", farms=farms
    )


def _dataframe_to_mappings(
    df: pd.DataFrame, import_time: dt.datetime, farm: str
) -> list[dict[str, Any]]:
    """Convert cleaned dataframe to dicts for bulk_insert_mappings."""

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

    def series_float(col: str) -> pd.Series:
        if col not in df.columns:
            return pd.Series([None] * len(df))
        return pd.to_numeric(df[col], errors="coerce")

    out = pd.DataFrame(
        {
            "cow_id": series_str("ID"),
            "etag": series_str("ETAG"),
            "bdat": series_date("BDAT"),
            "fdat": series_date("FDAT"),
            "lact": series_int("LACT"),
            "gndr": series_str("GNDR"),
            "edat": series_date("EDAT"),
            "event": series_str("Event"),
            "dim": series_float("DIM"),
            "event_date": series_date("Date"),
            "remark": series_str("Remark"),
            "r": series_str("R"),
            "t": series_str("T"),
            "b": series_str("B"),
            "protocols": series_str("Protocols"),
            "technician": series_str("Technician"),
            "dest": series_str("DEST"),
            "farm": farm,
            "month_label": series_str("mmm-yy"),
            "fiscal_year": series_int("Fiscal Year"),
            "sort_key": series_int("Sort Key"),
            "parity": series_str("Parity"),
            "cbrd": series_int("CBRD"),
            "import_timestamp": import_time,
        }
    )
    records = out.to_dict(orient="records")
    for row in records:
        for key, val in list(row.items()):
            if pd.isna(val):
                row[key] = None
            elif hasattr(val, "item"):
                try:
                    row[key] = val.item()
                except (ValueError, AttributeError):
                    pass
    return records


def _insert_dataframe_in_batches(
    db: Session, df: pd.DataFrame, import_time: dt.datetime, farm: str
) -> None:
    """Insert rows without building one giant mappings list in memory."""
    for start in range(0, len(df), _BATCH_SIZE):
        batch = df.iloc[start : start + _BATCH_SIZE]
        mappings = _dataframe_to_mappings(batch, import_time, farm)
        db.bulk_insert_mappings(CowEvent, mappings)


def _import_farm_file(
    db: Session, entry: dict[str, Any], farm: str, import_time: dt.datetime
) -> int:
    """Download, clean, and replace one farm's events.

    Existing rows are deleted only after at least one usable CSV row is parsed,
    so an empty or half-written DC305 export cannot wipe the farm.
    """
    file_bytes = download_dcexport_file(entry)
    buffer = io.BytesIO(file_bytes)
    del file_bytes

    rows_imported = 0
    replaced = False
    for chunk in pd.read_csv(
        buffer,
        encoding="utf-8",
        dayfirst=True,
        on_bad_lines="skip",
        chunksize=_CSV_CHUNK_SIZE,
    ):
        chunk["Farm"] = farm
        chunk = _clean_events_dataframe(chunk)
        if chunk.empty:
            continue
        if not replaced:
            db.execute(delete(CowEvent).where(CowEvent.farm == farm))
            replaced = True
        _insert_dataframe_in_batches(db, chunk, import_time, farm)
        rows_imported += len(chunk)
        del chunk

    del buffer
    gc.collect()
    return rows_imported


def import_cow_events(db: Session, *, force: bool = True) -> dict[str, Any]:
    """Download CM / GAD event CSVs and replace those farms' cow_events rows.

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

    entries = files_for_kind(KIND_EVENTS)
    if not entries:
        logger.warning("No DCEXPORT *EVENTS.csv files found on OneDrive")
    for entry in entries:
        farm = entry["farm"]
        relative_path = entry["relative_path"]
        last_modified = entry.get("last_modified") or ""
        fingerprint = events_source_fingerprint(relative_path, last_modified)
        sources.append(
            {
                "farm": farm,
                "source_file": relative_path,
                "last_modified": last_modified,
            }
        )

        if not force and last_modified:
            stored = load_source_fingerprint(db, FP_EVENTS, farm)
            if stored == fingerprint:
                farms_skipped.append(farm)
                logger.info("Herd events %s unchanged; skipping", farm)
                continue

        rows = _import_farm_file(db, entry, farm, import_time)
        if rows == 0:
            empty_source_farms.append(farm)
            logger.error(
                "Herd events %s file had 0 usable rows; leaving existing data",
                farm,
            )
            continue

        rows_imported += rows
        store_source_fingerprint(db, FP_EVENTS, farm, fingerprint)
        farms_imported.append(farm)

    if farms_imported:
        duplicate_fresh_dropped = remove_duplicate_fresh_cow_events(
            db, farms=farms_imported
        )
        duplicate_exit_dropped = remove_duplicate_exit_cow_events(
            db, farms=farms_imported
        )
        purchase_stats = {}
    else:
        duplicate_fresh_dropped = 0
        duplicate_exit_dropped = 0
        purchase_stats = {}

    farm_counts = dict(
        db.execute(
            select(CowEvent.farm, func.count()).group_by(CowEvent.farm)
        ).all()
    )
    latest_date = db.scalar(select(func.max(CowEvent.event_date)))
    source_files = [item["source_file"] for item in sources]
    all_skipped = bool(farms_skipped) and not farms_imported

    if all_skipped:
        return {
            "skipped": True,
            "reason": "source_unchanged",
            "rows_imported": sum(int(v) for v in farm_counts.values()),
            "duplicate_fresh_dropped": 0,
            "duplicate_exit_dropped": 0,
            "farm_counts": farm_counts,
            "farms_imported": [],
            "farms_skipped": farms_skipped,
            "empty_source_farms": empty_source_farms,
            "latest_event_date": latest_date.isoformat() if latest_date else None,
            "imported_at": None,
            "source_files": source_files,
            "sources": sources,
            "purchase_stats": {},
        }

    return {
        "skipped": False,
        "rows_imported": rows_imported,
        "duplicate_fresh_dropped": duplicate_fresh_dropped,
        "duplicate_exit_dropped": duplicate_exit_dropped,
        "farm_counts": farm_counts,
        "farms_imported": farms_imported,
        "farms_skipped": farms_skipped,
        "empty_source_farms": empty_source_farms,
        "latest_event_date": latest_date.isoformat() if latest_date else None,
        "imported_at": import_time.isoformat(timespec="seconds"),
        "source_files": source_files,
        "sources": sources,
        "purchase_stats": purchase_stats,
    }
