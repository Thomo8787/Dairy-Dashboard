"""Manual milk-ticket entry for Collections (one row per load)."""

from __future__ import annotations

import calendar
import datetime as dt
from typing import Any

from sqlalchemy import delete, select

from services.database import NmlMilkResult, get_session
from services.farms import HERD_FARM_OPTIONS
from services.nml_pdf import FARM_PRODUCER_REF

MANUAL_SOURCE = "manual"
MAX_LOADS = 5


def _farm_key(farm: str | None) -> str:
    key = (farm or "").strip().upper()
    if key not in HERD_FARM_OPTIONS:
        raise ValueError("Choose a farm.")
    return key


def _producer_ref(farm: str) -> str:
    ref = FARM_PRODUCER_REF.get(farm)
    if not ref:
        raise ValueError(f"No producer reference mapped for {farm}.")
    return ref


def _parse_volume(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("Load volume must be a number.") from exc
    if value < 0:
        raise ValueError("Load volume must be greater than zero.")
    if value == 0:
        return None
    return value


def _parse_temp(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return round(float(raw), 2)
    except (TypeError, ValueError) as exc:
        raise ValueError("Temperature must be a number.") from exc


def get_collection_day(*, farm: str, sample_date: dt.date) -> dict[str, Any]:
    farm_key = _farm_key(farm)
    with get_session() as session:
        rows = session.scalars(
            select(NmlMilkResult)
            .where(NmlMilkResult.farm == farm_key, NmlMilkResult.sample_date == sample_date)
            .order_by(NmlMilkResult.load_number.asc(), NmlMilkResult.id.asc())
        ).all()
    loads = [
        {
            "load_number": row.load_number,
            "volume_litres": row.litres_load,
            "temp_c": None if row.temp_c is None else round(float(row.temp_c), 2),
            "sample_id": "" if row.sample_missing else (row.sample_id or ""),
            "source": row.source or "",
        }
        for row in rows
        if row.litres_load is not None and row.litres_load > 0
    ]
    while len(loads) < MAX_LOADS:
        loads.append(
            {"load_number": len(loads) + 1, "volume_litres": None, "temp_c": None, "sample_id": ""}
        )
    return {
        "farm": farm_key,
        "sample_date": sample_date.isoformat(),
        "loads": loads[:MAX_LOADS],
        "has_existing": any(
            row.litres_load is not None and row.litres_load > 0 for row in rows
        ),
    }


def save_collection_day(
    *,
    farm: str,
    sample_date: dt.date,
    loads: list[dict[str, Any]],
) -> dict[str, Any]:
    farm_key = _farm_key(farm)
    producer_ref = _producer_ref(farm_key)
    cleaned: list[dict[str, Any]] = []
    seen_samples: set[str] = set()

    for index, raw in enumerate((loads or [])[:MAX_LOADS], start=1):
        volume = _parse_volume(raw.get("volume_litres"))
        if volume is None:
            continue
        sample = str(raw.get("sample_id") or "").strip()
        if sample:
            key = sample.lstrip("0") or "0"
            if key in seen_samples:
                raise ValueError(f"Duplicate sample number: {sample}")
            seen_samples.add(key)
        cleaned.append(
            {
                "load_number": index,
                "litres_load": volume,
                "temp_c": _parse_temp(raw.get("temp_c")),
                "sample_id": sample,
            }
        )

    if not cleaned:
        raise ValueError("Enter at least one load with a volume greater than zero.")

    report_month = f"{calendar.month_name[sample_date.month]} {sample_date.year}"
    inserted = 0
    updated = 0

    with get_session() as session:
        existing = session.scalars(
            select(NmlMilkResult).where(
                NmlMilkResult.farm == farm_key,
                NmlMilkResult.sample_date == sample_date,
            )
        ).all()
        by_load = {
            row.load_number: row
            for row in existing
            if row.load_number is not None
        }

        session.execute(
            delete(NmlMilkResult).where(
                NmlMilkResult.farm == farm_key,
                NmlMilkResult.sample_date == sample_date,
                NmlMilkResult.source == MANUAL_SOURCE,
                NmlMilkResult.load_number.notin_([item["load_number"] for item in cleaned]),
            )
        )

        used_ids = {
            (row.producer_ref, row.sample_date, row.sample_id)
            for row in existing
            if row.source != MANUAL_SOURCE
        }

        for item in cleaned:
            sample_missing = not bool(item["sample_id"])
            sample_id = item["sample_id"] or f"L{item['load_number']}"
            key = (producer_ref, sample_date, sample_id)
            if key in used_ids and (
                by_load.get(item["load_number"]) is None
                or by_load[item["load_number"]].sample_id != sample_id
            ):
                sample_id = f"{sample_id}-L{item['load_number']}"
                sample_missing = True
            used_ids.add((producer_ref, sample_date, sample_id))

            row = by_load.get(item["load_number"])
            if row is None or row.source == MANUAL_SOURCE:
                if row is None:
                    row = NmlMilkResult(
                        farm=farm_key,
                        producer_ref=producer_ref,
                        sample_date=sample_date,
                        sample_id=sample_id,
                        load_number=item["load_number"],
                        report_month=report_month,
                        source=MANUAL_SOURCE,
                        source_file=MANUAL_SOURCE,
                    )
                    session.add(row)
                    inserted += 1
                    by_load[item["load_number"]] = row
                else:
                    updated += 1
                row.sample_id = sample_id
                row.sample_missing = sample_missing
                row.litres_load = item["litres_load"]
                row.temp_c = item["temp_c"]
                row.source = MANUAL_SOURCE
                row.source_file = MANUAL_SOURCE
                row.imported_at = dt.datetime.now(dt.timezone.utc)
                continue

            row.litres_load = item["litres_load"]
            row.temp_c = item["temp_c"]
            if item["sample_id"]:
                row.sample_id = sample_id
                row.sample_missing = False
            row.imported_at = dt.datetime.now(dt.timezone.utc)
            updated += 1

        session.commit()

    return {
        "farm": farm_key,
        "sample_date": sample_date.isoformat(),
        "loads_saved": len(cleaned),
        "rows_inserted": inserted,
        "rows_updated": updated,
    }
