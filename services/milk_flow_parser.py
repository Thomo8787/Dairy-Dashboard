"""Parse DataFlow Milk Flow Report CSV attachments."""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

COLUMN_MAP = {
    "cow number": "cow_number",
    "average milk flow during milking": "avg_milk_flow_l_per_min",
    "date": "milking_date",
    "shift": "shift",
    "dim": "dim",
    "shift yield": "shift_yield_l",
    "peak milk flow during milking": "peak_milk_flow_l_per_min",
    "peak milk flow during milking time (mm:ss)": "peak_milk_flow_time",
    "flow rate at 15 seconds": "flow_rate_15s_ml_per_min",
    "flow rate at 30 seconds": "flow_rate_30s_ml_per_min",
    "flow rate at 60 seconds": "flow_rate_60s_ml_per_min",
    "flow rate at 120 seconds": "flow_rate_120s_ml_per_min",
    "percentage yield at 2 minutes": "percentage_yield_at_2_min",
    "milk yield at 2 minutes": "milk_yield_at_2_min_l",
    "group number": "group_number",
    "flow rate at removal": "flow_rate_at_removal_ml_per_min",
    "individual milking time by shift": "unit_on_time",
    "cow milking start time": "cow_milking_start_time",
    "final detaching": "final_detaching",
    "milking point": "milking_point",
}

FLOAT_FIELDS = {
    "avg_milk_flow_l_per_min",
    "shift_yield_l",
    "peak_milk_flow_l_per_min",
    "flow_rate_15s_ml_per_min",
    "flow_rate_30s_ml_per_min",
    "flow_rate_60s_ml_per_min",
    "flow_rate_120s_ml_per_min",
    "percentage_yield_at_2_min",
    "milk_yield_at_2_min_l",
    "flow_rate_at_removal_ml_per_min",
}


def _decode_csv_bytes(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _parse_date(value: str) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_float(value: str) -> float | None:
    text = (value or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_int(value: str) -> int | None:
    number = _parse_float(value)
    if number is None:
        return None
    return int(number)


def _normalize_header(name: str) -> str:
    return " ".join((name or "").strip().lower().split())


def parse_milk_flow_csv(file_path: str | Path, farm_code: str = "ALH") -> list[dict]:
    path = Path(file_path)
    text = _decode_csv_bytes(path.read_bytes())
    reader = csv.reader(text.splitlines())
    rows = list(reader)
    if not rows:
        return []

    headers = [_normalize_header(h) for h in rows[0]]
    field_indexes: dict[str, int] = {}
    for idx, header in enumerate(headers):
        mapped = COLUMN_MAP.get(header)
        if mapped:
            field_indexes[mapped] = idx

    required = {"cow_number", "milking_date"}
    if not required.issubset(field_indexes):
        raise RuntimeError(
            f"Milk Flow CSV missing required columns. Found headers: {rows[0]!r}"
        )

    records: list[dict] = []
    for raw in rows[1:]:
        if not raw or all(not str(cell).strip() for cell in raw):
            continue

        def get(field: str) -> str:
            idx = field_indexes.get(field)
            if idx is None or idx >= len(raw):
                return ""
            return str(raw[idx]).strip()

        milking_date = _parse_date(get("milking_date"))
        cow_number = get("cow_number")
        if not milking_date or not cow_number:
            continue

        record = {
            "farm_code": farm_code,
            "cow_number": cow_number,
            "milking_date": milking_date,
            "shift": get("shift") or None,
            "dim": _parse_int(get("dim")),
            "peak_milk_flow_time": get("peak_milk_flow_time") or None,
            "group_number": get("group_number") or None,
            "unit_on_time": get("unit_on_time") or None,
            "cow_milking_start_time": get("cow_milking_start_time") or None,
            "final_detaching": get("final_detaching") or None,
            "milking_point": get("milking_point") or None,
        }
        for field in FLOAT_FIELDS:
            record[field] = _parse_float(get(field)) if field in field_indexes else None

        records.append(record)

    return records
