"""Parse DataFlow Rotary Entry ID Report CSV attachments."""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

COLUMN_MAP = {
    "cow number": "cow_number",
    "date": "milking_date",
    "shift": "shift",
    "identification time": "identification_time",
    "entry id time": "identification_time",
    "entry identification time": "identification_time",
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


def _normalize_header(name: str) -> str:
    return " ".join((name or "").strip().lower().split())


def parse_rotary_entry_id_csv(file_path: str | Path, farm_code: str = "ALH") -> list[dict]:
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

    required = {"cow_number", "milking_date", "identification_time"}
    if not required.issubset(field_indexes):
        raise RuntimeError(
            f"Rotary Entry ID CSV missing required columns. Found headers: {rows[0]!r}"
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
        identification_time = get("identification_time")
        if not milking_date or not cow_number or not identification_time:
            continue

        records.append(
            {
                "farm_code": farm_code,
                "cow_number": cow_number,
                "milking_date": milking_date,
                "shift": get("shift") or None,
                "identification_time": identification_time,
            }
        )

    return records
