"""Manual milk-ticket entry for Collections (one row per load)."""

from __future__ import annotations

import calendar
import datetime as dt
import json
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from services.database import MilkMeterReading, NmlMilkResult, get_session
from services.farms import HERD_FARM_OPTIONS
from services.muller_collections_import import clear_unfit_weighbridges
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


def _parse_meter(raw: Any, label: str) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number.") from exc
    if value < 0:
        raise ValueError(f"{label} must be zero or greater.")
    return round(value, 1)


def _meter_out(value: float | None) -> float | int | None:
    if value is None:
        return None
    number = float(value)
    if number.is_integer():
        return int(number)
    return round(number, 1)


def _meters_payload(row: MilkMeterReading | None) -> dict[str, float | int | None]:
    if row is None:
        return {"morning": None, "afternoon": None, "evening": None}
    return {
        "morning": _meter_out(row.morning_reading),
        "afternoon": _meter_out(row.afternoon_reading),
        "evening": _meter_out(row.evening_reading),
    }


def get_collection_day(*, farm: str, sample_date: dt.date) -> dict[str, Any]:
    farm_key = _farm_key(farm)
    with get_session() as session:
        rows = session.scalars(
            select(NmlMilkResult)
            .where(NmlMilkResult.farm == farm_key, NmlMilkResult.sample_date == sample_date)
            .order_by(NmlMilkResult.load_number.asc(), NmlMilkResult.id.asc())
        ).all()
        meters = _meters_payload(
            session.scalar(
                select(MilkMeterReading).where(
                    MilkMeterReading.farm == farm_key,
                    MilkMeterReading.sample_date == sample_date,
                )
            )
        )
    loads = [
        {
            "load_number": row.load_number,
            "volume_litres": row.litres_load,
            "temp_c": None if row.temp_c is None else round(float(row.temp_c), 2),
            "sample_id": "" if row.sample_missing else _visible_sample(row),
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
        "meters": meters,
    }


def _save_meter_readings(
    session: Any,
    farm: str,
    sample_date: dt.date,
    meters: dict[str, Any] | None,
) -> None:
    """ALH only. Readings are for the day's milkings, not for each tanker load."""
    if farm != "ALH" or not isinstance(meters, dict):
        return
    morning = _parse_meter(meters.get("morning"), "Morning milking meter")
    afternoon = _parse_meter(meters.get("afternoon"), "Afternoon milking meter")
    evening = _parse_meter(meters.get("evening"), "Evening milking meter")
    row = session.scalar(
        select(MilkMeterReading).where(
            MilkMeterReading.farm == farm,
            MilkMeterReading.sample_date == sample_date,
        )
    )
    if row is None:
        if morning is None and afternoon is None and evening is None:
            return
        session.add(
            MilkMeterReading(
                farm=farm,
                sample_date=sample_date,
                morning_reading=morning,
                afternoon_reading=afternoon,
                evening_reading=evening,
                updated_at=dt.datetime.now(dt.timezone.utc),
            )
        )
        return
    row.morning_reading = morning
    row.afternoon_reading = afternoon
    row.evening_reading = evening
    row.updated_at = dt.datetime.now(dt.timezone.utc)


def save_collection_day(
    *,
    farm: str,
    sample_date: dt.date,
    loads: list[dict[str, Any]],
    meters: dict[str, Any] | None = None,
    allow_duplicate: bool = False,
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
            key = normalize_sample_id(sample)
            if key in seen_samples and not allow_duplicate:
                raise ValueError(f"Sample {sample} has already been used on that day.")
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
    merged = 0

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
            placeholder = _awaiting_ticket(session, farm_key, sample_date, item["sample_id"])
            key = (producer_ref, sample_date, sample_id)
            shown_sample = ""
            repeats = placeholder is None and key in used_ids and (
                by_load.get(item["load_number"]) is None
                or by_load[item["load_number"]].sample_id != sample_id
            )
            if repeats and not allow_duplicate:
                raise ValueError(
                    f"Sample {item['sample_id']} has already been used on that day."
                )
            if repeats:
                shown_sample = item["sample_id"]
                sample_id = f"{item['sample_id']}#{item['load_number']}"
                sample_missing = False
            used_ids.add((producer_ref, sample_date, sample_id))

            row = placeholder or by_load.get(item["load_number"])
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
                row.shown_sample_id = shown_sample or None
                row.sample_missing = sample_missing
                row.litres_load = item["litres_load"]
                clear_unfit_weighbridges([row])
                row.temp_c = item["temp_c"]
                row.source = MANUAL_SOURCE
                row.source_file = MANUAL_SOURCE
                row.imported_at = dt.datetime.now(dt.timezone.utc)
                continue

            row.litres_load = item["litres_load"]
            clear_unfit_weighbridges([row])
            row.temp_c = item["temp_c"]
            if item["sample_id"]:
                row.sample_id = sample_id
                row.shown_sample_id = shown_sample or None
                row.sample_missing = False
            row.imported_at = dt.datetime.now(dt.timezone.utc)
            updated += 1

        _save_meter_readings(session, farm_key, sample_date, meters)
        session.flush()
        _copy_shared_sample_quality(session, farm_key, sample_date)
        from services.nml_import import _merge_orphan_nml, _row_index

        merged = _merge_orphan_nml(
            session, _row_index(list(session.scalars(select(NmlMilkResult)).all()))
        )
        merged += join_lab_and_weighbridge_rows(session)
        saved_meters = _meters_payload(
            session.scalar(
                select(MilkMeterReading).where(
                    MilkMeterReading.farm == farm_key,
                    MilkMeterReading.sample_date == sample_date,
                )
            )
        )

    return {
        "farm": farm_key,
        "sample_date": sample_date.isoformat(),
        "loads_saved": len(cleaned),
        "rows_inserted": inserted,
        "rows_updated": updated,
        "orphans_merged": merged,
        "meters": saved_meters,
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


def _awaiting_ticket(session: Any, farm: str, sample_date: dt.date, sample_id: str) -> NmlMilkResult | None:
    """A Müller line with this sample number, still waiting for the farm litres."""
    target = normalize_sample_id(sample_id)
    if not target or not target.isdigit():
        return None
    hits: list[NmlMilkResult] = []
    rows = session.scalars(select(NmlMilkResult).where(NmlMilkResult.farm == farm)).all()
    for row in rows:
        if normalize_sample_id(row.sample_id) != target:
            continue
        if row.sample_date is None or abs((row.sample_date - sample_date).days) > 1:
            continue
        if row.litres_load is not None and float(row.litres_load) > 0:
            continue
        if row.litres_weighbridge is None:
            continue
        hits.append(row)
    exact = [row for row in hits if row.sample_date == sample_date]
    chosen = exact or hits
    if len(chosen) != 1:
        return None
    row = chosen[0]
    if row.sample_date != sample_date:
        taken = any(
            other is not row
            and other.producer_ref == row.producer_ref
            and other.sample_date == sample_date
            and normalize_sample_id(other.sample_id) == target
            for other in rows
        )
        if taken:
            return None
        row.sample_date = sample_date
        row.report_month = f"{calendar.month_name[sample_date.month]} {sample_date.year}"
    return row


def _has_volume(row: NmlMilkResult) -> bool:
    return row.litres_load is not None and float(row.litres_load) > 0


def _has_quality(row: NmlMilkResult) -> bool:
    return any(getattr(row, field) is not None for field in _QUALITY_FIELDS)

def update_collection_load(
    row_id: int,
    *,
    sample_date: dt.date,
    litres_load: Any,
    temp_c: Any,
    sample_id: Any,
    load_number: Any = None,
    allow_duplicate: bool = False,
) -> dict[str, Any]:
    volume = _parse_volume(litres_load)
    if volume is None:
        raise ValueError("Enter a volume greater than zero.")
    temp = _parse_temp(temp_c)
    typed_sample = str(sample_id or "").strip()
    sample_missing = not bool(typed_sample)
    new_load: int | None
    if load_number is None or load_number == "":
        new_load = None  # leave unchanged — resolved below from row
        load_provided = False
    else:
        load_provided = True
        try:
            new_load = int(load_number)
        except (TypeError, ValueError) as exc:
            raise ValueError("Load must be a whole number.") from exc
        if new_load < 1:
            raise ValueError("Load must be 1 or higher.")

    with get_session() as session:
        row = session.get(NmlMilkResult, row_id)
        if row is None:
            raise ValueError("Collection not found.")

        if not load_provided:
            new_load = row.load_number

        stored_sample = typed_sample or (row.sample_id if row.sample_missing else "") or (
            f"L{new_load}" if new_load else f"L{row.id}"
        )
        old_date = row.sample_date

        clashes = [
            other
            for other in session.scalars(
                select(NmlMilkResult).where(
                    NmlMilkResult.id != row.id,
                    NmlMilkResult.producer_ref == row.producer_ref,
                    NmlMilkResult.sample_date == sample_date,
                )
            ).all()
            if _same_visible_sample(other, stored_sample)
        ]
        volume_clash = next((other for other in clashes if _has_volume(other)), None)
        if volume_clash is not None and not allow_duplicate:
            raise ValueError(f"Sample {typed_sample} has already been used on that day.")
        orphan_clash = next((other for other in clashes if not _has_volume(other)), None)

        same_day = list(
            session.scalars(
                select(NmlMilkResult).where(
                    NmlMilkResult.farm == row.farm,
                    NmlMilkResult.sample_date == sample_date,
                    NmlMilkResult.id != row.id,
                )
            ).all()
        )
        taken_loads = {
            other.load_number for other in same_day if other.load_number is not None
        }
        if new_load is not None and new_load in taken_loads:
            raise ValueError(f"Load {new_load} is already used on that date.")

        if old_date != sample_date:
            if new_load is None or new_load in taken_loads:
                new_load = (max(taken_loads) if taken_loads else 0) + 1
            row.report_month = f"{calendar.month_name[sample_date.month]} {sample_date.year}"

        row.sample_date = sample_date
        row.load_number = new_load
        row.litres_load = volume
        clear_unfit_weighbridges([row])
        row.temp_c = temp
        if volume_clash is not None:
            _keep_repeated_sample(row, typed_sample)
            _copy_quality(volume_clash, row)
        elif orphan_clash is not None:
            _fold_orphan_into_load(row, orphan_clash)
            session.delete(orphan_clash)
        else:
            row.sample_id = stored_sample
            row.shown_sample_id = None
            row.sample_missing = sample_missing
        row.imported_at = dt.datetime.now(dt.timezone.utc)
        session.flush()

        # Re-link from NML rows already in the DB (no email reimport needed).
        from services.nml_import import _merge_orphan_nml, _row_index

        merged = _merge_orphan_nml(
            session, _row_index(list(session.scalars(select(NmlMilkResult)).all()))
        )
        merged += join_lab_and_weighbridge_rows(session)
        session.refresh(row)

        if _has_volume(row) and _has_quality(row) and not sample_missing:
            row.nml_matched = True
        elif _has_volume(row) and not _has_quality(row):
            row.nml_matched = False

        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise ValueError(
                f"Sample {typed_sample} has already been used on that day."
            ) from exc

        return {
            "id": row.id,
            "farm": row.farm,
            "sample_date": row.sample_date.isoformat() if row.sample_date else "",
            "load_number": row.load_number,
            "litres_load": row.litres_load,
            "temp_c": None if row.temp_c is None else round(float(row.temp_c), 2),
            "sample_id": "" if row.sample_missing else _visible_sample(row),
            "nml_matched": bool(row.nml_matched),
            "orphans_merged": merged,
        }


def delete_collection_load(row_id: int) -> dict[str, Any]:
    with get_session() as session:
        row = session.get(NmlMilkResult, row_id)
        if row is None:
            raise ValueError("Collection not found.")
        summary = {
            "id": row.id,
            "farm": row.farm,
            "sample_date": row.sample_date.isoformat() if row.sample_date else "",
        }
        session.delete(row)
        session.commit()
        return summary


def _visible_sample(row: NmlMilkResult) -> str:
    if row.sample_missing:
        return ""
    shown = str(getattr(row, "shown_sample_id", None) or "").strip()
    if shown:
        return shown
    return str(row.sample_id or "").strip()


def _same_visible_sample(row: NmlMilkResult, sample: str) -> bool:
    target = normalize_sample_id(sample)
    if not target:
        return False
    return normalize_sample_id(_visible_sample(row)) == target


def _copy_quality(source: NmlMilkResult, target: NmlMilkResult) -> None:
    copied = False
    for field in _QUALITY_FIELDS:
        value = getattr(source, field)
        if value is not None and getattr(target, field) is None:
            setattr(target, field, value)
            copied = True
    if copied and _has_volume(target) and not target.sample_missing:
        target.nml_matched = True
        target.quality_estimated = False


def _keep_repeated_sample(row: NmlMilkResult, sample: str) -> None:
    """Store a second load under the same visible sample without breaking the unique key."""
    row.shown_sample_id = sample
    row.sample_missing = False
    if normalize_sample_id(row.sample_id) == normalize_sample_id(sample):
        row.sample_id = f"{sample}#{row.id or row.load_number or 0}"


def _fold_orphan_into_load(load: NmlMilkResult, orphan: NmlMilkResult) -> None:
    """A lab or weighbridge row with no litres joins the load that uses its sample."""
    _copy_quality(orphan, load)
    if load.litres_weighbridge is None and orphan.litres_weighbridge is not None:
        load.litres_weighbridge = orphan.litres_weighbridge
        load.weighbridge_confirmed = bool(orphan.weighbridge_confirmed)
    load.sample_id = _visible_sample(orphan) or load.sample_id
    load.shown_sample_id = None
    load.sample_missing = False


def _copy_shared_sample_quality(session: Any, farm: str, sample_date: dt.date) -> None:
    """A repeated sample shows the same lab result as the first load that has it."""
    rows = list(
        session.scalars(
            select(NmlMilkResult).where(
                NmlMilkResult.farm == farm,
                NmlMilkResult.sample_date == sample_date,
            )
        ).all()
    )
    donors: dict[str, NmlMilkResult] = {}
    for row in rows:
        sample = normalize_sample_id(_visible_sample(row))
        if sample and _has_quality(row) and sample not in donors:
            donors[sample] = row
    for row in rows:
        sample = normalize_sample_id(_visible_sample(row))
        donor = donors.get(sample)
        if donor is not None and donor is not row:
            _copy_quality(donor, row)


def join_lab_and_weighbridge_rows(session: Any) -> int:
    """Put a weighbridge on the lab row that has its sample and no litres, or the reverse.

    A different sample with no litres is left on its own row. A load that already
    has another sample is left alone.
    """
    rows = list(session.scalars(select(NmlMilkResult)).all())
    groups: dict[tuple[str, str], list[NmlMilkResult]] = {}
    for row in rows:
        if getattr(row, "match_parts", None):
            continue
        if _has_volume(row) or row.sample_missing:
            continue
        sample = normalize_sample_id(_visible_sample(row))
        if not sample or row.sample_date is None or not row.farm:
            continue
        groups.setdefault((row.farm, sample), []).append(row)

    joined = 0
    for grouped in groups.values():
        labs = [row for row in grouped if _has_quality(row) and row.litres_weighbridge is None]
        bridges = [
            row
            for row in grouped
            if row.litres_weighbridge is not None and not _has_quality(row)
        ]
        if len(labs) != 1 or len(bridges) != 1:
            continue
        lab, bridge = labs[0], bridges[0]
        if lab.sample_date is None or bridge.sample_date is None:
            continue
        if abs((lab.sample_date - bridge.sample_date).days) > 1:
            continue
        lab.litres_weighbridge = bridge.litres_weighbridge
        lab.weighbridge_confirmed = bool(bridge.weighbridge_confirmed)
        if lab.temp_c is None and bridge.temp_c is not None:
            lab.temp_c = bridge.temp_c
        session.delete(bridge)
        joined += 1
    if joined:
        session.flush()
    _copy_repeated_sample_quality(session)
    return joined


def _copy_repeated_sample_quality(session: Any) -> None:
    """Every load showing a sample gets the lab result already stored for that sample."""
    rows = list(session.scalars(select(NmlMilkResult)).all())
    donors: dict[tuple[str, dt.date, str], NmlMilkResult] = {}
    for row in rows:
        sample = normalize_sample_id(_visible_sample(row))
        if not sample or row.sample_date is None or not _has_quality(row):
            continue
        key = (row.farm or "", row.sample_date, sample)
        if key not in donors:
            donors[key] = row
    for row in rows:
        sample = normalize_sample_id(_visible_sample(row))
        if not sample or row.sample_date is None:
            continue
        donor = donors.get((row.farm or "", row.sample_date, sample))
        if donor is not None and donor is not row:
            _copy_quality(donor, row)


_SNAPSHOT_FIELDS = (
    "farm",
    "producer_ref",
    "milk_buyer",
    "report_month",
    "report_date",
    "sample_date",
    "sample_id",
    "shown_sample_id",
    "load_number",
    "litres_load",
    "litres_weighbridge",
    "weighbridge_confirmed",
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
    "quality_estimated",
    "source",
    "source_message_id",
    "source_file",
)


def _snapshot_row(row: NmlMilkResult) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field in _SNAPSHOT_FIELDS:
        value = getattr(row, field, None)
        if isinstance(value, dt.datetime):
            value = value.isoformat()
        elif isinstance(value, dt.date):
            value = value.isoformat()
        payload[field] = value
    return payload


def _parts_of(row: NmlMilkResult) -> list[dict[str, Any]]:
    raw = getattr(row, "match_parts", None)
    if raw:
        try:
            saved = json.loads(raw)
        except (TypeError, ValueError):
            saved = None
        if isinstance(saved, dict) and isinstance(saved.get("rows"), list):
            return list(saved["rows"])
    return [_snapshot_row(row)]


def _restore_row(session: Any, payload: dict[str, Any]) -> NmlMilkResult:
    values: dict[str, Any] = {}
    for field in _SNAPSHOT_FIELDS:
        value = payload.get(field)
        if field in {"sample_date", "report_date"} and isinstance(value, str) and value:
            value = dt.date.fromisoformat(value)
        values[field] = value
    row = NmlMilkResult(**values)
    row.match_parts = None
    session.add(row)
    return row


def match_collection_rows(row_ids: list[int]) -> dict[str, Any]:
    """Join the selected rows into one. The sample kept is the lab, then the weighbridge."""
    ids = [int(row_id) for row_id in row_ids]
    if len(ids) < 2 or len(set(ids)) != len(ids):
        raise ValueError("Select at least two rows to match.")
    with get_session() as session:
        rows = list(
            session.scalars(select(NmlMilkResult).where(NmlMilkResult.id.in_(ids))).all()
        )
        if len(rows) != len(ids):
            raise ValueError("A selected collection could not be found.")
        if len({row.farm for row in rows}) != 1:
            raise ValueError("Match rows from the same farm.")
        loads = [row for row in rows if _has_volume(row)]
        if len(loads) > 1:
            raise ValueError(
                "Select one load, plus the lab or weighbridge row to join to it."
            )
        labs = [row for row in rows if _has_quality(row) and not _has_volume(row)]
        bridges = [
            row
            for row in rows
            if row.litres_weighbridge is not None and not _has_volume(row) and row not in labs
        ]
        lab_sample = next((_visible_sample(row) for row in labs if _visible_sample(row)), "")
        bridge_sample = next(
            (_visible_sample(row) for row in bridges if _visible_sample(row)),
            "",
        )
        kept = lab_sample or bridge_sample
        if not kept:
            kept = next((_visible_sample(row) for row in rows if _visible_sample(row)), "")
        survivor = loads[0] if loads else (labs[0] if labs else rows[0])
        parts: list[dict[str, Any]] = []
        for row in rows:
            parts.extend(_parts_of(row))
        for row in rows:
            if row is survivor:
                continue
            if _has_quality(row):
                for field in _QUALITY_FIELDS:
                    value = getattr(row, field)
                    if value is not None:
                        setattr(survivor, field, value)
                survivor.quality_estimated = False
            if survivor.litres_weighbridge is None and row.litres_weighbridge is not None:
                survivor.litres_weighbridge = row.litres_weighbridge
                survivor.weighbridge_confirmed = bool(row.weighbridge_confirmed)
            if survivor.temp_c is None and row.temp_c is not None:
                survivor.temp_c = row.temp_c
            session.delete(row)
        session.flush()
        if kept:
            clash = session.scalars(
                select(NmlMilkResult).where(
                    NmlMilkResult.id != survivor.id,
                    NmlMilkResult.producer_ref == survivor.producer_ref,
                    NmlMilkResult.sample_date == survivor.sample_date,
                    NmlMilkResult.sample_id == kept,
                )
            ).first()
            if clash is None:
                survivor.sample_id = kept
                survivor.shown_sample_id = None
            else:
                _keep_repeated_sample(survivor, kept)
            survivor.sample_missing = False
        if _has_volume(survivor) and _has_quality(survivor):
            survivor.nml_matched = True
        if (
            survivor.litres_load is not None
            and survivor.litres_weighbridge is not None
            and any(row.litres_weighbridge is not None for row in rows)
        ):
            survivor.weighbridge_confirmed = True
        survivor.match_parts = json.dumps({"rows": parts})
        session.flush()
        return {"id": survivor.id, "sample_id": _visible_sample(survivor), "rows_matched": len(ids)}


def unmatch_collection_rows(row_ids: list[int]) -> dict[str, Any]:
    """Split a matched row back into the load, the lab row, and the weighbridge row."""
    ids = [int(row_id) for row_id in row_ids]
    if not ids:
        raise ValueError("Select a matched row to separate.")
    restored = 0
    with get_session() as session:
        rows = list(
            session.scalars(select(NmlMilkResult).where(NmlMilkResult.id.in_(ids))).all()
        )
        if len(rows) != len(set(ids)):
            raise ValueError("A selected collection could not be found.")
        for row in rows:
            parts = _parts_of(row) if getattr(row, "match_parts", None) else []
            if len(parts) < 2:
                raise ValueError("That row is not a matched collection.")
            session.delete(row)
            session.flush()
            for part in parts:
                _restore_row(session, part)
                restored += 1
        try:
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            raise ValueError(
                "Could not unmatch because one of those sample numbers is already used."
            ) from exc
    return {"rows_restored": restored}
