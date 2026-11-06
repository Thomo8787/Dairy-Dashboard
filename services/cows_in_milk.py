"""Daily cows-in-milk counts from herd inventory + DairyComp events.

Inventory defines who is currently milking (LACT > 0, RPRO != DRY, and by
default DIM > 4). Historical days are reconstructed from FRESH / DRY / SOLD /
DIED events, with inventory freshen dates used for the current lactation.

Today's Bank (BNK) count is cows in pens 89–93 on the shared ALH/BNK
inventory. Those pens are left out of today's ALH count. Pen is only on the
latest snapshot, so earlier dates still follow each farm's BNAME events.

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

_TRACKED_EVENTS = ("FRESH", "DRY", "SOLD", "DIED")
MIN_DIM = 4
SHARED_HERD_FARMS = frozenset({"ALH", "BNK"})
BANK_FARM = "BNK"
BANK_MILKING_PENS = frozenset({"89", "90", "91", "92", "93"})


def snapshot_source_farms(farms: Iterable[str]) -> set[str]:
    """Farms whose inventory rows can contribute to today's snapshot counts."""
    keys = {(farm or "").strip().upper() for farm in farms if farm}
    if keys & SHARED_HERD_FARMS:
        keys |= set(SHARED_HERD_FARMS)
    return keys


def inventory_row_in_farm_snapshot(farm: str, pen: object, row_farm: str) -> bool:
    """Whether an inventory row counts toward ``farm`` on today's snapshot.

    Bank is pens 89–93 on the shared ALH/BNK herd. ALH keeps its BNAME rows
    except those pens. Every other farm is its own inventory.
    """
    farm_key = (farm or "").strip().upper()
    source = (row_farm or "").strip().upper()
    in_bank = str(pen or "").strip() in BANK_MILKING_PENS
    if farm_key == BANK_FARM:
        return source in SHARED_HERD_FARMS and in_bank
    if farm_key == "ALH":
        return source == "ALH" and not in_bank
    return source == farm_key


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
    rows = db.execute(
        select(
            HerdInventory.farm,
            HerdInventory.pen,
            HerdInventory.rpro,
            HerdInventory.lact,
            HerdInventory.dim,
        ).where(HerdInventory.farm.in_(sorted(snapshot_source_farms([farm_key]))))
    ).all()
    return sum(
        1
        for row_farm, pen, rpro, lact, dim in rows
        if inventory_row_in_farm_snapshot(farm_key, pen, row_farm)
        and _is_milking_inventory_row(rpro, lact, dim, min_dim=min_dim)
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
        select(HerdInventory).where(
            HerdInventory.farm.in_(sorted(snapshot_source_farms(farm_keys)))
        )
    ).all()
    inv_by_farm: dict[str, dict[str, HerdInventory]] = defaultdict(dict)
    for row in inventory:
        if row.farm not in farm_keys or not row.cow_id:
            continue
        try:
            lact_n = float(row.lact) if row.lact is not None else 0.0
        except (TypeError, ValueError):
            lact_n = 0.0
        if lact_n <= 0:
            continue
        inv_by_farm[row.farm][str(row.cow_id)] = row

    result: dict[tuple[str, dt.date], int] = {}
    for farm in farm_keys:
        inv_map = inv_by_farm.get(farm, {})
        inv_today = sum(
            1
            for row in inventory
            if inventory_row_in_farm_snapshot(farm, row.pen, row.farm)
            and _is_milking_inventory_row(row.rpro, row.lact, row.dim, min_dim=min_dim)
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
                min_dim=min_dim,
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
