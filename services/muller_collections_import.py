"""Apply Müller collection CSV litres to Collections weighbridge values.

One CSV row is one tanker collection. Litres is written to litres_weighbridge.
Payment Date is the primary calendar day. When that day and the Actual
Collection date differ, the collection day is used if it has the same sample
and the same litres.

ALH uses volume-tolerant matching (within 1000 L / 5%), sample override, and
provisional blank-sample order matching. Other farms match by sample ID and
exact ticket litres only. A portal row that does not land on a load is still
added as its own row, so the volume can be matched by hand.
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
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

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
# Do not invent a Collections line for tiny portal residues (e.g. 278 L).
_MIN_INSERT_LITRES = 1000.0
_QUALITY_COPY_FIELDS = (
    "butterfat_pct",
    "protein_pct",
    "scc",
    "bactoscan",
    "fpd",
    "antibiotic_pass",
    "urea_pct",
)


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
    """True when ticket and bridge are close enough to be the same load.

    Uses 1000 L, or 5% of the larger volume — so a flat 28,000 ticket still
    matches a 26,987 bridge (1,013 L apart).
    """
    left = _litres_key(ticket_litres)
    right = _litres_key(bridge_litres)
    if left is None or right is None:
        return False
    limit = max(_VOLUME_TOLERANCE_L, 0.05 * max(left, right))
    return abs(left - right) <= limit


def weighbridge_fits(ticket_litres: Any, bridge_litres: Any) -> bool:
    """True when a weighbridge can stay on this ticket.

    Same size class (small load under 10,000 L, or a full tanker) and within
    the usual tolerance. A missing side is not a clash.
    """
    if _litres_key(ticket_litres) is None or _litres_key(bridge_litres) is None:
        return True
    if _is_small_litres(ticket_litres) != _is_small_litres(bridge_litres):
        return False
    return _within_tolerance(ticket_litres, bridge_litres)


def clear_unfit_weighbridges(rows: list[Any]) -> int:
    """Drop a weighbridge that no longer belongs on its ticket litres."""
    cleared = 0
    for row in rows:
        ticket = getattr(row, "litres_load", None)
        bridge = getattr(row, "litres_weighbridge", None)
        if ticket is None or bridge is None:
            continue
        if weighbridge_fits(ticket, bridge):
            continue
        row.litres_weighbridge = None
        row.weighbridge_confirmed = False
        cleared += 1
    return cleared


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


# Claim: record, ticket, confirmed, override_sample_to_muller
Claim = tuple[dict[str, Any], NmlMilkResult, bool, bool]


def _volume_match_ticket(
    rows: list[NmlMilkResult],
    record: dict[str, Any],
    claimed: dict[int, list[Claim]],
) -> NmlMilkResult | None:
    """Single unclaimed ticket on the payment day within 1000 L of the portal litres."""
    day = _primary_date(record)
    if day is None:
        return None
    hits = [
        row
        for row in rows
        if row.id is not None
        and row.id not in claimed
        and row.sample_date == day
        and row.litres_load is not None
        and _within_tolerance(row.litres_load, record["litres"])
    ]
    if not hits:
        return None
    bridge_small = _is_small_litres(record["litres"])
    same = [row for row in hits if _is_small_litres(row.litres_load) == bridge_small]
    pool = same if same else hits
    if len(pool) == 1:
        return pool[0]
    bridge = _litres_key(record["litres"])
    assert bridge is not None
    # Prefer a farm sample that already mentions the portal sample (e.g. 95/96).
    portal = normalize_sample_id(record.get("sample_id") or "")
    if portal:
        mentioned = [
            row
            for row in pool
            if portal in normalize_sample_id(row.sample_id or "")
            or portal in str(row.sample_id or "")
        ]
        if len(mentioned) == 1:
            return mentioned[0]
        if mentioned:
            pool = mentioned
    pool = sorted(
        pool,
        key=lambda row: (
            abs((_litres_key(row.litres_load) or 0) - bridge),
            _ticket_sort_key(row),
        ),
    )
    best = abs((_litres_key(pool[0].litres_load) or 0) - bridge)
    second = abs((_litres_key(pool[1].litres_load) or 0) - bridge)
    if best < second:
        return pool[0]
    # Same distance: pick earliest load so matching is stable.
    return pool[0]


def _same_sample(row: NmlMilkResult, sample_id: str) -> bool:
    target = normalize_sample_id(sample_id)
    raw = str(row.sample_id or "").strip()
    return bool(target) and (
        normalize_sample_id(raw) == target or raw == str(sample_id).strip()
    )


def _same_portal_volume(row: NmlMilkResult, record: dict[str, Any]) -> bool:
    """True when this spare row is the same portal collection, not a different volume."""
    if _litres_key(row.litres_weighbridge) != _litres_key(record.get("litres")):
        return False
    sample = normalize_sample_id(record.get("sample_id") or "")
    if not sample:
        return bool(row.sample_missing)
    shown = str(getattr(row, "shown_sample_id", None) or "").strip()
    return _same_sample(row, sample) or normalize_sample_id(shown) == sample


def _sample_taken(
    rows: list[NmlMilkResult],
    *,
    farm: str,
    day: dt.date,
    sample_id: str,
    except_id: int | None,
) -> bool:
    """True when any other row that day already holds this sample (incl. orphans)."""
    for row in rows:
        if row.id == except_id:
            continue
        if row.farm != farm or row.sample_date != day:
            continue
        if _same_sample(row, sample_id):
            return True
    return False


def _is_orphan_bridge_row(row: NmlMilkResult) -> bool:
    """Müller-only line: bridge litres, no farm ticket volume."""
    return row.litres_load is None and row.litres_weighbridge is not None


def _merge_orphan_into(session: Any, ticket: NmlMilkResult, orphan: NmlMilkResult) -> None:
    for field in _QUALITY_COPY_FIELDS:
        if getattr(ticket, field) is None and getattr(orphan, field) is not None:
            setattr(ticket, field, getattr(orphan, field))
    if ticket.temp_c is None and orphan.temp_c is not None:
        ticket.temp_c = orphan.temp_c
    if ticket.litres_weighbridge is None and orphan.litres_weighbridge is not None:
        ticket.litres_weighbridge = orphan.litres_weighbridge
        ticket.weighbridge_confirmed = bool(
            getattr(orphan, "weighbridge_confirmed", False)
        )
    if not ticket.nml_matched and orphan.nml_matched:
        ticket.nml_matched = True
    session.delete(orphan)


def _displace_sample(row: NmlMilkResult) -> None:
    """Move a ticket off its sample ID with a unique placeholder (avoids uq clashes)."""
    row.sample_id = f"L{row.load_number or 0}-{row.id}"
    row.sample_missing = True


def _clear_sample_holders(
    session: Any,
    rows: list[NmlMilkResult],
    *,
    day: dt.date,
    sample_id: str,
    except_id: int | None,
) -> int:
    """Remove or displace every row holding this sample that day. Always flushes."""
    changed = 0
    for other in list(rows):
        if other.id == except_id:
            continue
        if other.sample_date != day:
            continue
        if not _same_sample(other, sample_id):
            continue
        if _is_orphan_bridge_row(other):
            session.delete(other)
            if other in rows:
                rows.remove(other)
        else:
            _displace_sample(other)
        changed += 1
    if changed:
        session.flush()
    return changed


def _claim_blank_by_size(
    blanks: list[dict[str, Any]],
    tickets: list[NmlMilkResult],
    claimed: dict[int, list[Claim]],
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
            _keep_brief(unmatched, record)
            continue
        claimed[chosen.id].append((record, chosen, False, False))


def _match_blank_records(
    blank: list[dict[str, Any]],
    rows: list[NmlMilkResult],
    claimed: dict[int, list[Claim]],
    unmatched: list[dict[str, Any]],
) -> None:
    """ALH provisional match for Müller rows with no sample number."""
    by_day: dict[dt.date, list[dict[str, Any]]] = defaultdict(list)
    for record in blank:
        day = _primary_date(record)
        if day is None:
            _keep_brief(unmatched, record)
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


def _match_blank_exact(
    blank: list[dict[str, Any]],
    rows: list[NmlMilkResult],
    claimed: dict[int, list[Claim]],
    unmatched: list[dict[str, Any]],
) -> None:
    """Non-ALH: blank portal rows match only on exact ticket litres."""
    for record in blank:
        target = _litres_key(record["litres"])
        if target is None:
            _keep_brief(unmatched, record)
            continue
        close = _close_dates(record["actual_date"], record["payment_date"])
        hits = [
            row
            for row in rows
            if row.id is not None
            and row.id not in claimed
            and row.sample_date in close
            and _litres_key(row.litres_load) == target
        ]
        if len(hits) == 1:
            claimed[hits[0].id].append((record, hits[0], True, False))
        elif len(hits) > 1:
            _keep_brief(unmatched, record, hits)
        else:
            _keep_brief(unmatched, record)


def _match_simple_ticket(
    candidates: list[NmlMilkResult],
    record: dict[str, Any],
) -> tuple[NmlMilkResult | None, str]:
    """Non-ALH sample match. Payment day first, then the collection day.

    Müller stamps an after-midnight collection on the previous payment day.
    The farm ticket is on the actual collection date. Use that ticket when it
    has the same sample and the same litres and the payment-day ticket does not.
    """
    actual = record.get("actual_date")
    payment = record.get("payment_date")
    litres = _litres_key(record.get("litres"))

    def tickets_on(day: dt.date | None) -> list[NmlMilkResult]:
        if day is None:
            return []
        return [
            row
            for row in candidates
            if row.sample_date == day and not _is_orphan_bridge_row(row)
        ]

    def exact_litres(rows: list[NmlMilkResult]) -> list[NmlMilkResult]:
        if litres is None:
            return []
        return [row for row in rows if _litres_key(row.litres_load) == litres]

    # Same sample on two loads: keep the ticket whose litres are the portal litres.
    days: list[dt.date] = []
    if payment is not None:
        days.append(payment)
    if actual is not None and actual not in days:
        days.append(actual)
    for day in days:
        exact = exact_litres(tickets_on(day))
        if len(exact) == 1:
            return exact[0], "matched"
        if len(exact) > 1:
            return None, "ambiguous"

    return _match_row(candidates, actual, payment)


def _claim_sampled_simple(
    sampled: list[dict[str, Any]],
    rows: list[NmlMilkResult],
    index: dict[str, list[NmlMilkResult]],
    claimed: dict[int, list[Claim]],
    *,
    session: Any,
    farm: str,
    source_file: str,
    fill_temp: bool,
    unmatched: list[dict[str, Any]],
    ambiguous: list[dict[str, Any]],
) -> int:
    """Non-ALH: match by sample ID only; insert when no ticket exists."""
    inserted = 0
    for record in sampled:
        candidates = index.get(record["sample_id"], [])
        chosen, status = _match_simple_ticket(candidates, record)
        if status == "ambiguous":
            _keep_brief(ambiguous, record, candidates)
            continue
        if status == "matched" and chosen is not None and chosen.id is not None:
            if _is_orphan_bridge_row(chosen):
                status = "unmatched"
            else:
                claimed[chosen.id].append((record, chosen, True, False))
                continue

        day = _primary_date(record)
        if day is not None:
            _clear_sample_holders(
                session,
                rows,
                day=day,
                sample_id=record["sample_id"],
                except_id=None,
            )
        created = _insert_weighbridge_row(
            session,
            farm,
            record,
            list(rows),
            source_file=source_file,
            fill_temp=fill_temp,
        )
        if created is None:
            _keep_brief(unmatched, record)
            continue
        rows.append(created)
        index.setdefault(record["sample_id"], []).append(created)
        inserted += 1
    return inserted


def _shared_sample_ticket(
    candidates: list[NmlMilkResult],
    record: dict[str, Any],
) -> NmlMilkResult | None:
    """The one load already showing this sample whose litres fit the portal row.

    Two loads may share a sample. Each portal row confirms the load it fits.
    Two loads that both fit the same portal row are left unconfirmed.
    """

    def fitting(day: dt.date | None) -> list[NmlMilkResult]:
        if day is None:
            return []
        pool = [
            row
            for row in candidates
            if row.id is not None
            and not _is_orphan_bridge_row(row)
            and row.sample_date == day
            and _within_tolerance(row.litres_load, record["litres"])
        ]
        exact_key = _litres_key(record.get("litres"))
        exact = [row for row in pool if _litres_key(row.litres_load) == exact_key]
        if len(exact) == 1:
            return exact
        if exact:
            return []
        if len(pool) == 1:
            return pool
        return []

    seen: set[dt.date] = set()
    for day in (record.get("payment_date"), record.get("actual_date")):
        if day is None or day in seen:
            continue
        seen.add(day)
        hits = fitting(day)
        if len(hits) == 1:
            return hits[0]
    return None


def _volume_distance(ticket_litres: Any, bridge_litres: Any) -> int | None:
    left = _litres_key(ticket_litres)
    right = _litres_key(bridge_litres)
    if left is None or right is None:
        return None
    return abs(left - right)


def _claim_sampled_records(
    sampled: list[dict[str, Any]],
    rows: list[NmlMilkResult],
    index: dict[str, list[NmlMilkResult]],
    claimed: dict[int, list[Claim]],
    *,
    session: Any,
    farm: str,
    source_file: str,
    fill_temp: bool,
    unmatched: list[dict[str, Any]],
    ambiguous: list[dict[str, Any]],
) -> int:
    """Match portal rows that have a sample number. Returns insert count.

    A repeated sample confirms every load that already shows it and whose litres
    fit. A load with a different sample stays provisional.
    """
    inserted = 0
    # score, record, ticket, adopt_sample (override farm sample to Müller's)
    pairs: list[tuple[int, dict[str, Any], NmlMilkResult, bool]] = []

    for record in sampled:
        sample_hits = index.get(record["sample_id"], [])
        chosen, status = _match_row(
            sample_hits, record["actual_date"], record["payment_date"]
        )
        if status == "ambiguous":
            shared = _shared_sample_ticket(sample_hits, record)
            if shared is not None and shared.id is not None:
                dist = _volume_distance(shared.litres_load, record["litres"])
                if dist is not None:
                    pairs.append((dist * 2, record, shared, False))
                    continue
            _keep_brief(ambiguous, record, sample_hits)
            continue

        if (
            status == "matched"
            and chosen is not None
            and chosen.id is not None
            and not _is_orphan_bridge_row(chosen)
            and _within_tolerance(chosen.litres_load, record["litres"])
        ):
            dist = _volume_distance(chosen.litres_load, record["litres"])
            if dist is not None:
                # Closest litres win sample ownership; sample match wins ties.
                pairs.append((dist * 2, record, chosen, False))

        volume_hit = _volume_match_ticket(rows, record, {})
        if volume_hit is not None and volume_hit.id is not None:
            dist = _volume_distance(volume_hit.litres_load, record["litres"])
            if dist is not None:
                already = any(
                    item[1] is record and item[2].id == volume_hit.id for item in pairs
                )
                if not already:
                    pairs.append((dist * 2 + 1, record, volume_hit, True))

    pairs.sort(key=lambda item: (item[0], _sort_datetime(item[1])))
    used_records: set[int] = set()
    samples_owned: set[tuple[dt.date, str]] = set()

    for _score, record, ticket, adopt in pairs:
        if ticket.id is None or ticket.id in claimed:
            continue
        if id(record) in used_records:
            continue

        day = ticket.sample_date or _primary_date(record)
        sample_key = (
            (day, normalize_sample_id(record["sample_id"]))
            if day and record["sample_id"]
            else None
        )
        sample_taken = sample_key is not None and sample_key in samples_owned

        if adopt:
            if sample_taken:
                claimed[ticket.id].append((record, ticket, False, False))
            else:
                claimed[ticket.id].append((record, ticket, True, True))
                if sample_key is not None:
                    samples_owned.add(sample_key)
        else:
            # This load already shows the sample, so a repeat still confirms.
            claimed[ticket.id].append((record, ticket, True, False))
            if sample_key is not None:
                samples_owned.add(sample_key)
        used_records.add(id(record))

    for record in sampled:
        if id(record) in used_records:
            continue
        # Ticket taken by a better pair — try another free volume match.
        chosen = _volume_match_ticket(rows, record, claimed)
        if chosen is not None and chosen.id is not None:
            claimed[chosen.id].append((record, chosen, False, False))
            used_records.add(id(record))
            continue

        litres = _litres_key(record["litres"])
        if litres is None or litres < _MIN_INSERT_LITRES:
            _keep_brief(unmatched, record)
            continue
        day = _primary_date(record)
        sample_key = (
            (day, normalize_sample_id(record["sample_id"]))
            if day and record["sample_id"]
            else None
        )
        if sample_key is not None and sample_key in samples_owned:
            _keep_brief(unmatched, record)
            continue
        # A real ticket already has this sample — don't invent a second line.
        if day is not None and any(
            other.farm == farm
            and other.sample_date == day
            and not _is_orphan_bridge_row(other)
            and _same_sample(other, record["sample_id"])
            for other in rows
        ):
            _keep_brief(unmatched, record)
            continue

        if day is not None:
            _clear_sample_holders(
                session,
                rows,
                day=day,
                sample_id=record["sample_id"],
                except_id=None,
            )
        created = _insert_weighbridge_row(
            session,
            farm,
            record,
            list(rows),
            source_file=source_file,
            fill_temp=fill_temp,
        )
        if created is None:
            _keep_brief(unmatched, record)
            continue
        rows.append(created)
        index.setdefault(record["sample_id"], []).append(created)
        inserted += 1
        used_records.add(id(record))

    return inserted


def _keep_brief(
    bucket: list[dict[str, Any]],
    record: dict[str, Any],
    rows: list[NmlMilkResult] | None = None,
    *,
    held_litres: Any = None,
) -> None:
    """Record a portal row that was not written onto a load, and keep the row itself."""
    brief = _row_brief(record, rows)
    brief["_record"] = record
    if held_litres is not None:
        brief["_held_litres"] = held_litres
    bucket.append(brief)


def _strip_private(briefs: list[dict[str, Any]]) -> None:
    for brief in briefs:
        brief.pop("_record", None)
        brief.pop("_held_litres", None)


def _shown_sample(row: NmlMilkResult) -> str:
    if row.sample_missing:
        return ""
    shown = str(getattr(row, "shown_sample_id", None) or "").strip()
    if shown:
        return normalize_sample_id(shown)
    return normalize_sample_id(row.sample_id)


def _stored_sample_ids(
    existing: list[NmlMilkResult], farm: str, day: dt.date
) -> set[str]:
    return {
        str(row.sample_id or "").strip()
        for row in existing
        if row.farm == farm and row.sample_date == day
    }


def _unique_stored_sample(
    existing: list[NmlMilkResult], farm: str, day: dt.date, sample: str
) -> tuple[str, str | None]:
    """Return (stored id, shown sample). A repeat keeps the visible number."""
    taken = _stored_sample_ids(existing, farm, day)
    if sample not in taken:
        return sample, None
    n = 2
    while f"{sample}#{n}" in taken:
        n += 1
    return f"{sample}#{n}", sample


def _unique_blank_sample(
    existing: list[NmlMilkResult], farm: str, day: dt.date
) -> str:
    taken = _stored_sample_ids(existing, farm, day)
    n = 1
    while f"WB{n}" in taken:
        n += 1
    return f"WB{n}"


def _keep_spare_weighbridges(
    session: Any,
    farm: str,
    rows: list[NmlMilkResult],
    leftovers: list[dict[str, Any]],
    *,
    source_file: str,
    fill_temp: bool,
) -> int:
    """Add a row for every portal volume that was not written onto a load.

    A volume already on the table for that day and sample is left as it is,
    so uploading the same file again does not create a second copy.
    """
    budget: dict[tuple[dt.date, str, int], int] = defaultdict(int)
    for row in rows:
        if row.farm != farm or row.sample_date is None:
            continue
        litres = _litres_key(row.litres_weighbridge)
        if litres is None:
            continue
        budget[(row.sample_date, _shown_sample(row), litres)] += 1

    inserted = 0
    for brief in leftovers:
        record = brief.get("_record")
        if not isinstance(record, dict):
            continue
        day = _primary_date(record)
        litres = _litres_key(record.get("litres"))
        if day is None or litres is None:
            continue
        sample = normalize_sample_id(record.get("sample_id") or "")
        held = _litres_key(brief.get("_held_litres"))
        if held is not None and held == litres:
            continue
        key = (day, sample, litres)
        if budget[key] > 0:
            budget[key] -= 1
            continue
        created = _insert_weighbridge_row(
            session,
            farm,
            record,
            list(rows),
            source_file=source_file,
            fill_temp=fill_temp,
        )
        if created is None:
            continue
        rows.append(created)
        inserted += 1
    return inserted


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
    """Add a Müller volume that did not land on a farm ticket.

    A repeated sample keeps the same visible number. A blank sample is stored
    as its own row with no sample number showing.
    """
    sample_date = _placeholder_date(record)
    producer_ref = FARM_PRODUCER_REF.get(farm)
    litres = record.get("litres")
    if sample_date is None or not producer_ref or litres is None:
        return None
    raw_sample = normalize_sample_id(record.get("sample_id") or "")
    shown_sample: str | None = None
    sample_missing = False
    if raw_sample:
        sample_id, shown_sample = _unique_stored_sample(
            existing, farm, sample_date, raw_sample
        )
    else:
        sample_id = _unique_blank_sample(existing, farm, sample_date)
        sample_missing = True
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
        shown_sample_id=shown_sample,
        load_number=(max(taken) if taken else 0) + 1,
        litres_weighbridge=litres,
        weighbridge_confirmed=not sample_missing,
        report_month=f"{calendar.month_name[sample_date.month]} {sample_date.year}",
        sample_missing=sample_missing,
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
    merged_orphans = 0
    temp_filled = 0
    cleared = 0
    unmatched: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    date_gaps: list[dict[str, Any]] = []

    try:
        with get_session() as session:
            rows = list(
                session.scalars(
                    select(NmlMilkResult).where(NmlMilkResult.farm == farm)
                ).all()
            )
            cleared = clear_unfit_weighbridges(rows)
            index: dict[str, list[NmlMilkResult]] = {}
            for row in rows:
                if _is_orphan_bridge_row(row):
                    continue
                shown = str(getattr(row, "shown_sample_id", None) or "").strip()
                sample = normalize_sample_id(shown or row.sample_id)
                if row.sample_missing or not sample or not sample.isdigit():
                    continue
                index.setdefault(sample, []).append(row)

            claimed: dict[int, list[Claim]] = defaultdict(list)
            sampled = [record for record in records if record["sample_id"]]
            blank = [record for record in records if not record["sample_id"]]
            alh = farm.upper() == "ALH"
            if alh:
                inserted = _claim_sampled_records(
                    sampled,
                    rows,
                    index,
                    claimed,
                    session=session,
                    farm=farm,
                    source_file=source_file,
                    fill_temp=fill_temp,
                    unmatched=unmatched,
                    ambiguous=ambiguous,
                )
                _match_blank_records(blank, rows, claimed, unmatched)
            else:
                inserted = _claim_sampled_simple(
                    sampled,
                    rows,
                    index,
                    claimed,
                    session=session,
                    farm=farm,
                    source_file=source_file,
                    fill_temp=fill_temp,
                    unmatched=unmatched,
                    ambiguous=ambiguous,
                )
                _match_blank_exact(blank, rows, claimed, unmatched)

            for claims in claimed.values():
                row = claims[0][1]
                litres = {record["litres"] for record, _row, _ok, _ov in claims}
                if len(litres) > 1:
                    # Prefer the claim that fits the ticket litres when Müller reused a sample.
                    fitting = [
                        item
                        for item in claims
                        if row.litres_load is None
                        or _within_tolerance(row.litres_load, item[0]["litres"])
                    ]
                    if len(fitting) == 1:
                        for record, _ticket, _ok, _ov in claims:
                            if record is not fitting[0][0]:
                                _keep_brief(unmatched, record, [row])
                        claims = fitting
                    else:
                        if (
                            repair_conflicts
                            and row.litres_load is None
                            and row.litres_weighbridge in litres
                        ):
                            row.litres_weighbridge = None
                            row.weighbridge_confirmed = False
                            if fill_temp:
                                temps = {
                                    record["temp_c"] for record, _row, _ok, _ov in claims
                                }
                                if row.temp_c in temps:
                                    row.temp_c = None
                        for record, _row, _ok, _ov in claims:
                            _keep_brief(
                                ambiguous,
                                record,
                                [row],
                                held_litres=row.litres_weighbridge,
                            )
                        continue

                # Confirmed sample claim wins over a provisional one on the same ticket.
                confirmed_claims = [item for item in claims if item[2]]
                record, _chosen, confirmed, override = (
                    confirmed_claims[-1] if confirmed_claims else claims[-1]
                )
                if confirmed_claims:
                    confirmed = True
                    override = any(item[3] for item in confirmed_claims)
                    record = confirmed_claims[-1][0]

                # Fold a spare copy of this same portal volume into the ticket.
                # A different volume is left on its own row, even when it is close.
                day = row.sample_date
                if day is not None:
                    orphans = [
                        other
                        for other in list(rows)
                        if other.id != row.id
                        and other.sample_date == day
                        and _is_orphan_bridge_row(other)
                        and _same_portal_volume(other, record)
                    ]
                    for orphan in orphans:
                        _merge_orphan_into(session, row, orphan)
                        if orphan in rows:
                            rows.remove(orphan)
                        merged_orphans += 1
                    if orphans:
                        session.flush()

                if override and record["sample_id"] and day is not None:
                    # Free every other holder (tickets + stragglers) then adopt Müller sample.
                    _clear_sample_holders(
                        session,
                        rows,
                        day=day,
                        sample_id=record["sample_id"],
                        except_id=row.id,
                    )
                    row.sample_id = record["sample_id"]
                    row.sample_missing = False
                    session.flush()

                if not weighbridge_fits(row.litres_load, record["litres"]):
                    _keep_brief(unmatched, record, [row])
                    continue
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
                if _date_gap(
                    row.sample_date, record["actual_date"], record["payment_date"]
                ):
                    gap = _row_brief(record)
                    gap["row_date"] = (
                        row.sample_date.isoformat() if row.sample_date else ""
                    )
                    date_gaps.append(gap)

            inserted += _keep_spare_weighbridges(
                session,
                farm,
                rows,
                list(unmatched) + list(ambiguous),
                source_file=source_file,
                fill_temp=fill_temp,
            )
            _strip_private(unmatched)
            _strip_private(ambiguous)
            if apply:
                from services.nml_manual import join_lab_and_weighbridge_rows

                join_lab_and_weighbridge_rows(session)
            else:
                session.rollback()
    except IntegrityError as exc:
        raise ValueError(
            "Could not save Müller weighbridge values — a sample number clash on the same day. "
            "Check duplicate samples on the portal or in Collections."
        ) from exc
    except SQLAlchemyError as exc:
        raise ValueError(f"Could not save Müller weighbridge values: {exc}") from exc

    return {
        "source_file": source_file,
        "farm": farm,
        "rows_parsed": len(records),
        "rows_updated": len(updated_ids),
        "rows_cleared": cleared,
        "rows_inserted": inserted,
        "rows_merged": merged_orphans,
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
        except UnicodeDecodeError:
            errors.append(
                {
                    "source_file": name or filename,
                    "error": f"{name} is not a text CSV.",
                }
            )
            continue
        farm = farm_from_filename(name)
        assert farm is not None
        try:
            records = parse_muller_csv_text(name, text)
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
        except (ValueError, SQLAlchemyError) as exc:
            logger.exception("Müller import failed for %s", name)
            errors.append({"source_file": name or filename, "error": str(exc)})

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
                "rows_cleared": item.get("rows_cleared", 0),
                "rows_inserted": item.get("rows_inserted", 0),
                "rows_unmatched": item["rows_unmatched"],
                "rows_ambiguous": item["rows_ambiguous"],
            }
            for item in results
        ],
        "errors": errors,
        "rows_updated": sum(item["rows_updated"] for item in results),
        "rows_cleared": sum(item.get("rows_cleared", 0) for item in results),
        "rows_inserted": sum(item.get("rows_inserted", 0) for item in results),
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
