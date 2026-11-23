"""Apply Müller collection CSV litres to Collections weighbridge values.

One CSV row is one tanker collection. Litres is written to litres_weighbridge.
A sample number with no ticket is added as a new line. Farm-ticket litres are
left alone. A row with no sample number is matched provisionally (small loads
first, then big loads in payment-time order) within 1000 L and is not inserted.
Payment Date is the primary calendar day (Actual Collection is fallback).
"""

from __future__ import annotations

import calendar
import csv
import datetime as dt
import io
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import select

from services.database import NmlMilkResult, get_session
from services.farms import FARMS
from services.nml_pdf import FARM_PRODUCER_REF, normalize_sample_id

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = ROOT / "data" / "Muller_Milk_Collections_Monthly"
FARM_CODES = {farm.code for farm in FARMS}

_DATE_FORMATS = ("%d-%b-%y", "%d-%b-%Y")
_VOLUME_TOLERANCE_L = 1000.0
# ALH full tankers are ~28k; the last/small load is a few thousand at most.
_SMALL_LOAD_MAX_L = 10000.0


def farm_from_filename(name: str) -> str | None:
    prefix = Path(name).name.split("_", 1)[0].strip().upper()
    if prefix in FARM_CODES:
        return prefix
    return None


def _parse_date(value: str | None) -> dt.date | None:
    text = (value or "").strip()
    if not text or text == "-":
        return None
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_time(value: str | None) -> dt.time | None:
    text = (value or "").strip()
    if not text or text == "-":
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return dt.datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def _parse_litres(value: str | None) -> float | None:
    text = (value or "").strip().replace(",", "")
    if not text or text == "-":
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if number <= 0:
        return None
    return number


def _parse_temp(value: str | None) -> float | None:
    text = (value or "").strip().replace(",", "")
    if not text or text == "-":
        return None
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def _is_real_sample(row: NmlMilkResult) -> bool:
    """Skip blank tickets stored as L1 / L2 placeholders."""
    if row.sample_missing:
        return False
    sample = normalize_sample_id(row.sample_id)
    return bool(sample) and sample.isdigit()


def _primary_date(record: dict[str, Any]) -> dt.date | None:
    """Payment day first — that is the farm collection day on Collections."""
    return record.get("payment_date") or record.get("actual_date")


def _sort_datetime(record: dict[str, Any]) -> tuple[dt.date, dt.time]:
    day = _primary_date(record) or dt.date.min
    time = (
        record.get("payment_time")
        or record.get("actual_time")
        or dt.time.min
    )
    return day, time


def _is_small_litres(value: Any) -> bool:
    key = _litres_key(value)
    return key is not None and key < _SMALL_LOAD_MAX_L


def _within_tolerance(ticket_litres: Any, bridge_litres: Any) -> bool:
    left = _litres_key(ticket_litres)
    right = _litres_key(bridge_litres)
    if left is None or right is None:
        return False
    return abs(left - right) <= _VOLUME_TOLERANCE_L


def parse_muller_rows(filename: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    farm = farm_from_filename(filename)
    if farm is None:
        raise ValueError(f"Cannot read a farm code from {filename}")

    records: list[dict[str, Any]] = []
    for raw in rows:
        sample_raw = str(raw.get("Sample Number") or "").strip()
        if sample_raw in {"", "-"}:
            sample = ""
        else:
            sample = normalize_sample_id(sample_raw)
            if not sample.isdigit():
                continue
        litres = _parse_litres(raw.get("Litres"))
        if litres is None:
            continue
        records.append(
            {
                "farm": farm,
                "sample_id": sample,
                "litres": litres,
                "temp_c": _parse_temp(raw.get("Collection Temperature")),
                "actual_date": _parse_date(raw.get("Actual Collection Date")),
                "actual_time": _parse_time(raw.get("Actual Collection Time")),
                "payment_date": _parse_date(raw.get("Payment Date")),
                "payment_time": _parse_time(raw.get("Payment Time")),
            }
        )
    return records


def parse_muller_csv_text(filename: str, text: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text))
    return parse_muller_rows(filename, list(reader))


def parse_muller_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return parse_muller_rows(path.name, list(csv.DictReader(handle)))


def _date_gap(
    row_date: dt.date | None,
    actual: dt.date | None,
    payment: dt.date | None,
) -> bool:
    """True when the Collections date is more than a day from both Müller dates."""
    if row_date is None:
        return True
    for day in (payment, actual):
        if day is not None and abs((row_date - day).days) <= 1:
            return False
    return True


def _close_dates(actual: dt.date | None, payment: dt.date | None) -> set[dt.date]:
    """Payment date (preferred), actual date, and neighbouring days."""
    dates: set[dt.date] = set()
    for day in (payment, actual):
        if day is None:
            continue
        dates.add(day)
        dates.add(day - dt.timedelta(days=1))
        dates.add(day + dt.timedelta(days=1))
    return dates


def _match_row(
    candidates: list[NmlMilkResult],
    actual: dt.date | None,
    payment: dt.date | None,
) -> tuple[NmlMilkResult | None, str]:
    if not candidates:
        return None, "unmatched"

    # Prefer exact Payment Date (farm collection day).
    if payment is not None:
        on_payment = [row for row in candidates if row.sample_date == payment]
        if len(on_payment) == 1:
            return on_payment[0], "matched"
        if len(on_payment) > 1:
            return None, "ambiguous"

    close = _close_dates(actual, payment)
    dated = [row for row in candidates if row.sample_date in close]
    if len(dated) == 1:
        return dated[0], "matched"
    if len(dated) > 1:
        # Prefer payment day, then actual, among the near-dated hits.
        for preferred in (payment, actual):
            if preferred is None:
                continue
            on_exact = [row for row in dated if row.sample_date == preferred]
            if len(on_exact) == 1:
                return on_exact[0], "matched"
            if len(on_exact) > 1:
                return None, "ambiguous"
        return None, "ambiguous"
    return None, "unmatched"


def _litres_key(value: Any) -> int | None:
    """Nearest whole litre, used to compare a ticket with a Müller load."""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return int(round(number))


def _ticket_sort_key(row: NmlMilkResult) -> tuple[int, int]:
    load = row.load_number if row.load_number is not None else 10**9
    return (load, row.id or 0)


def _claim_blank_by_size(
    blanks: list[dict[str, Any]],
    tickets: list[NmlMilkResult],
    claimed: dict[int, list[tuple[dict[str, Any], NmlMilkResult, bool]]],
    unmatched: list[dict[str, Any]],
) -> None:
    """Match blank Müller rows to tickets of the same size class within 1000 L.

    Small loads are paired first (usually Load 4). Big loads then take remaining
    tickets in payment-time order → load-number order, so a small portal row
    between two big ones does not steal Load 2.
    """
    available = [
        row
        for row in tickets
        if row.id is not None and row.id not in claimed and row.litres_load is not None
    ]
    available.sort(key=_ticket_sort_key)
    blanks_sorted = sorted(blanks, key=_sort_datetime)

    for record in blanks_sorted:
        bridge_small = _is_small_litres(record["litres"])
        pool = [
            row
            for row in available
            if row.id not in claimed and _is_small_litres(row.litres_load) == bridge_small
        ]
        chosen = next(
            (row for row in pool if _within_tolerance(row.litres_load, record["litres"])),
            None,
        )
        if chosen is None or chosen.id is None:
            unmatched.append(_row_brief(record))
            continue
        claimed[chosen.id].append((record, chosen, False))


def _match_blank_records(
    blank: list[dict[str, Any]],
    rows: list[NmlMilkResult],
    claimed: dict[int, list[tuple[dict[str, Any], NmlMilkResult, bool]]],
    unmatched: list[dict[str, Any]],
) -> None:
    """Provisional match for Müller rows with no sample number."""
    by_day: dict[dt.date, list[dict[str, Any]]] = defaultdict(list)
    for record in blank:
        day = _primary_date(record)
        if day is None:
            unmatched.append(_row_brief(record))
            continue
        by_day[day].append(record)

    for day, day_blanks in sorted(by_day.items()):
        day_tickets = [
            row
            for row in rows
            if row.sample_date == day
            and row.id is not None
            and row.id not in claimed
            and row.litres_load is not None
        ]
        small_blanks = [r for r in day_blanks if _is_small_litres(r["litres"])]
        big_blanks = [r for r in day_blanks if not _is_small_litres(r["litres"])]
        # Small first so Load 4 is taken before big rows fill Loads 1–3.
        _claim_blank_by_size(small_blanks, day_tickets, claimed, unmatched)
        _claim_blank_by_size(big_blanks, day_tickets, claimed, unmatched)


def _row_brief(record: dict[str, Any], rows: list[NmlMilkResult] | None = None) -> dict[str, Any]:
    brief: dict[str, Any] = {
        "farm": record["farm"],
        "sample_id": record["sample_id"],
        "actual_date": record["actual_date"].isoformat() if record["actual_date"] else "",
        "payment_date": record["payment_date"].isoformat() if record["payment_date"] else "",
        "litres": record["litres"],
    }
    if rows:
        brief["row_dates"] = sorted(
            {row.sample_date.isoformat() for row in rows if row.sample_date}
        )
    return brief


def _placeholder_date(record: dict[str, Any]) -> dt.date | None:
    return _primary_date(record)


def _insert_weighbridge_row(
    session: Any,
    farm: str,
    record: dict[str, Any],
    existing: list[NmlMilkResult],
    *,
    source_file: str,
    fill_temp: bool,
) -> NmlMilkResult | None:
    """Add a collection that has Müller litres and a sample number, but no farm ticket yet."""
    sample_id = record.get("sample_id") or ""
    sample_date = _placeholder_date(record)
    producer_ref = FARM_PRODUCER_REF.get(farm)
    if not sample_id or sample_date is None or not producer_ref:
        return None
    taken = [
        row.load_number
        for row in existing
        if row.sample_date == sample_date and row.load_number is not None
    ]
    row = NmlMilkResult(
        farm=farm,
        producer_ref=producer_ref,
        sample_date=sample_date,
        sample_id=sample_id,
        load_number=(max(taken) if taken else 0) + 1,
        litres_weighbridge=record["litres"],
        weighbridge_confirmed=True,
        report_month=f"{calendar.month_name[sample_date.month]} {sample_date.year}",
        sample_missing=False,
        nml_matched=False,
        source="muller",
        source_file=source_file,
    )
    if fill_temp and record.get("temp_c") is not None:
        row.temp_c = record["temp_c"]
    session.add(row)
    session.flush()
    return row


def apply_muller_records(
    farm: str,
    records: list[dict[str, Any]],
    *,
    source_file: str,
    fill_temp: bool = False,
    repair_conflicts: bool = False,
    apply: bool = True,
) -> dict[str, Any]:
    """Write Müller litres onto tickets. A sample with no ticket is added as a new line."""
    updated_ids: set[int] = set()
    inserted = 0
    temp_filled = 0
    unmatched: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    date_gaps: list[dict[str, Any]] = []

    with get_session() as session:
        rows = session.scalars(
            select(NmlMilkResult).where(NmlMilkResult.farm == farm)
        ).all()
        index: dict[str, list[NmlMilkResult]] = {}
        for row in rows:
            if not _is_real_sample(row):
                continue
            index.setdefault(normalize_sample_id(row.sample_id), []).append(row)

        # (record, row, confirmed) — confirmed when Müller had a sample number.
        claimed: dict[int, list[tuple[dict[str, Any], NmlMilkResult, bool]]] = defaultdict(list)
        sampled = [record for record in records if record["sample_id"]]
        blank = [record for record in records if not record["sample_id"]]
        for record in sampled:
            candidates = index.get(record["sample_id"], [])
            chosen, status = _match_row(
                candidates, record["actual_date"], record["payment_date"]
            )
            if status == "unmatched":
                created = _insert_weighbridge_row(
                    session,
                    farm,
                    record,
                    list(rows),
                    source_file=source_file,
                    fill_temp=fill_temp,
                )
                if created is None:
                    unmatched.append(_row_brief(record))
                    continue
                rows.append(created)
                index.setdefault(record["sample_id"], []).append(created)
                inserted += 1
                continue
            if status == "ambiguous" or chosen is None:
                ambiguous.append(_row_brief(record, candidates))
                continue
            claimed[chosen.id].append((record, chosen, True))

        _match_blank_records(blank, list(rows), claimed, unmatched)

        for claims in claimed.values():
            row = claims[0][1]
            litres = {record["litres"] for record, _row, _ok in claims}
            if len(litres) > 1:
                for record, _row, _ok in claims:
                    ambiguous.append(_row_brief(record, [row]))
                if repair_conflicts and row.litres_load is None and row.litres_weighbridge in litres:
                    row.litres_weighbridge = None
                    row.weighbridge_confirmed = False
                    if fill_temp:
                        temps = {record["temp_c"] for record, _row, _ok in claims}
                        if row.temp_c in temps:
                            row.temp_c = None
                continue
            record, _chosen, confirmed = claims[-1]
            # Sample-confirmed claim wins over a provisional one on the same ticket.
            if any(ok for _rec, _row, ok in claims):
                confirmed = True
                record = next(rec for rec, _row, ok in claims if ok)
            row.litres_weighbridge = record["litres"]
            row.weighbridge_confirmed = confirmed
            updated_ids.add(row.id)
            if (
                fill_temp
                and record["sample_id"]
                and row.temp_c is None
                and record["temp_c"] is not None
            ):
                row.temp_c = record["temp_c"]
                temp_filled += 1
            if _date_gap(row.sample_date, record["actual_date"], record["payment_date"]):
                gap = _row_brief(record)
                gap["row_date"] = row.sample_date.isoformat() if row.sample_date else ""
                date_gaps.append(gap)

        if not apply:
            session.rollback()

    return {
        "source_file": source_file,
        "farm": farm,
        "rows_parsed": len(records),
        "rows_updated": len(updated_ids),
        "rows_inserted": inserted,
        "temps_filled": temp_filled,
        "rows_unmatched": len(unmatched),
        "rows_ambiguous": len(ambiguous),
        "rows_date_gap": len(date_gaps),
        "unmatched": unmatched,
        "ambiguous": ambiguous,
        "date_gaps": date_gaps,
        "applied": apply,
    }


def import_muller_collections(path: Path, *, apply: bool = True) -> dict[str, Any]:
    workbook = Path(path)
    if not workbook.is_file():
        raise FileNotFoundError(f"Müller CSV not found: {workbook}")

    farm = farm_from_filename(workbook.name)
    if farm is None:
        raise ValueError(f"Cannot read a farm code from {workbook.name}")
    records = parse_muller_csv(workbook)
    return apply_muller_records(
        farm,
        records,
        source_file=workbook.name,
        fill_temp=True,
        repair_conflicts=True,
        apply=apply,
    )


def import_muller_uploads(files: list[tuple[str, bytes]]) -> dict[str, Any]:
    """Apply uploaded Müller CSVs. Weighbridge only; ticket litres and temperature stay put."""
    if not files:
        raise ValueError("Choose at least one Müller CSV.")

    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for filename, content in files:
        name = Path(filename or "").name
        if farm_from_filename(name) is None:
            errors.append(
                {
                    "source_file": name or filename,
                    "error": "Farm code not recognised. Name the file ALH_, BNK_, COF_, or SFR_.",
                }
            )
            continue
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{name} is not a text CSV.") from exc
        records = parse_muller_csv_text(name, text)
        farm = farm_from_filename(name)
        assert farm is not None
        results.append(
            apply_muller_records(
                farm,
                records,
                source_file=name,
                fill_temp=False,
                repair_conflicts=False,
                apply=True,
            )
        )

    if not results:
        detail = errors[0]["error"] if errors else "No Müller files were imported."
        raise ValueError(detail)

    return {
        "files": [
            {
                "source_file": item["source_file"],
                "farm": item["farm"],
                "rows_parsed": item["rows_parsed"],
                "rows_updated": item["rows_updated"],
                "rows_unmatched": item["rows_unmatched"],
                "rows_ambiguous": item["rows_ambiguous"],
            }
            for item in results
        ],
        "errors": errors,
        "rows_updated": sum(item["rows_updated"] for item in results),
        "rows_unmatched": sum(item["rows_unmatched"] for item in results),
        "rows_ambiguous": sum(item["rows_ambiguous"] for item in results),
    }


def import_muller_folder(
    directory: Path | None = None,
    *,
    apply: bool = True,
) -> list[dict[str, Any]]:
    folder = Path(directory) if directory else DEFAULT_DIR
    if not folder.is_dir():
        raise FileNotFoundError(f"Müller folder not found: {folder}")

    paths = sorted(folder.glob("*_MULLER_collection_details_for_*.csv"))
    if not paths:
        logger.warning("No Müller collection CSVs in %s", folder)
    results: list[dict[str, Any]] = []
    for path in paths:
        if farm_from_filename(path.name) is None:
            results.append(
                {
                    "source_file": path.name,
                    "farm": None,
                    "error": "farm code not recognised",
                }
            )
            continue
        results.append(import_muller_collections(path, apply=apply))
    return results
