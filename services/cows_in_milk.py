"""Daily cows-in-milk counts from herd inventory + DairyComp events.

Inventory defines who is currently milking (LACT > 0, RPRO != DRY, and by
default DIM > 4). Historical days are reconstructed from FRESH / DRY / SOLD /
DIED events, with inventory freshen dates used for the current lactation.

ALH and BNK share one herd. Today's count uses the current pen: BNK is pens
89 to 93, ALH is every other pen, and pen 150 is left out. Earlier days stay
on the freshen / dry / sold / died timeline for the farm stored on the event.
A cow now in pen 150 still counts on ALH up to the day that ear tag arrives
on another farm.

Pass ``min_dim=None`` to include fresh cows from the freshen date (used by
Milking Efficiency ID Efficiency). Collections/Home keep the default DIM > 4.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.database import CowEvent, HerdInventory
from services.duplicate_animals import ear_tag_key

_TRACKED_EVENTS = ("FRESH", "DRY", "SOLD", "DIED")
MIN_DIM = 4
SHARED_HERD_FARMS = frozenset({"ALH", "BNK"})
BNK_MILK_PENS = frozenset(range(89, 94))
MOVED_PEN = 150


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
    min_dim: int | None = MIN_DIM,
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
    min_dim: int | None = MIN_DIM,
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
    min_dim: int | None = MIN_DIM,
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
    min_dim: int | None = MIN_DIM,
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


def _cap_intervals(
    intervals: list[tuple[dt.date, dt.date | None]],
    cap: dt.date | None,
) -> list[tuple[dt.date, dt.date | None]]:
    """Stop each interval on ``cap``. That day is not counted."""
    if cap is None:
        return intervals
    capped: list[tuple[dt.date, dt.date | None]] = []
    for start, end in intervals:
        if start >= cap:
            continue
        if end is None or end > cap:
            end = cap
        if end <= start:
            continue
        capped.append((start, end))
    return capped


def _open_lactation(row: HerdInventory) -> tuple[dt.date | None, bool | None]:
    """Freshen date and milking flag used for the current lactation.

    Heifers (LACT <= 0) contribute only through their events, matching the
    historical reconstruction for every other cow.
    """
    try:
        lact_n = float(row.lact) if row.lact is not None else 0.0
    except (TypeError, ValueError):
        lact_n = 0.0
    if lact_n <= 0:
        return None, None
    return row.fdat, (row.rpro or "").strip().upper() != "DRY"


def _events_for_inventory_cow(
    row: HerdInventory,
    by_farm_cow: dict[str, dict[str, list[tuple[dt.date, str]]]],
) -> list[tuple[dt.date, str]]:
    """Events for a shared-herd cow, preferring the farm she is stored on."""
    cow_id = str(row.cow_id or "")
    stored = row.farm if row.farm in SHARED_HERD_FARMS else "ALH"
    events = by_farm_cow.get(stored, {}).get(cow_id, [])
    if events:
        return events
    for farm in SHARED_HERD_FARMS:
        if farm == stored:
            continue
        other = by_farm_cow.get(farm, {}).get(cow_id, [])
        if other:
            return other
    return []


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


def cows_in_milk_for_dates(
    db: Session,
    farms: Iterable[str],
    dates: Iterable[dt.date],
    *,
    min_dim: int | None = MIN_DIM,
) -> dict[tuple[str, dt.date], int]:
    """Return ``{(farm, date): cows_in_milk}`` for the requested pairs.

    ``min_dim=None`` includes fresh cows from the freshen date (no DIM delay).
    """
    farm_keys = sorted({(f or "").strip().upper() for f in farms if f})
    date_list = sorted({d for d in dates if isinstance(d, dt.date)})
    if not farm_keys or not date_list:
        return {}

    date_min = date_list[0]
    date_max = date_list[-1]
    today = dt.date.today()
    load_farms = set(farm_keys)
    if load_farms & SHARED_HERD_FARMS:
        load_farms |= SHARED_HERD_FARMS
    load_farm_keys = sorted(load_farms)

    events = db.execute(
        select(
            CowEvent.farm,
            CowEvent.cow_id,
            CowEvent.event,
            CowEvent.event_date,
            CowEvent.id,
        )
        .where(
            CowEvent.farm.in_(load_farm_keys),
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

    moved_on = _arrival_on_other_farm(db, moved_cows) if moved_cows else {}
    shared_today = {
        farm: _shared_today_count(inventory, farm, min_dim=min_dim)
        for farm in SHARED_HERD_FARMS
        if farm in farm_keys
    }

    result: dict[tuple[str, dt.date], int] = {}
    for farm in farm_keys:
        inv_map = inv_by_farm.get(farm, {})
        if farm in SHARED_HERD_FARMS:
            inv_today = shared_today[farm]
        else:
            inv_today = sum(
                1
                for row in inv_map.values()
                if _is_milking_inventory_row(row.rpro, row.lact, row.dim, min_dim=min_dim)
            )

        cow_ids = set(by_farm_cow.get(farm, {})) | set(inv_map)
        deltas: dict[dt.date, int] = defaultdict(int)
        for cow_id in cow_ids:
            if farm in SHARED_HERD_FARMS and cow_id in moved_cows:
                continue
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

        if farm == "ALH":
            for cow_id, moved in moved_cows.items():
                fdat, currently_milking = _open_lactation(moved)
                _add_intervals(
                    deltas,
                    _cap_intervals(
                        _cow_intervals(
                            _events_for_inventory_cow(moved, by_farm_cow),
                            fdat=fdat,
                            currently_milking=currently_milking,
                            today=today,
                            min_dim=min_dim,
                        ),
                        moved_on.get(cow_id),
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
