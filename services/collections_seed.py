"""Snapshot local Collections rows so production Postgres can load them on boot."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Any

from sqlalchemy import select

from services.database import NmlMilkResult, get_session

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
SEED_PATH = ROOT / "seeds" / "nml_collections.json"

_FIELDS = (
    "farm",
    "producer_ref",
    "milk_buyer",
    "report_month",
    "report_date",
    "sample_date",
    "sample_id",
    "load_number",
    "litres_load",
    "litres_weighbridge",
    "temp_c",
    "butterfat_pct",
    "protein_pct",
    "scc",
    "bactoscan",
    "fpd",
    "antibiotic_pass",
    "urea_pct",
    "sample_missing",
    "nml_matched",
    "source",
    "source_message_id",
    "source_file",
    "imported_at",
)


def _json_value(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


def _parse_date(value: Any) -> dt.date | None:
    if not value:
        return None
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def _parse_datetime(value: Any) -> dt.datetime | None:
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def export_collections_seed(path: Path | None = None) -> Path:
    out = path or SEED_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    with get_session() as session:
        rows = session.scalars(
            select(NmlMilkResult).order_by(
                NmlMilkResult.sample_date.asc(),
                NmlMilkResult.farm.asc(),
                NmlMilkResult.load_number.asc(),
            )
        ).all()
        payload = [{field: _json_value(getattr(row, field)) for field in _FIELDS} for row in rows]
    out.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    logger.info("Wrote %s collection rows to %s", len(payload), out)
    return out


def load_collections_seed(path: Path | None = None) -> dict[str, int]:
    seed = path or SEED_PATH
    if not seed.is_file():
        return {"inserted": 0, "updated": 0, "skipped": 1}

    records = json.loads(seed.read_text(encoding="utf-8"))
    inserted = 0
    updated = 0
    with get_session() as session:
        existing = session.scalars(select(NmlMilkResult)).all()
        by_key = {
            (row.producer_ref, row.sample_date, row.sample_id): row for row in existing
        }
        for raw in records:
            sample_date = _parse_date(raw.get("sample_date"))
            sample_id = str(raw.get("sample_id") or "").strip()
            producer_ref = str(raw.get("producer_ref") or "").strip()
            if not sample_date or not sample_id or not producer_ref:
                continue
            values = {
                "farm": raw.get("farm"),
                "producer_ref": producer_ref,
                "milk_buyer": raw.get("milk_buyer"),
                "report_month": raw.get("report_month"),
                "report_date": _parse_date(raw.get("report_date")),
                "sample_date": sample_date,
                "sample_id": sample_id,
                "load_number": raw.get("load_number"),
                "litres_load": raw.get("litres_load"),
                "litres_weighbridge": raw.get("litres_weighbridge"),
                "temp_c": raw.get("temp_c"),
                "butterfat_pct": raw.get("butterfat_pct"),
                "protein_pct": raw.get("protein_pct"),
                "scc": raw.get("scc"),
                "bactoscan": raw.get("bactoscan"),
                "fpd": raw.get("fpd"),
                "antibiotic_pass": raw.get("antibiotic_pass"),
                "urea_pct": raw.get("urea_pct"),
                "sample_missing": bool(raw.get("sample_missing")),
                "nml_matched": bool(raw.get("nml_matched")),
                "source": raw.get("source"),
                "source_message_id": raw.get("source_message_id"),
                "source_file": raw.get("source_file"),
                "imported_at": _parse_datetime(raw.get("imported_at")),
            }
            key = (producer_ref, sample_date, sample_id)
            row = by_key.get(key)
            if row is None:
                row = NmlMilkResult(**values)
                session.add(row)
                by_key[key] = row
                inserted += 1
                continue
            for field, value in values.items():
                setattr(row, field, value)
            updated += 1
        session.commit()
    logger.info("Collections seed: %s inserted, %s updated from %s", inserted, updated, seed.name)
    return {"inserted": inserted, "updated": updated, "skipped": 0}


def should_load_collections_seed() -> bool:
    url = (os.environ.get("DATABASE_URL") or "").lower()
    return "postgres" in url
