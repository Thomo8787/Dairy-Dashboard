"""Daily cows-in-milk counts from herd inventory + DairyComp events.

Inventory defines who is currently milking (LACT > 0 and RPRO != DRY),
including the fresh date. Other farms rebuild history from FRESH / DRY /
SOLD / DIED.

ALH and BNK share one herd. Stock inventory stays on the barn name. Cows in
milk follow the pen. Bank is pens 89 to 93. ALH is every other pen, and pen
150 is left out. She joins Bank on a MOVE into those pens and leaves on a
MOVE out, DRY, SOLD, or DIED. Fresh pens 1 and 6 stay on ALH. Today's count
uses the pen on the herd file. Earlier days are rebuilt from each cow's
moves. A cow now in pen 150 with no MOVE into that pen still leaves ALH on
the day that ear tag arrives on another farm.

An abort opens lactation 1 with no FRESH line. It counts as a calving only
when a later move shows her in pen 1, pen 6, or pens 89 to 93. The abort
itself can put her in a fresh pen with no MOVE line, so the next line often
shows her leaving that pen. An early abort that is sold from a heifer pen
does not count.
"""

from __future__ import annotations

import datetime as dt
import itertools
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.database import CowEvent, HerdInventory
from services.duplicate_animals import ear_tag_key

_TRACKED_EVENTS = ("FRESH", "DRY", "SOLD", "DIED")
_PEN_HISTORY_EVENTS = ("FRESH", "ABORT", "DRY", "SOLD", "DIED", "MOVE")
_END_MILK_EVENTS = frozenset({"DRY", "SOLD", "DIED"})
_STOP_ABORT_SEARCH = frozenset({"DRY", "SOLD", "DIED", "FRESH", "ABORT"})
_START_IN_MILK = "IN MILK"
SHARED_HERD_FARMS = frozenset({"ALH", "BNK"})
BNK_MILK_PENS = frozenset(range(89, 94))
FRESH_PENS = frozenset({1, 6})
MILKING_ENTRY_PENS = FRESH_PENS | BNK_MILK_PENS
MOVED_PEN = 150
MOVE_REMARK = re.compile(r"^F(\d{3})T(\d{3})$")
ABORT_DAYS_REMARK = re.compile(r"^\d+\s+DAYS$")


def pen_number(value: object) -> int | None:
    """Parse a DairyComp pen. ``89`` and ``89.0`` are the same pen."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    if not number.is_integer():
        return None
    return int(number)


def shared_inventory_farm(pen: object) -> str | None:
    """Today's farm for a cow in the shared ALH/BNK herd.

    Pens 89 to 93 are BNK. Pen 150 has moved to another farm and is left out.
    Every other pen, including a blank pen, is ALH.
    """
    number = pen_number(pen)
    if number == MOVED_PEN:
        return None
    if number in BNK_MILK_PENS:
        return "BNK"
    return "ALH"


def _is_milking_inventory_row(
    rpro: str | None,
    lact: object,
    dim: object,
    *,
    min_dim: int | None = None,
) -> bool:
    try:
        lact_n = float(lact) if lact is not None else 0.0
    except (TypeError, ValueError):
        lact_n = 0.0
    if lact_n <= 0:
        return False
    if (rpro or "").strip().upper() == "DRY":
        return False
    if min_dim is None:
        return True
    try:
        dim_n = float(dim) if dim is not None else None
    except (TypeError, ValueError):
        dim_n = None
    return dim_n is not None and dim_n > min_dim


def current_cows_in_milk(
    db: Session,
    farm: str,
    *,
    min_dim: int | None = None,
) -> int:
    """Cows in milk from the latest inventory snapshot for ``farm``."""
    farm_key = (farm or "").strip().upper()
    if farm_key in SHARED_HERD_FARMS:
        rows = db.execute(
            select(
                HerdInventory.cow_id,
                HerdInventory.pen,
                HerdInventory.rpro,
                HerdInventory.lact,
                HerdInventory.dim,
            ).where(HerdInventory.farm.in_(tuple(SHARED_HERD_FARMS)))
        ).all()
        return _shared_today_count(rows, farm_key, min_dim=min_dim)

    rows = db.execute(
        select(HerdInventory.rpro, HerdInventory.lact, HerdInventory.dim).where(
            HerdInventory.farm == farm_key
        )
    ).all()
    return sum(
        1
        for rpro, lact, dim in rows
        if _is_milking_inventory_row(rpro, lact, dim, min_dim=min_dim)
    )


def _intervals_from_events(
    evs: list[tuple[dt.date, str]],
) -> list[tuple[dt.date, dt.date | None]]:
    intervals: list[tuple[dt.date, dt.date | None]] = []
    open_start: dt.date | None = None
    for event_date, event in evs:
        if event == "FRESH":
            if open_start is not None and open_start < event_date:
                intervals.append((open_start, event_date))
            open_start = event_date
        elif open_start is not None:
            if open_start < event_date:
                intervals.append((open_start, event_date))
            open_start = None
    if open_start is not None:
        intervals.append((open_start, None))
    return intervals


def _apply_min_dim(
    intervals: list[tuple[dt.date, dt.date | None]],
    *,
    min_dim: int | None = None,
) -> list[tuple[dt.date, dt.date | None]]:
    if min_dim is None:
        return list(intervals)
    shifted: list[tuple[dt.date, dt.date | None]] = []
    offset = dt.timedelta(days=min_dim + 1)
    for start, end in intervals:
        countable_start = start + offset
        if end is not None and countable_start >= end:
            continue
        shifted.append((countable_start, end))
    return shifted


def _cow_intervals(
    evs: list[tuple[dt.date, str]],
    *,
    fdat: dt.date | None,
    currently_milking: bool | None,
    today: dt.date,
    min_dim: int | None = None,
) -> list[tuple[dt.date, dt.date | None]]:
    base = _intervals_from_events(evs)
    if fdat is None or currently_milking is None:
        return _apply_min_dim(base, min_dim=min_dim)

    kept: list[tuple[dt.date, dt.date | None]] = []
    for start, end in base:
        if end is not None and end <= fdat:
            kept.append((start, end))
        elif start < fdat:
            kept.append((start, fdat))

    if currently_milking:
        kept.append((fdat, None))
        return _apply_min_dim(kept, min_dim=min_dim)

    dry_end = None
    for event_date, event in evs:
        if event == "DRY" and event_date >= fdat:
            dry_end = event_date
            break
    if dry_end is None:
        dry_end = today
    if fdat < dry_end:
        kept.append((fdat, dry_end))
    return _apply_min_dim(kept, min_dim=min_dim)


def _shared_today_count(rows: Iterable[object], farm: str, *, min_dim: int | None) -> int:
    """Milking cows in the shared herd whose current pen belongs to ``farm``."""
    seen: set[str] = set()
    count = 0
    for row in rows:
        if isinstance(row, HerdInventory):
            stored_farm = row.farm
            cow_id, pen, rpro, lact, dim = row.cow_id, row.pen, row.rpro, row.lact, row.dim
        else:
            stored_farm = getattr(row, "farm", None)
            cow_id, pen, rpro, lact, dim = row[0], row[1], row[2], row[3], row[4]
        if stored_farm is not None and stored_farm not in SHARED_HERD_FARMS:
            continue
        if shared_inventory_farm(pen) != farm:
            continue
        identity = str(cow_id or "")
        if identity and identity in seen:
            continue
        if not _is_milking_inventory_row(rpro, lact, dim, min_dim=min_dim):
            continue
        if identity:
            seen.add(identity)
        count += 1
    return count


@dataclass
class MilkSegment:
    """One stretch of a cow's history after an event changes pen or milking."""

    start: dt.date
    in_milk: bool
    fdat: dt.date | None
    pen: int | None
    pen_note: str = ""
    milk_note: str = ""


@dataclass(frozen=True)
class MoveFact:
    """One MOVE line, compared with the pen the rebuild had her in."""

    cow_id: str
    date: dt.date
    remark: str
    pen_before: int | None
    pen_note: str
    in_milk: bool
    from_pen: int
    to_pen: int
    filled_since: dt.date | None


@dataclass(frozen=True)
class _HistoryEvent:
    date: dt.date
    order: int
    name: str
    remark: str
    fdat: dt.date | None
    lact: int


def _event_is_abort(event: object) -> bool:
    """True when this line is the lactation-1 calving DairyComp writes as ABORT.

    The import stores that line as FRESH and keeps the days-pregnant remark.
    A real fresh remark is a treatment or a name, not ``227 DAYS``.
    """
    try:
        lact = int(getattr(event, "lact", 0) or 0)
    except (TypeError, ValueError):
        return False
    fdat = getattr(event, "fdat", None)
    event_date = getattr(event, "date", None)
    if lact != 1 or fdat is None or fdat != event_date:
        return False
    name = (getattr(event, "name", "") or "").upper()
    if name == "ABORT":
        return True
    remark = (getattr(event, "remark", "") or "").strip().upper()
    return name == "FRESH" and ABORT_DAYS_REMARK.match(remark) is not None


def _confirmed_abort_keys(events: Sequence[object]) -> set[tuple[dt.date, int]]:
    """Abort calvings followed by a move through a fresh pen or a Bank pen.

    The search stops at the next fresh, abort, dry, sold, or died. Moves on
    that same day still count, so a same-day sale does not hide the move.
    """
    ordered = sorted(events, key=lambda event: (event.date, event.order))
    confirmed: set[tuple[dt.date, int]] = set()
    for event in ordered:
        if not _event_is_abort(event):
            continue
        end_on: dt.date | None = None
        for later in ordered:
            if later.date == event.date and later.order == event.order:
                continue
            if later.date < event.date:
                continue
            if (
                end_on is None
                and later.date > event.date
                and (later.name or "").upper() in _STOP_ABORT_SEARCH
            ):
                end_on = later.date
            if end_on is not None and later.date > end_on:
                break
            if (later.name or "").upper() != "MOVE":
                continue
            remark = (later.remark or "").strip().upper()
            match = MOVE_REMARK.match(remark)
            if match and (
                int(match.group(1)) in MILKING_ENTRY_PENS
                or int(match.group(2)) in MILKING_ENTRY_PENS
            ):
                confirmed.add((event.date, event.order))
                break
    return confirmed


def _with_missing_fresh_dates(
    events: Sequence[object], entry: dt.date | None
) -> list[object]:
    """Add a start for each lactation whose FRESH line is not in the file.

    An abort date is not filled in. A confirmed abort is its own start, and
    an early abort must not become a milking spell.
    """
    fresh_dates = {
        event.date for event in events if (event.name or "").upper() == "FRESH"
    }
    abort_dates = {
        (event.fdat or event.date) for event in events if _event_is_abort(event)
    }
    added: dict[dt.date, object] = {}
    for event in events:
        fdat = event.fdat
        try:
            lact = int(event.lact or 0)
        except (TypeError, ValueError):
            lact = 0
        if lact <= 0 or fdat is None or fdat > event.date:
            continue
        if fdat in fresh_dates or fdat in abort_dates or fdat in added:
            continue
        start = max(fdat, entry) if entry is not None else fdat
        added[fdat] = _HistoryEvent(
            date=start,
            order=-1,
            name=_START_IN_MILK,
            remark="",
            fdat=fdat,
            lact=lact,
        )
    return sorted([*events, *added.values()], key=lambda event: (event.date, event.order))


def _link_moves(moves: list[object], pen: int | None) -> list[object]:
    """Order one day's moves so each starts in the pen the last one ended in."""
    if len(moves) < 2 or len(moves) > 6:
        return moves
    best, best_links = moves, -1
    for order in itertools.permutations(moves):
        links = 0
        current = pen
        for move in order:
            match = MOVE_REMARK.match((move.remark or "").strip().upper())
            if match is None:
                continue
            if current is not None and int(match.group(1)) == current:
                links += 1
            current = int(match.group(2))
        if links > best_links:
            best, best_links = list(order), links
    return best


def _day_order(
    day: list[object], confirmed: set[tuple[dt.date, int]]
) -> tuple[list[object], list[object], list[object]]:
    """Calvings first, then moves, then the events that end her milking."""
    starts = []
    for event in day:
        name = (event.name or "").upper()
        # An abort stored as FRESH still has to pass the pen check. Leaving it
        # in with the other fresh lines would count an early abort.
        if _event_is_abort(event):
            if (event.date, event.order) in confirmed:
                starts.append(event)
            continue
        if name in ("FRESH", _START_IN_MILK):
            starts.append(event)
    moves = [
        event
        for event in day
        if (event.name or "").upper() == "MOVE"
        and MOVE_REMARK.match((event.remark or "").strip().upper())
    ]
    ends = [event for event in day if (event.name or "").upper() in _END_MILK_EVENTS]
    return starts, moves, ends


def build_milk_segments(
    cow_id: str, events: Sequence[object], entry: dt.date | None
) -> tuple[list[MilkSegment], list[MoveFact]]:
    """Rebuild one cow's milking spells and pens from her events.

    ``events`` need ``date``, ``order``, ``name``, ``remark``, ``fdat``, and
    ``lact``. Events before ``entry`` are ignored.
    """
    if entry is not None:
        events = [event for event in events if event.date >= entry]
    confirmed = _confirmed_abort_keys(events)
    ordered = _with_missing_fresh_dates(events, entry)

    segments: list[MilkSegment] = []
    facts: list[MoveFact] = []
    in_milk = False
    fdat: dt.date | None = None
    pen: int | None = None
    not_known_from = 0
    pen_note = "pen not known, no MOVE line"
    milk_note = "no fresh date"

    def add_segment(day: dt.date) -> None:
        segments.append(
            MilkSegment(
                start=day,
                in_milk=in_milk,
                fdat=fdat,
                pen=pen,
                pen_note=pen_note,
                milk_note=milk_note,
            )
        )

    for day, group in itertools.groupby(ordered, key=lambda event: event.date):
        starts, moves, ends = _day_order(list(group), confirmed)
        for event in starts:
            if (event.name or "").upper() == _START_IN_MILK:
                if fdat == event.fdat:
                    continue
                in_milk = True
                fdat = event.fdat
                milk_note = (
                    f"fresh {event.fdat.strftime('%d %b %Y') if event.fdat else 'no date'}"
                )
                add_segment(day)
                continue
            in_milk = True
            fdat = event.fdat or event.date
            pen = None
            not_known_from = len(segments)
            shown = fdat.strftime("%d %b %Y")
            pen_note = f"pen not known since fresh {shown}"
            milk_note = f"fresh {shown}"
            add_segment(day)

        for event in _link_moves(moves, pen):
            match = MOVE_REMARK.match((event.remark or "").strip().upper())
            if match is None:
                continue
            from_pen, to_pen = int(match.group(1)), int(match.group(2))
            pen_before = pen
            note_before = pen_note
            filled_since = None
            if pen is None:
                filled_since = (
                    segments[not_known_from].start
                    if not_known_from < len(segments)
                    else day
                )
                for segment in segments[not_known_from:]:
                    segment.pen = from_pen
            pen = to_pen
            remark = (event.remark or "").strip().upper()
            pen_note = f"pen {to_pen} since MOVE {remark} {day.strftime('%d %b %Y')}"
            facts.append(
                MoveFact(
                    cow_id=cow_id,
                    date=day,
                    remark=remark,
                    pen_before=pen_before,
                    pen_note=note_before,
                    in_milk=in_milk,
                    from_pen=from_pen,
                    to_pen=to_pen,
                    filled_since=filled_since,
                )
            )
            add_segment(day)

        for event in ends:
            in_milk = False
            pen = None
            not_known_from = len(segments)
            name = (event.name or "").upper()
            pen_note = f"pen not known since {name} {day.strftime('%d %b %Y')}"
            milk_note = f"{name} {day.strftime('%d %b %Y')}"
            add_segment(day)

    return segments, facts


def _as_date(value: object) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return None


def _animal_key(cow_id: object, etag: object) -> str:
    tag = ear_tag_key(str(etag) if etag else None)
    if tag and tag != "-":
        return tag
    return f"ID {str(cow_id or '').strip()}"


def _segment_deltas(
    segments: Sequence[MilkSegment],
    *,
    min_dim: int | None,
    cap: dt.date | None,
) -> dict[str, dict[dt.date, int]]:
    """Day-by-day joins and leaves for ALH and BNK. Pen 150 is left out."""
    deltas: dict[str, dict[dt.date, int]] = {"ALH": defaultdict(int), "BNK": defaultdict(int)}
    for index, segment in enumerate(segments):
        if not segment.in_milk or segment.fdat is None:
            continue
        end = segments[index + 1].start if index + 1 < len(segments) else None
        if min_dim is None:
            start = max(segment.start, segment.fdat)
        else:
            start = max(segment.start, segment.fdat + dt.timedelta(days=min_dim + 1))
        farm = shared_inventory_farm(segment.pen)
        if farm is None:
            continue
        if cap is not None:
            if start >= cap:
                continue
            if end is None or end > cap:
                end = cap
        if end is not None and start >= end:
            continue
        deltas[farm][start] += 1
        if end is not None:
            deltas[farm][end] -= 1
    return deltas


def _sweep(deltas: dict[dt.date, int], days: Sequence[dt.date]) -> dict[dt.date, int]:
    changes = sorted(deltas.items())
    running = 0
    position = 0
    out: dict[dt.date, int] = {}
    for day in days:
        while position < len(changes) and changes[position][0] <= day:
            running += changes[position][1]
            position += 1
        out[day] = max(0, running)
    return out


def _arrival_on_other_farm(
    db: Session, moved_cows: dict[str, HerdInventory]
) -> dict[str, dt.date]:
    """Map a pen-150 cow id to the latest entry date on a farm outside ALH/BNK."""
    tag_by_cow: dict[str, str] = {}
    tags: set[str] = set()
    for cow_id, row in moved_cows.items():
        tag = ear_tag_key(row.etag)
        if not tag:
            continue
        tag_by_cow[cow_id] = tag
        tags.add(tag)
    if not tags:
        return {}

    rows = db.execute(
        select(HerdInventory.etag, HerdInventory.edat).where(
            HerdInventory.farm.notin_(tuple(SHARED_HERD_FARMS)),
            HerdInventory.etag.isnot(None),
            HerdInventory.edat.isnot(None),
        )
    ).all()
    latest_by_tag: dict[str, dt.date] = {}
    for etag, edat in rows:
        tag = ear_tag_key(etag)
        if isinstance(edat, dt.datetime):
            edat = edat.date()
        if tag not in tags or not isinstance(edat, dt.date):
            continue
        current = latest_by_tag.get(tag)
        if current is None or edat > current:
            latest_by_tag[tag] = edat

    return {
        cow_id: latest_by_tag[tag]
        for cow_id, tag in tag_by_cow.items()
        if tag in latest_by_tag
    }


def _add_intervals(
    deltas: dict[dt.date, int],
    intervals: list[tuple[dt.date, dt.date | None]],
    *,
    date_min: dt.date,
    date_max: dt.date,
) -> None:
    for start, end in intervals:
        if end is not None and end <= start:
            continue
        if end is not None and end <= date_min:
            continue
        if start > date_max:
            continue
        deltas[start] += 1
        if end is not None:
            deltas[end] -= 1


def _shared_history_counts(
    db: Session,
    dates: Sequence[dt.date],
    moved_cows: dict[str, HerdInventory],
    *,
    min_dim: int | None,
) -> dict[tuple[str, dt.date], int]:
    """ALH and BNK cows in milk rebuilt from moves, for days before today."""
    if not dates:
        return {}
    date_max = max(dates)
    rows = db.execute(
        select(
            CowEvent.cow_id,
            CowEvent.etag,
            CowEvent.event,
            CowEvent.event_date,
            CowEvent.remark,
            CowEvent.fdat,
            CowEvent.lact,
            CowEvent.edat,
            CowEvent.id,
        ).where(
            CowEvent.farm.in_(tuple(SHARED_HERD_FARMS)),
            CowEvent.event_date.isnot(None),
            CowEvent.event_date <= date_max,
            CowEvent.event.in_(_PEN_HISTORY_EVENTS),
        )
    ).all()
    lactation_rows = db.execute(
        select(
            CowEvent.cow_id,
            CowEvent.etag,
            CowEvent.fdat,
            CowEvent.lact,
            func.max(CowEvent.event_date),
            func.min(CowEvent.edat),
        )
        .where(
            CowEvent.farm.in_(tuple(SHARED_HERD_FARMS)),
            CowEvent.event_date.isnot(None),
            CowEvent.event_date <= date_max,
            CowEvent.fdat.isnot(None),
            CowEvent.lact > 0,
        )
        .group_by(CowEvent.cow_id, CowEvent.etag, CowEvent.fdat, CowEvent.lact)
    ).all()

    by_cow: dict[str, list[_HistoryEvent]] = defaultdict(list)
    entry_dates: dict[str, dt.date] = {}
    seen: dict[str, set[tuple[object, ...]]] = defaultdict(set)
    fresh_dates_by_cow: dict[str, set[dt.date]] = defaultdict(set)
    for cow_id, etag, event, event_date, remark, fdat, lact, edat, event_id in rows:
        key = _animal_key(cow_id, etag)
        if key == "ID":
            continue
        event_day = _as_date(event_date)
        if event_day is None:
            continue
        entered = _as_date(edat)
        if entered is not None:
            previous = entry_dates.get(key)
            if previous is None or entered < previous:
                entry_dates[key] = entered
        try:
            lact_n = int(lact or 0)
        except (TypeError, ValueError):
            lact_n = 0
        name = (event or "").strip().upper()
        remark_text = (remark or "").strip().upper()
        fresh_day = _as_date(fdat)
        identity = (event_day, name, remark_text, lact_n, fresh_day)
        if identity in seen[key]:
            continue
        seen[key].add(identity)
        if fresh_day is not None:
            fresh_dates_by_cow[key].add(fresh_day)
        by_cow[key].append(
            _HistoryEvent(
                date=event_day,
                order=int(event_id or 0),
                name=name,
                remark=remark_text,
                fdat=fresh_day,
                lact=lact_n,
            )
        )

    # A lactation with no fresh, dry, sold, died, or move still carries its
    # fresh date on later events. Keep one row so that spell can start.
    for cow_id, etag, fdat, lact, last_date, first_edat in lactation_rows:
        key = _animal_key(cow_id, etag)
        if key == "ID":
            continue
        fresh_day = _as_date(fdat)
        last_day = _as_date(last_date)
        if fresh_day is None or last_day is None or fresh_day in fresh_dates_by_cow[key]:
            continue
        entered = _as_date(first_edat)
        if entered is not None:
            previous = entry_dates.get(key)
            if previous is None or entered < previous:
                entry_dates[key] = entered
        try:
            lact_n = int(lact or 0)
        except (TypeError, ValueError):
            lact_n = 0
        fresh_dates_by_cow[key].add(fresh_day)
        by_cow[key].append(
            _HistoryEvent(
                date=last_day,
                order=-2,
                name="LACT",
                remark="",
                fdat=fresh_day,
                lact=lact_n,
            )
        )

    arrivals = _arrival_on_other_farm(db, moved_cows) if moved_cows else {}
    caps_by_key: dict[str, dt.date] = {}
    for cow_id, row in moved_cows.items():
        arrival = arrivals.get(cow_id)
        if arrival is not None:
            caps_by_key.setdefault(_animal_key(row.cow_id, row.etag), arrival)

    combined: dict[str, dict[dt.date, int]] = {
        "ALH": defaultdict(int),
        "BNK": defaultdict(int),
    }
    for key, cow_events in by_cow.items():
        entry = entry_dates.get(key)
        segments, _facts = build_milk_segments(key, cow_events, entry)
        cap = None
        if not any(segment.pen == MOVED_PEN for segment in segments):
            cap = caps_by_key.get(key)
        part = _segment_deltas(segments, min_dim=min_dim, cap=cap)
        for farm, farm_deltas in part.items():
            for day, change in farm_deltas.items():
                combined[farm][day] += change

    result: dict[tuple[str, dt.date], int] = {}
    for farm, farm_deltas in combined.items():
        for day, count in _sweep(farm_deltas, dates).items():
            result[(farm, day)] = count
    return result


def cows_in_milk_for_dates(
    db: Session,
    farms: Iterable[str],
    dates: Iterable[dt.date],
    *,
    min_dim: int | None = None,
) -> dict[tuple[str, dt.date], int]:
    """Return ``{(farm, date): cows_in_milk}`` for the requested pairs.

    Fresh cows count from the freshen date.
    """
    farm_keys = sorted({(f or "").strip().upper() for f in farms if f})
    date_list = sorted({d for d in dates if isinstance(d, dt.date)})
    if not farm_keys or not date_list:
        return {}

    date_min = date_list[0]
    date_max = date_list[-1]
    today = dt.date.today()
    other_farms = [farm for farm in farm_keys if farm not in SHARED_HERD_FARMS]
    shared_farms = [farm for farm in farm_keys if farm in SHARED_HERD_FARMS]
    load_farms = set(other_farms)
    if shared_farms:
        load_farms |= SHARED_HERD_FARMS
    load_farm_keys = sorted(load_farms)

    events = []
    if other_farms:
        events = db.execute(
            select(
                CowEvent.farm,
                CowEvent.cow_id,
                CowEvent.event,
                CowEvent.event_date,
                CowEvent.id,
            )
            .where(
                CowEvent.farm.in_(other_farms),
                CowEvent.event.in_(_TRACKED_EVENTS),
                CowEvent.cow_id.isnot(None),
                CowEvent.event_date.isnot(None),
                CowEvent.event_date <= date_max,
            )
            .order_by(
                CowEvent.farm,
                CowEvent.cow_id,
                CowEvent.event_date,
                CowEvent.id,
            )
        ).all()

    by_farm_cow: dict[str, dict[str, list[tuple[dt.date, str]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for farm, cow_id, event, event_date, _event_id in events:
        if not cow_id or event_date is None:
            continue
        by_farm_cow[farm][str(cow_id)].append((event_date, event))

    inventory = db.scalars(
        select(HerdInventory).where(HerdInventory.farm.in_(load_farm_keys))
    ).all()
    inv_by_farm: dict[str, dict[str, HerdInventory]] = defaultdict(dict)
    moved_cows: dict[str, HerdInventory] = {}
    for row in inventory:
        if (
            row.farm in SHARED_HERD_FARMS
            and row.cow_id
            and pen_number(row.pen) == MOVED_PEN
        ):
            moved_cows.setdefault(str(row.cow_id), row)
        try:
            lact_n = float(row.lact) if row.lact is not None else 0.0
        except (TypeError, ValueError):
            lact_n = 0.0
        if lact_n <= 0 or not row.cow_id:
            continue
        inv_by_farm[row.farm][str(row.cow_id)] = row

    shared_today = {
        farm: _shared_today_count(inventory, farm, min_dim=min_dim)
        for farm in shared_farms
    }
    history_dates = [day for day in date_list if day < today]
    shared_history = (
        _shared_history_counts(db, history_dates, moved_cows, min_dim=min_dim)
        if shared_farms and history_dates
        else {}
    )

    result: dict[tuple[str, dt.date], int] = {}
    for farm in shared_farms:
        for day in date_list:
            if day >= today:
                result[(farm, day)] = shared_today[farm]
            else:
                result[(farm, day)] = shared_history.get((farm, day), 0)

    for farm in other_farms:
        inv_map = inv_by_farm.get(farm, {})
        inv_today = sum(
            1
            for row in inv_map.values()
            if _is_milking_inventory_row(row.rpro, row.lact, row.dim, min_dim=min_dim)
        )

        cow_ids = set(by_farm_cow.get(farm, {})) | set(inv_map)
        deltas: dict[dt.date, int] = defaultdict(int)
        for cow_id in cow_ids:
            inv_row = inv_map.get(cow_id)
            currently_milking = None
            fdat = None
            if inv_row is not None:
                fdat = inv_row.fdat
                currently_milking = (inv_row.rpro or "").strip().upper() != "DRY"
            _add_intervals(
                deltas,
                _cow_intervals(
                    by_farm_cow.get(farm, {}).get(cow_id, []),
                    fdat=fdat,
                    currently_milking=currently_milking,
                    today=today,
                    min_dim=min_dim,
                ),
                date_min=date_min,
                date_max=date_max,
            )

        running = 0
        for day in sorted(d for d in deltas if d < date_min):
            running += deltas[day]

        delta_days = sorted(deltas)
        di = 0
        while di < len(delta_days) and delta_days[di] < date_min:
            di += 1

        for day in date_list:
            while di < len(delta_days) and delta_days[di] <= day:
                running += deltas[delta_days[di]]
                di += 1
            if day >= today:
                result[(farm, day)] = inv_today
            else:
                result[(farm, day)] = max(0, running)

    return result
