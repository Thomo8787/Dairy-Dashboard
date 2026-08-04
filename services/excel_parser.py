"""Read and normalize Excel attachments with pandas."""

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# Map common Excel column headers to our normalized schema.
# Extend this mapping as you learn your supplier's file format.
COLUMN_ALIASES = {
    "date": "record_date",
    "record date": "record_date",
    "report date": "record_date",
    "category": "category",
    "product": "category",
    "product type": "category",
    "metric": "metric_name",
    "metric name": "metric_name",
    "description": "metric_name",
    "item": "metric_name",
    "value": "metric_value",
    "amount": "metric_value",
    "quantity": "metric_value",
    "volume": "metric_value",
    "litres": "metric_value",
    "liters": "metric_value",
    "unit": "unit",
    "uom": "unit",
    "notes": "notes",
    "comment": "notes",
    "comments": "notes",
}


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    renamed = {}
    for col in df.columns:
        key = str(col).strip().lower()
        renamed[col] = COLUMN_ALIASES.get(key, key.replace(" ", "_"))
    return df.rename(columns=renamed)


def _parse_date(value) -> datetime | None:
    if pd.isna(value):
        return None
    parsed = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(parsed):
        return None
    if parsed.tzinfo is None:
        return parsed.to_pydatetime().replace(tzinfo=timezone.utc)
    return parsed.to_pydatetime()


def _parse_float(value) -> float | None:
    if pd.isna(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        cleaned = str(value).replace(",", "").strip()
        if not cleaned:
            return None
        return float(cleaned)


def parse_excel_file(file_path: str | Path) -> list[dict]:
    """
    Parse an Excel file into normalized dairy records.

    Expects columns that can be mapped via COLUMN_ALIASES. Unmapped columns
    are ignored. If no recognized columns exist, each row is stored using
    the first two columns as metric_name / metric_value.
    """
    path = Path(file_path)
    df = pd.read_excel(path, engine="openpyxl")
    if df.empty:
        return []

    df = _normalize_columns(df)

    records: list[dict] = []
    for _, row in df.iterrows():
        record = {
            "record_date": _parse_date(row.get("record_date")),
            "category": _as_str(row.get("category")),
            "metric_name": _as_str(row.get("metric_name")),
            "metric_value": _parse_float(row.get("metric_value")),
            "unit": _as_str(row.get("unit")),
            "notes": _as_str(row.get("notes")),
        }

        # Fallback for unfamiliar spreadsheets: use first two columns.
        if not record["metric_name"] and len(df.columns) >= 1:
            record["metric_name"] = _as_str(row.iloc[0])
        if record["metric_value"] is None and len(df.columns) >= 2:
            record["metric_value"] = _parse_float(row.iloc[1])

        if record["metric_name"] or record["metric_value"] is not None:
            records.append(record)

    return records


def _as_str(value) -> str | None:
    if pd.isna(value):
        return None
    text = str(value).strip()
    return text or None
