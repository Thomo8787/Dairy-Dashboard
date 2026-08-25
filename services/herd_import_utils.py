"""Shared helpers for DCEXPORT herd CSV imports."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Callable

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.database import AppSetting

HERD_DATE_FORMAT = "%d/%m/%y"
HERD_DATE_FORMATS = ("%d/%m/%y", "%d/%m/%Y")
BATCH_SIZE = 2000
BEEF_CBREED_MIN = 102
CATEGORY_DAIRY = "Dairy"
CATEGORY_BEEF = "Beef"

# AppSetting key prefixes for per-farm OneDrive source fingerprints.
FP_INVENTORY = "herd_inventory.source_fingerprint"
FP_EVENTS = "herd_events.source_fingerprint"
FP_BIRTHS = "herd_births.source_fingerprint"

# ALH and BNK share one DairyComp; DCEXPORTALH files are split on BNAME.
SHARED_HERD_SOURCE_FARM = "ALH"
SHARED_HERD_SPLIT_FARMS: tuple[str, ...] = ("ALH", "BNK")
_BNAME_COLUMN_ALIASES = ("BNAME", "BARN NAME", "BARNNAME")
_BNK_BNAME_TOKENS = frozenset({"BNK", "BANK", "BANK FARM", "BNK FARM"})


def source_fingerprint(source_file: str, last_modified: str, **extra: str) -> str:
    """Stable JSON fingerprint of a herd export file identity + mtime."""
    payload = {"source_file": source_file, "last_modified": last_modified, **extra}
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def fingerprint_setting_key(prefix: str, farm: str) -> str:
    return f"{prefix}.{farm.upper()}"


def load_source_fingerprint(db: Session, prefix: str, farm: str) -> str | None:
    row = db.scalar(
        select(AppSetting).where(
            AppSetting.key == fingerprint_setting_key(prefix, farm)
        )
    )
    value = (row.value if row else None) or ""
    return value.strip() or None


def store_source_fingerprint(
    db: Session, prefix: str, farm: str, fingerprint: str
) -> None:
    key = fingerprint_setting_key(prefix, farm)
    row = db.scalar(select(AppSetting).where(AppSetting.key == key))
    if row is None:
        db.add(AppSetting(key=key, value=fingerprint))
    else:
        row.value = fingerprint


def bname_column(df: pd.DataFrame) -> str | None:
    lookup = {str(col).strip().upper(): col for col in df.columns}
    for alias in _BNAME_COLUMN_ALIASES:
        if alias in lookup:
            return lookup[alias]
    return None


def _normalize_bname(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip().upper()
    if text in {"", "-", "NAN", "NONE", "<NA>", "NAT"}:
        return ""
    return " ".join(text.split())


def farm_code_from_bname(value: Any, *, source_farm: str) -> str:
    """Map a DairyComp BNAME onto ALH or BNK when reading the shared ALH export."""
    if source_farm != SHARED_HERD_SOURCE_FARM:
        return source_farm
    text = _normalize_bname(value)
    if text in _BNK_BNAME_TOKENS or text.startswith("BNK") or "BANK" in text:
        return "BNK"
    return "ALH"


def is_shared_bname_split(df: pd.DataFrame, source_farm: str) -> bool:
    return source_farm == SHARED_HERD_SOURCE_FARM and bname_column(df) is not None


def split_targets_for_source(df: pd.DataFrame, source_farm: str) -> tuple[str, ...]:
    if is_shared_bname_split(df, source_farm):
        return SHARED_HERD_SPLIT_FARMS
    return (source_farm,)


def split_dataframe_by_bname(df: pd.DataFrame, *, source_farm: str) -> dict[str, pd.DataFrame]:
    """Split a shared ALH DairyComp frame into ALH / BNK; other BNAME values stay on ALH."""
    targets = split_targets_for_source(df, source_farm)
    if len(targets) == 1:
        out = df.copy()
        if "Farm" in out.columns:
            out["Farm"] = source_farm
        return {source_farm: out}

    col = bname_column(df)
    assigned = df[col].map(lambda value: farm_code_from_bname(value, source_farm=source_farm))
    parts: dict[str, pd.DataFrame] = {}
    for farm in targets:
        part = df.loc[assigned.eq(farm)].copy()
        if "Farm" in part.columns:
            part["Farm"] = farm
        parts[farm] = part
    return parts


def shared_source_fingerprints_match(
    db: Session, prefix: str, fingerprint: str, source_farm: str
) -> bool:
    """True when this source file was already imported at the current fingerprint.

    For DCEXPORTALH, BNK is also checked once a BNAME split has stored its
    fingerprint. If BNK has never been imported from this file, ALH matching is
    enough (pre-split exports).
    """
    source_ok = load_source_fingerprint(db, prefix, source_farm) == fingerprint
    if source_farm != SHARED_HERD_SOURCE_FARM or not source_ok:
        return source_ok
    bnk_stored = load_source_fingerprint(db, prefix, "BNK")
    return bnk_stored is None or bnk_stored == fingerprint


def store_split_source_fingerprints(
    db: Session, prefix: str, fingerprint: str, farms: list[str] | tuple[str, ...]
) -> None:
    for farm in farms:
        store_source_fingerprint(db, prefix, farm, fingerprint)


def category_from_birth(cbrd: int | float | None, gndr: str | None) -> str:
    """Dairy when CBRD < 102 and GNDR is F; all other rows are beef."""
    try:
        if cbrd is None or pd.isna(cbrd):
            return CATEGORY_BEEF
        code = int(cbrd)
    except (TypeError, ValueError):
        return CATEGORY_BEEF

    gender = str(gndr).strip().upper() if gndr is not None and not pd.isna(gndr) else ""
    if code < BEEF_CBREED_MIN and gender == "F":
        return CATEGORY_DAIRY
    return CATEGORY_BEEF


def birth_category_series(cbrd: pd.Series, gndr: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(cbrd, errors="coerce")
    gender = gndr.astype("string").str.strip().str.upper()
    dairy_mask = numeric.notna() & (numeric < BEEF_CBREED_MIN) & (gender == "F")
    out = pd.Series(CATEGORY_BEEF, index=cbrd.index, dtype="object")
    out.loc[dairy_mask] = CATEGORY_DAIRY
    return out


def parse_date_series(series: pd.Series) -> pd.Series:
    """Parse DC305 dates. Accepts dd/mm/yy and dd/mm/yyyy."""
    if pd.api.types.is_datetime64_any_dtype(series):
        return series
    text = series.astype("string").str.strip()
    blank = text.isna() | text.eq("") | text.eq("<NA>")
    parsed = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    remaining = ~blank
    for fmt in HERD_DATE_FORMATS:
        if not remaining.any():
            break
        attempt = pd.to_datetime(text[remaining], format=fmt, errors="coerce")
        ok = attempt.notna()
        if ok.any():
            parsed.loc[attempt.index[ok]] = attempt[ok]
            remaining = remaining.copy()
            remaining.loc[attempt.index[ok]] = False
    if remaining.any():
        fallback = pd.to_datetime(text[remaining], dayfirst=True, errors="coerce")
        ok = fallback.notna()
        if ok.any():
            parsed.loc[fallback.index[ok]] = fallback[ok]
    return parsed


def fiscal_year_from_dates(dates: pd.Series) -> pd.Series:
    month = dates.dt.month
    year = dates.dt.year
    return year.where(month < 4, year + 1).astype("Int64")


def strip_string_columns(df: pd.DataFrame) -> pd.DataFrame:
    str_cols = df.select_dtypes(include="object").columns
    if len(str_cols):
        df[str_cols] = df[str_cols].apply(lambda col: col.str.strip())
    return df


def drop_unnamed_columns(df: pd.DataFrame) -> pd.DataFrame:
    return df.loc[:, ~df.columns.astype(str).str.match(r"^Unnamed")]


def remove_invalid_id_rows(df: pd.DataFrame, id_column: str = "ID") -> pd.DataFrame:
    if id_column not in df.columns:
        return df
    ids = df[id_column].astype(str).str.strip()
    return df[
        ids.str.upper().ne("ID")
        & ids.ne("")
        & ids.str.lower().ne("nan")
    ].copy()


def dedupe_birth_rows(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drop duplicate birth rows per farm; keep the first occurrence in file order."""
    if df.empty:
        return df, 0

    before = len(df)
    etag = df["ETAG"].astype(str).str.strip()
    has_etag = etag.ne("") & etag.str.lower().ne("nan")

    with_etag = df[has_etag].drop_duplicates(subset=["Farm", "ETAG"], keep="first")
    without_etag = df[~has_etag].drop_duplicates(subset=["Farm", "ID", "BDAT"], keep="first")
    out = pd.concat([with_etag, without_etag], ignore_index=True)
    return out, before - len(out)


def _dedupe_matching_event_rows(
    df: pd.DataFrame,
    match: pd.Series,
) -> tuple[pd.DataFrame, int]:
    """Drop duplicate rows per farm/animal/date/lact/event; keep first in file order."""
    if df.empty or not match.any():
        return df, 0

    before = len(df)
    matched = df[match]
    other = df[~match]

    etag = (
        matched["ETAG"].astype(str).str.strip()
        if "ETAG" in matched.columns
        else pd.Series([""] * len(matched), index=matched.index)
    )
    has_etag = etag.ne("") & etag.str.lower().ne("nan")

    dedupe_cols_etag = ["Farm", "ETAG", "Date", "LACT", "Event"]
    dedupe_cols_id = ["Farm", "ID", "Date", "LACT", "Event"]

    with_etag = matched[has_etag].drop_duplicates(subset=dedupe_cols_etag, keep="first")
    without_etag = matched[~has_etag].drop_duplicates(subset=dedupe_cols_id, keep="first")
    deduped = pd.concat([with_etag, without_etag]).sort_index()
    out = pd.concat([other, deduped]).sort_index()
    return out, before - len(out)


def dedupe_fresh_event_rows(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drop duplicate FRESH rows per farm/animal/date/lact; keep first in file order."""
    if df.empty or "Event" not in df.columns:
        return df, 0
    fresh_mask = df["Event"].astype(str).str.strip().str.upper().eq("FRESH")
    return _dedupe_matching_event_rows(df, fresh_mask)


def dedupe_exit_event_rows(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drop duplicate SOLD/DIED rows per farm/animal/date/lact; keep first in file order."""
    if df.empty or "Event" not in df.columns:
        return df, 0
    exit_mask = df["Event"].astype(str).str.strip().str.upper().isin({"SOLD", "DIED"})
    return _dedupe_matching_event_rows(df, exit_mask)


def normalize_mapping_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
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


def bulk_insert_dataframe(
    db: Session,
    model: type,
    df: pd.DataFrame,
    mapping_fn: Callable[[pd.DataFrame, dt.datetime], list[dict[str, Any]]],
    import_time: dt.datetime,
) -> None:
    for start in range(0, len(df), BATCH_SIZE):
        batch = df.iloc[start : start + BATCH_SIZE]
        mappings = normalize_mapping_records(mapping_fn(batch, import_time))
        db.bulk_insert_mappings(model, mappings)
