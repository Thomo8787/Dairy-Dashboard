"""Apply Müller collection CSV litres to existing Collections weighbridge values.

One CSV row is one tanker collection. Litres is written to litres_weighbridge on
an existing NmlMilkResult. Farm-ticket litres are left alone. Unmatched rows are
not inserted.
"""

from __future__ import annotations

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
from services.nml_pdf import normalize_sample_id

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = ROOT / "data" / "Muller_Milk_Collections_Monthly"
FARM_CODES = {farm.code for farm in FARMS}

_DATE_FORMATS = ("%d-%b-%y", "%d-%b-%Y")


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
                "payment_date": _parse_date(raw.get("Payment Date")),
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
    for day in (actual, payment):
        if day is not None and abs((row_date - day).days) <= 1:
            return False
    return True


def _close_dates(actual: dt.date | None, payment: dt.date | None) -> set[dt.date]:
    """Müller collection date, payment date, and the neighbouring day.

    A night load is often stamped after midnight, so the Collections date can be
    the day before the actual collection. Sample numbers also restart through the
    year, so a lone row on a distant month is not a match.
    """
    dates: set[dt.date] = set()
    for day in (actual, payment):
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
    close = _close_dates(actual, payment)
    dated = [row for row in candidates if row.sample_date in close]
    if len(dated) == 1:
        return dated[0], "matched"
    if len(dated) > 1:
        exact = {day for day in (actual, payment) if day is not None}
        on_exact = [row for row in dated if row.sample_date in exact]
        if len(on_exact) == 1:
            return on_exact[0], "matched"
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


def _match_by_volume(
    rows: list[NmlMilkResult],
    record: dict[str, Any],
    taken_ids: set[int],
) -> tuple[NmlMilkResult | None, str]:
    """Farm, date, and ticket litres. Used when Müller has no sample number."""
    target = _litres_key(record["litres"])
    if target is None:
        return None, "unmatched"
    close = _close_dates(record["actual_date"], record["payment_date"])
    hits = [
        row
        for row in rows
        if row.id not in taken_ids
        and row.sample_date in close
        and _litres_key(row.litres_load) == target
    ]
    if len(hits) == 1:
        return hits[0], "matched"
    if len(hits) > 1:
        return None, "ambiguous"
    return None, "unmatched"


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


def apply_muller_records(
    farm: str,
    records: list[dict[str, Any]],
    *,
    source_file: str,
    fill_temp: bool = False,
    repair_conflicts: bool = False,
    apply: bool = True,
) -> dict[str, Any]:
    """Write Müller litres onto existing tickets. Does not insert rows."""
    updated_ids: set[int] = set()
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

        claimed: dict[int, list[tuple[dict[str, Any], NmlMilkResult]]] = defaultdict(list)
        sampled = [record for record in records if record["sample_id"]]
        blank = [record for record in records if not record["sample_id"]]
        for record in sampled:
            candidates = index.get(record["sample_id"], [])
            chosen, status = _match_row(
                candidates, record["actual_date"], record["payment_date"]
            )
            if status == "unmatched":
                unmatched.append(_row_brief(record))
                continue
            if status == "ambiguous" or chosen is None:
                ambiguous.append(_row_brief(record, candidates))
                continue
            claimed[chosen.id].append((record, chosen))

        for record in blank:
            chosen, status = _match_by_volume(list(rows), record, set(claimed))
            if status == "ambiguous":
                ambiguous.append(_row_brief(record))
                continue
            if status != "matched" or chosen is None:
                unmatched.append(_row_brief(record))
                continue
            claimed[chosen.id].append((record, chosen))

        for claims in claimed.values():
            row = claims[0][1]
            litres = {record["litres"] for record, _row in claims}
            if len(litres) > 1:
                for record, _row in claims:
                    ambiguous.append(_row_brief(record, [row]))
                if repair_conflicts and row.litres_load is None and row.litres_weighbridge in litres:
                    row.litres_weighbridge = None
                    if fill_temp:
                        temps = {record["temp_c"] for record, _row in claims}
                        if row.temp_c in temps:
                            row.temp_c = None
                continue
            record = claims[-1][0]
            row.litres_weighbridge = record["litres"]
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
