"""Conception summaries for cow services, in the DairyComp breed-summary shape.

A service is one BRED on a lactation (lactation 1 and above). Its result is the
last pregnancy outcome before the next service:

- preg: a pregnancy check, or a calving, with no later open or abort
- open: an open check, or another service with no pregnancy recorded
- abort: an abort after that service
- other: sold, died, or no result

Conception rate and services per conception use pregnancies and opens only.
Other and abort stay in the totals and out of the rate. The 95% interval is a
Wilson interval and is blank below 20 diagnosed services.
"""

from __future__ import annotations

import datetime as dt
import math
import threading
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.breeding_sires import normalize_sire_code
from services.database import CowEvent
from services.events_common import _animal_identifier, _coerce_date
from services.farms import HERD_FARM_OPTIONS

_FERTILITY_EVENTS = ("FRESH", "BRED", "PREG", "OPEN", "ABORT", "SOLD", "DIED")
_EVENT_SORT = {"OPEN": 0, "ABORT": 1, "SOLD": 2, "DIED": 3, "FRESH": 4, "BRED": 5, "PREG": 6}
_OUTCOME_INDEX = {"preg": 0, "open": 1, "other": 2, "abort": 3}
_CI_MIN_DIAGNOSED = 20
_NOT_RECORDED = "Not recorded"
_DASH = "–"
_WILSON_Z = 1.959963984540054

Counts = tuple[int, int, int, int]
_ZERO: Counts = (0, 0, 0, 0)

_CACHE: dict[str, Any] = {"stamp": None, "cube": None}
_LOCK = threading.Lock()


def _round_half_up(value: float, digits: int = 0) -> Decimal:
    quant = Decimal("1") if digits == 0 else Decimal("0.1") ** digits
    return Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP)


def wilson_interval(pregnancies: int, diagnosed: int) -> tuple[int, int] | None:
    """95% Wilson interval on pregnancies / diagnosed, as whole percentages."""
    if diagnosed < _CI_MIN_DIAGNOSED or pregnancies < 0 or pregnancies > diagnosed:
        return None
    z2 = _WILSON_Z * _WILSON_Z
    p = pregnancies / diagnosed
    denom = 1 + z2 / diagnosed
    centre = (p + z2 / (2 * diagnosed)) / denom
    margin = _WILSON_Z * math.sqrt(p * (1 - p) / diagnosed + z2 / (4 * diagnosed * diagnosed)) / denom
    low = int(_round_half_up((centre - margin) * 100))
    high = int(_round_half_up((centre + margin) * 100))
    return low, high


def _ci_text(pregnancies: int, diagnosed: int) -> str:
    bounds = wilson_interval(pregnancies, diagnosed)
    if bounds is None:
        return _DASH
    return f"{bounds[0]}-{bounds[1]}"


def _pct_text(pregnancies: int, diagnosed: int) -> str:
    if diagnosed <= 0:
        return _DASH
    return str(int(_round_half_up(100 * pregnancies / diagnosed)))


def _spc_text(pregnancies: int, diagnosed: int) -> str:
    if pregnancies <= 0:
        return _DASH
    return f"{_round_half_up(diagnosed / pregnancies, 1):.1f}"


def _share_text(total: int, grand: int) -> str:
    if grand <= 0:
        return _DASH
    if total <= 0:
        return "0"
    return str(int(_round_half_up(100 * total / grand)))


def _sire_name(remark: str | None) -> str:
    code = normalize_sire_code(remark)
    if not code:
        return ""
    return code.upper()


def _tech_name(value: str | None) -> str:
    return (value or "").strip()


def _display_name(value: str) -> str:
    return value or _NOT_RECORDED


def _add(counts: Counts, outcome: str) -> Counts:
    values = list(counts)
    values[_OUTCOME_INDEX[outcome]] += 1
    return (values[0], values[1], values[2], values[3])


def _sum(rows: list[Counts]) -> Counts:
    preg = open_ = other = abort = 0
    for item in rows:
        preg += item[0]
        open_ += item[1]
        other += item[2]
        abort += item[3]
    return (preg, open_, other, abort)


def _outcome_after(
    ordered: list[tuple[dt.date, str, str | None, str | None]],
    start: int,
    service_day: dt.date,
) -> str:
    outcome: str | None = None
    index = start
    last = len(ordered)
    while index < last:
        day, name, _remark, _tech = ordered[index]
        if name == "BRED" and day > service_day:
            break
        if name == "PREG":
            outcome = "preg"
        elif name == "OPEN":
            outcome = "open"
        elif name == "ABORT":
            outcome = "abort"
        elif name == "FRESH":
            if outcome is None:
                outcome = "preg"
            return outcome
        elif name in ("SOLD", "DIED"):
            return outcome or "other"
        index += 1
    if outcome is not None:
        return outcome
    if index < last and ordered[index][1] == "BRED":
        return "open"
    return "other"


def lactation_services(
    events: list[tuple[dt.date, str, str | None, str | None]],
    fdat: dt.date | None,
) -> list[tuple[dt.date, str, str, str]]:
    """Services on one lactation: (date, sire, technician, outcome)."""
    if fdat is not None and not any(name == "FRESH" for _day, name, _remark, _tech in events):
        events = [(fdat, "FRESH", None, None), *events]
    ordered = sorted(events, key=lambda item: (item[0], _EVENT_SORT.get(item[1], 9)))
    fresh = next((day for day, name, _remark, _tech in ordered if name == "FRESH"), None)
    exit_day = next(
        (
            day
            for day, name, _remark, _tech in ordered
            if name in ("SOLD", "DIED") and (fresh is None or day >= fresh)
        ),
        None,
    )
    services: list[tuple[dt.date, str, str, str]] = []
    index = 0
    last = len(ordered)
    while index < last:
        day, name, remark, tech = ordered[index]
        if name != "BRED":
            index += 1
            continue
        if fresh is not None and day < fresh:
            index += 1
            continue
        if exit_day is not None and day >= exit_day:
            index += 1
            continue
        sire = _sire_name(remark)
        technician = _tech_name(tech)
        nxt = index + 1
        while nxt < last and ordered[nxt][0] == day and ordered[nxt][1] == "BRED":
            extra_sire = _sire_name(ordered[nxt][2])
            extra_tech = _tech_name(ordered[nxt][3])
            if extra_sire:
                sire = extra_sire
            if extra_tech:
                technician = extra_tech
            nxt += 1
        services.append((day, sire, technician, _outcome_after(ordered, nxt, day)))
        index = nxt
    return services


def _month_key(day: dt.date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _parse_month(value: str) -> int:
    year, month = value.split("-", 1)
    return int(year) * 12 + int(month) - 1


def _format_month(index: int) -> str:
    year, month_index = divmod(index, 12)
    return f"{year:04d}-{month_index + 1:02d}"


def month_blocks(start_month: str, end_month: str) -> list[tuple[str, str]]:
    """Full 12-month blocks ending on ``end_month``, newest first.

    A block is kept whole when the start month falls inside it. Moving the
    start into the previous block adds that whole block underneath.
    """
    start = _parse_month(start_month)
    end = _parse_month(end_month)
    if start > end:
        start, end = end, start
    blocks: list[tuple[str, str]] = []
    cursor = end
    while len(blocks) < 40:
        block_start = cursor - 11
        blocks.append((_format_month(block_start), _format_month(cursor)))
        if start >= block_start:
            break
        cursor = block_start - 1
    return blocks


def _months_between(start_month: str, end_month: str) -> list[str]:
    start = _parse_month(start_month)
    end = _parse_month(end_month)
    if start > end:
        start, end = end, start
    return [_format_month(index) for index in range(end, start - 1, -1)]


def _continuous_months(keys: set[str]) -> list[str]:
    if not keys:
        return []
    indexes = [_parse_month(key) for key in keys]
    return [_format_month(index) for index in range(min(indexes), max(indexes) + 1)]


def _shift_months(month: str, delta: int) -> str:
    return _format_month(_parse_month(month) + delta)


def _build_cube(db: Session) -> dict[str, Any]:
    rows = db.execute(
        select(
            CowEvent.farm,
            CowEvent.cow_id,
            CowEvent.etag,
            CowEvent.lact,
            CowEvent.event,
            CowEvent.event_date,
            CowEvent.remark,
            CowEvent.technician,
            CowEvent.fdat,
        )
        .where(CowEvent.event.in_(_FERTILITY_EVENTS))
        .where(CowEvent.event_date.isnot(None))
        .where(CowEvent.lact >= 1)
        .where(CowEvent.farm.in_(HERD_FARM_OPTIONS))
    ).all()

    grouped: dict[tuple[str, str, int], list[tuple[dt.date, str, str | None, str | None]]] = {}
    fresh_dates: dict[tuple[str, str, int], dt.date] = {}
    allowed = set(HERD_FARM_OPTIONS)
    for farm, cow_id, etag, lact, event, event_date, remark, technician, fdat in rows:
        if farm not in allowed or lact is None:
            continue
        day = _coerce_date(event_date)
        if not isinstance(day, dt.date):
            continue
        animal = _animal_identifier(cow_id, etag)
        if animal is None:
            continue
        key = (farm, animal, int(lact))
        grouped.setdefault(key, []).append(
            (
                day,
                event,
                remark if event == "BRED" else None,
                technician if event == "BRED" else None,
            )
        )
        fresh = _coerce_date(fdat)
        if isinstance(fresh, dt.date):
            fresh_dates.setdefault(key, fresh)

    by_month: dict[tuple[str, str], Counts] = {}
    by_sire: dict[tuple[str, str, str], Counts] = {}
    by_tech: dict[tuple[str, str, str], Counts] = {}
    seen_months: set[str] = set()
    for key, events in grouped.items():
        farm = key[0]
        for day, sire, technician, outcome in lactation_services(events, fresh_dates.get(key)):
            month = _month_key(day)
            seen_months.add(month)
            by_month[(farm, month)] = _add(by_month.get((farm, month), _ZERO), outcome)
            by_sire[(farm, month, sire)] = _add(by_sire.get((farm, month, sire), _ZERO), outcome)
            by_tech[(farm, month, technician)] = _add(by_tech.get((farm, month, technician), _ZERO), outcome)

    return {
        "months": _continuous_months(seen_months),
        "month": by_month,
        "sire": by_sire,
        "technician": by_tech,
    }


def get_conception_cube(db: Session) -> dict[str, Any]:
    stamp = db.execute(select(func.max(CowEvent.import_timestamp))).scalar()
    with _LOCK:
        if _CACHE["cube"] is not None and _CACHE["stamp"] == stamp:
            return _CACHE["cube"]
        cube = _build_cube(db)
        _CACHE["stamp"] = stamp
        _CACHE["cube"] = cube
        return cube


def _default_range(months: list[str]) -> tuple[str, str]:
    if not months:
        today = dt.date.today()
        end = _month_key(today)
        return _shift_months(end, -11), end
    end = months[-1]
    start = _shift_months(end, -11)
    if start < months[0]:
        start = months[0]
    return start, end


def _clamp_month(value: str | None, months: list[str], fallback: str) -> str:
    if not months:
        return fallback
    if value and value in months:
        return value
    if value and months[0] <= value <= months[-1]:
        return value
    return fallback


def _row(label: str, counts: Counts, grand: int) -> dict[str, Any]:
    pregnancies, opens, other, abort = counts
    total = pregnancies + opens + other + abort
    diagnosed = pregnancies + opens
    return {
        "label": label,
        "ci": _ci_text(pregnancies, diagnosed),
        "conc": _pct_text(pregnancies, diagnosed),
        "preg": pregnancies,
        "open": opens,
        "other": other,
        "abort": abort,
        "total": total,
        "share": _share_text(total, grand),
        "spc": _spc_text(pregnancies, diagnosed),
    }


def _table(rows: list[tuple[str, Counts]]) -> dict[str, Any]:
    summed = _sum([counts for _label, counts in rows])
    grand = sum(summed)
    body = [_row(label, counts, grand) for label, counts in rows]
    total = _row("TOTALS", summed, grand)
    if grand > 0:
        total["share"] = "100"
    return {"rows": body, "total": total}


def _month_heading(start_month: str, end_month: str) -> str:
    return f"By calendar month from {_stamp(start_month, False)} through {_stamp(end_month, True)}"


def _named_heading(kind: str, start_month: str, end_month: str) -> str:
    return f"By {kind} from {_stamp(start_month, False)} through {_stamp(end_month, True)}"


def _stamp(month: str, end: bool) -> str:
    year, month_no = (int(part) for part in month.split("-", 1))
    if end:
        if month_no == 12:
            day = 31
        else:
            day = (dt.date(year, month_no + 1, 1) - dt.timedelta(days=1)).day
    else:
        day = 1
    return f"{day:02d}/{month_no:02d}/{year % 100:02d}"


def _long_month(month: str) -> str:
    year, month_no = (int(part) for part in month.split("-", 1))
    return f"{year} {dt.date(year, month_no, 1):%B}"


def conception_tables(
    db: Session,
    farms: list[str] | None,
    start_month: str | None,
    end_month: str | None,
) -> dict[str, Any]:
    cube = get_conception_cube(db)
    months: list[str] = cube["months"]
    default_start, default_end = _default_range(months)
    start = _clamp_month(start_month, months, default_start)
    end = _clamp_month(end_month, months, default_end)
    if start > end:
        start, end = end, start
    selected = [farm for farm in HERD_FARM_OPTIONS if not farms or farm in farms]
    if not selected:
        selected = list(HERD_FARM_OPTIONS)

    month_tables = []
    for farm in selected:
        for block_start, block_end in month_blocks(start, end):
            counts = [
                (
                    _long_month(month),
                    cube["month"].get((farm, month), _ZERO),
                )
                for month in _months_between(block_start, block_end)
            ]
            month_tables.append(
                {
                    "farm": farm,
                    "heading": _month_heading(block_start, block_end),
                    **_table(counts),
                }
            )

    window = set(_months_between(start, end))
    sire_counts: dict[str, Counts] = {}
    tech_counts: dict[str, Counts] = {}
    for (farm, month, name), counts in cube["sire"].items():
        if farm in selected and month in window:
            sire_counts[name] = _sum([sire_counts.get(name, _ZERO), counts])
    for (farm, month, name), counts in cube["technician"].items():
        if farm in selected and month in window:
            tech_counts[name] = _sum([tech_counts.get(name, _ZERO), counts])

    def named_rows(bucket: dict[str, Counts]) -> list[tuple[str, Counts]]:
        return sorted(
            ((_display_name(name), counts) for name, counts in bucket.items()),
            key=lambda item: (-sum(item[1]), item[0].lower()),
        )

    return {
        "months": months,
        "from": start,
        "to": end,
        "farms": selected,
        "month_tables": month_tables,
        "sire": {
            "heading": _named_heading("sire", start, end),
            **_table(named_rows(sire_counts)),
        },
        "technician": {
            "heading": _named_heading("technician", start, end),
            **_table(named_rows(tech_counts)),
        },
    }
