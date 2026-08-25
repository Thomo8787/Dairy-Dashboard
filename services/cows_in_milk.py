"""Daily cows-in-milk counts from herd inventory + DairyComp events.

Inventory defines who is currently milking (LACT > 0, RPRO != DRY, and DIM > 4).
Historical days are reconstructed from FRESH / DRY / SOLD / DIED events, with
inventory freshen dates used for the current lactation. Fresh cows are only
counted once DIM > 4.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.database import CowEvent, HerdInventory

_TRACKED_EVENTS = ("FRESH", "DRY", "SOLD", "DIED")
MIN_DIM = 4


def _is_milking_inventory_row(rpro: str | None, lact: object, dim: object) -> bool:
    try:
        lact_n = float(lact) if lact is not None else 0.0
    except (TypeError, ValueError):
        lact_n = 0.0
    if lact_n <= 0:
        return False
    if (rpro or "").strip().upper() == "DRY":
        return False
    try:
        dim_n = float(dim) if dim is not None else None
    except (TypeError, ValueError):
        dim_n = None
    return dim_n is not None and dim_n > MIN_DIM


def current_cows_in_milk(db: Session, farm: str) -> int:
    """Cows in milk from the latest inventory snapshot for ``farm``."""
    farm_key = (farm or "").strip().upper()
    rows = db.execute(
        select(HerdInventory.rpro, HerdInventory.lact, HerdInventory.dim).where(
            HerdInventory.farm == farm_key
        )
    ).all()
    return sum(1 for rpro, lact, dim in rows if _is_milking_inventory_row(rpro, lact, dim))


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
) -> list[tuple[dt.date, dt.date | None]]:
    shifted: list[tuple[dt.date, dt.date | None]] = []
    offset = dt.timedelta(days=MIN_DIM + 1)
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
) -> list[tuple[dt.date, dt.date | None]]:
    base = _intervals_from_events(evs)
    if fdat is None or currently_milking is None:
        return _apply_min_dim(base)

    kept: list[tuple[dt.date, dt.date | None]] = []
    for start, end in base:
        if end is not None and end <= fdat:
            kept.append((start, end))
        elif start < fdat:
            kept.append((start, fdat))

    if currently_milking:
        kept.append((fdat, None))
        return _apply_min_dim(kept)

    dry_end = None
    for event_date, event in evs:
        if event == "DRY" and event_date >= fdat:
            dry_end = event_date
            break
    if dry_end is None:
        dry_end = today
    if fdat < dry_end:
        kept.append((fdat, dry_end))
    return _apply_min_dim(kept)


def cows_in_milk_for_dates(
    db: Session,
    farms: Iterable[str],
    dates: Iterable[dt.date],
) -> dict[tuple[str, dt.date], int]:
    """Return ``{(farm, date): cows_in_milk}`` for the requested pairs."""
    farm_keys = sorted({(f or "").strip().upper() for f in farms if f})
    date_list = sorted({d for d in dates if isinstance(d, dt.date)})
    if not farm_keys or not date_list:
        return {}

    date_min = date_list[0]
    date_max = date_list[-1]
    today = dt.date.today()

    events = db.execute(
        select(
            CowEvent.farm,
            CowEvent.cow_id,
            CowEvent.event,
            CowEvent.event_date,
            CowEvent.id,
        )
        .where(
            CowEvent.farm.in_(farm_keys),
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
        select(HerdInventory).where(HerdInventory.farm.in_(farm_keys))
    ).all()
    inv_by_farm: dict[str, dict[str, HerdInventory]] = defaultdict(dict)
    for row in inventory:
        try:
            lact_n = float(row.lact) if row.lact is not None else 0.0
        except (TypeError, ValueError):
            lact_n = 0.0
        if lact_n <= 0 or not row.cow_id:
            continue
        inv_by_farm[row.farm][str(row.cow_id)] = row

    result: dict[tuple[str, dt.date], int] = {}
    for farm in farm_keys:
        inv_map = inv_by_farm.get(farm, {})
        inv_today = sum(
            1
            for row in inv_map.values()
            if _is_milking_inventory_row(row.rpro, row.lact, row.dim)
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
            for start, end in _cow_intervals(
                by_farm_cow.get(farm, {}).get(cow_id, []),
                fdat=fdat,
                currently_milking=currently_milking,
                today=today,
            ):
                if end is not None and end <= start:
                    continue
                if end is not None and end <= date_min:
                    continue
                if start > date_max:
                    continue
                deltas[start] += 1
                if end is not None:
                    deltas[end] -= 1

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
