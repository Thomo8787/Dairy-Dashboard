"""Manual milk-ticket entry for Collections (one row per load)."""

from __future__ import annotations

import calendar
import datetime as dt
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from services.database import NmlMilkResult, get_session
from services.farms import HERD_FARM_OPTIONS
from services.nml_pdf import FARM_PRODUCER_REF, normalize_sample_id

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


_QUALITY_FIELDS = (
    "butterfat_pct",
    "protein_pct",
    "scc",
    "bactoscan",
    "fpd",
    "antibiotic_pass",
    "urea_pct",
)
_DATE_WINDOW_DAYS = 1


def _has_volume(row: NmlMilkResult) -> bool:
    return row.litres_load is not None and float(row.litres_load) > 0


def _copy_quality(target: NmlMilkResult, source: NmlMilkResult) -> None:
    for field in _QUALITY_FIELDS:
        value = getattr(source, field)
        if value is not None:
            setattr(target, field, value)
    target.nml_matched = True


def update_collection_load(
    row_id: int,
    *,
    sample_date: dt.date,
    litres_load: Any,
    temp_c: Any,
    sample_id: Any,
) -> dict[str, Any]:
    volume = _parse_volume(litres_load)
    if volume is None:
        raise ValueError("Enter a volume greater than zero.")
    temp = _parse_temp(temp_c)
    typed_sample = str(sample_id or "").strip()
    sample_missing = not bool(typed_sample)

    with get_session() as session:
        row = session.get(NmlMilkResult, row_id)
        if row is None:
            raise ValueError("Collection not found.")

        stored_sample = typed_sample or (row.sample_id if row.sample_missing else "") or (
            f"L{row.load_number}" if row.load_number else f"L{row.id}"
        )
        old_date = row.sample_date
        old_sample = row.sample_id
        sample_or_date_changed = (
            old_date != sample_date
            or normalize_sample_id(old_sample) != normalize_sample_id(stored_sample)
        )

        clash = session.scalars(
            select(NmlMilkResult).where(
                NmlMilkResult.id != row.id,
                NmlMilkResult.producer_ref == row.producer_ref,
                NmlMilkResult.sample_date == sample_date,
                NmlMilkResult.sample_id == stored_sample,
            )
        ).first()
        if clash is not None and _has_volume(clash):
            raise ValueError("Another load already uses that sample number on that date.")

        rematched = False
        if typed_sample:
            orphans = session.scalars(
                select(NmlMilkResult).where(
                    NmlMilkResult.id != row.id,
                    NmlMilkResult.producer_ref == row.producer_ref,
                )
            ).all()
            for orphan in orphans:
                if _has_volume(orphan):
                    continue
                if normalize_sample_id(orphan.sample_id) != normalize_sample_id(typed_sample):
                    continue
                if orphan.sample_date is None:
                    continue
                if abs((orphan.sample_date - sample_date).days) > _DATE_WINDOW_DAYS:
                    continue
                _copy_quality(row, orphan)
                session.delete(orphan)
                rematched = True
                break
            if rematched:
                session.flush()

        if old_date != sample_date:
            taken = {
                other.load_number
                for other in session.scalars(
                    select(NmlMilkResult).where(
                        NmlMilkResult.farm == row.farm,
                        NmlMilkResult.sample_date == sample_date,
                        NmlMilkResult.id != row.id,
                    )
                ).all()
                if other.load_number is not None
            }
            if row.load_number in taken:
                row.load_number = (max(taken) if taken else 0) + 1
            row.report_month = f"{calendar.month_name[sample_date.month]} {sample_date.year}"

        row.sample_date = sample_date
        row.litres_load = volume
        row.temp_c = temp
        row.sample_id = stored_sample
        row.sample_missing = sample_missing
        row.imported_at = dt.datetime.now(dt.timezone.utc)

        if sample_or_date_changed and not rematched:
            if sample_missing:
                row.nml_matched = False
            elif old_date is not None and abs((old_date - sample_date).days) <= _DATE_WINDOW_DAYS:
                if normalize_sample_id(old_sample) == normalize_sample_id(stored_sample):
                    pass
                else:
                    row.nml_matched = False
            else:
                row.nml_matched = False

        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise ValueError("Another load already uses that sample number on that date.") from exc

        return {
            "id": row.id,
            "farm": row.farm,
            "sample_date": row.sample_date.isoformat() if row.sample_date else "",
            "load_number": row.load_number,
            "litres_load": row.litres_load,
            "temp_c": None if row.temp_c is None else round(float(row.temp_c), 2),
            "sample_id": "" if row.sample_missing else (row.sample_id or ""),
            "nml_matched": bool(row.nml_matched),
        }
