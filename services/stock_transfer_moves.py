"""One move date for an animal that changes DairyComp, and one first-lactation fresh.

The farm she leaves records SOLD and the farm she joins records an entry date
(EDAT). When the two dates sit either side of a month end she is in both counts,
or in neither. Stock Accruals uses one date for both farms:

- She leaves ALH or BNK with a MOVE into pen 27 (Park Hall), pen 140 (The
  Parkes) or pen 150 (any other farm) from 30 days before to 14 days after the sale and entry dates:
  the date of that move.
- She joins within 14 days after the sale: the sold date.
- She joins before the sale and the farm she left still records events of its
  own after her entry date: the sold date, so the purchase moves later.
- She joins before the sale and the farm she left records nothing of its own
  after her entry date: the entry date, so the sale moves earlier.

An entry before the sale only counts as her move when the farm she joined still
has her after the sale. Events copied across with her history (same event on
the same day on both farms) do not count as the leaving farm's own.

A first-lactation fresh on both farms within 7 days counts once, on the farm she
left. The farm she joined then books her as a cow purchase.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.database import STOCK_GROUP_COWS, STOCK_GROUP_YOUNGSTOCK, CowEvent, HerdInventory
from services.farms import FARMS_BY_CODE

GAP_DAYS = 14
FRESH_MATCH_DAYS = 7
LEAVING_PEN_DAYS_BEFORE = 30
LEAVING_PEN_DAYS_AFTER = 14
ADJUST_SALE_MOVED = "sale_moved"
ADJUST_FRESH_IGNORED = "fresh_ignored"
_EXIT_EVENTS = ("SOLD", "DIED")
_LEAVING_PEN_FARMS = frozenset({"ALH", "BNK"})
_MOVE_TO_LEAVING_PEN = re.compile(r"^F\d{3}T(027|140|150)$")
_CHUNK = 500


def etag_key(etag: str | None) -> str:
    return re.sub(r"\s+", "", etag or "").upper()


def _dairy_group(farm: str) -> str:
    config = FARMS_BY_CODE.get(farm)
    return config.dairy_data_group if config else farm


def _fetch_cross_group_events(db: Session) -> dict[str, list[dict[str, Any]]]:
    """Events for ear tags seen in more than one DairyComp."""
    groups_by_key: dict[str, set[str]] = defaultdict(set)
    etags_by_key: dict[str, set[str]] = defaultdict(set)
    for farm, etag in db.execute(
        select(CowEvent.farm, CowEvent.etag).where(CowEvent.etag.isnot(None)).distinct()
    ).all():
        key = etag_key(etag)
        if not key:
            continue
        groups_by_key[key].add(_dairy_group(str(farm)))
        etags_by_key[key].add(etag)

    etags = sorted(
        etag
        for key, groups in groups_by_key.items()
        if len(groups) > 1
        for etag in etags_by_key[key]
    )
    events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for start in range(0, len(etags), _CHUNK):
        rows = db.execute(
            select(
                CowEvent.id,
                CowEvent.farm,
                CowEvent.etag,
                CowEvent.event,
                CowEvent.event_date,
                CowEvent.lact,
                CowEvent.edat,
                CowEvent.remark,
            ).where(CowEvent.etag.in_(etags[start : start + _CHUNK]))
        ).mappings()
        for row in rows:
            events[etag_key(row["etag"])].append(dict(row))
    return events


def _fetch_inventory_keys(db: Session) -> set[tuple[str, str]]:
    return {
        (str(farm), etag_key(etag))
        for farm, etag in db.execute(
            select(HerdInventory.farm, HerdInventory.etag).where(HerdInventory.etag.isnot(None))
        ).all()
    }


def _transfer_candidates(
    key: str,
    by_farm: dict[str, list[dict[str, Any]]],
    purchase_by_farm_key: dict[tuple[str, str], dict[str, Any]],
    inventory_keys: set[tuple[str, str]],
) -> list[tuple[int, dict[str, Any], str, str, dict[str, Any]]]:
    candidates = []
    for to_farm, to_events in by_farm.items():
        purchase = purchase_by_farm_key.get((to_farm, key))
        if purchase is None:
            continue
        entry: dt.date = purchase["edat"]
        on_inventory = (to_farm, key) in inventory_keys
        to_last = max((e["event_date"] for e in to_events if e["event_date"]), default=None)
        for from_farm, from_events in by_farm.items():
            if _dairy_group(from_farm) == _dairy_group(to_farm):
                continue
            sales = sorted(
                (e for e in from_events if e["event"] == "SOLD" and e["event_date"]),
                key=lambda e: e["event_date"],
            )
            for sale in sales:
                sold: dt.date = sale["event_date"]
                if entry < sold:
                    # A sale elsewhere is not her move unless the farm she joined still has her.
                    if on_inventory or (to_last is not None and to_last >= sold):
                        candidates.append(
                            ((sold - entry).days, sale, from_farm, to_farm, purchase)
                        )
                    break
                if (entry - sold).days <= GAP_DAYS:
                    candidates.append(((entry - sold).days, sale, from_farm, to_farm, purchase))
                    break
    candidates.sort(key=lambda item: item[0])
    return candidates


def _leaving_pen_date(
    from_farm: str, from_events: list[dict[str, Any]], entry: dt.date, sold: dt.date
) -> dt.date | None:
    """The day ALH or BNK moved her into pen 27, 140 or 150, nearest her entry date."""
    if from_farm not in _LEAVING_PEN_FARMS:
        return None
    earliest = min(entry, sold) - dt.timedelta(days=LEAVING_PEN_DAYS_BEFORE)
    latest = max(entry, sold) + dt.timedelta(days=LEAVING_PEN_DAYS_AFTER)
    moves = [
        e["event_date"]
        for e in from_events
        if e["event"] == "MOVE"
        and e["event_date"] is not None
        and earliest <= e["event_date"] <= latest
        and _MOVE_TO_LEAVING_PEN.match((e.get("remark") or "").strip().upper())
    ]
    if not moves:
        return None
    return min(moves, key=lambda day: (abs((day - entry).days), day))


def _still_on_leaving_farm(
    from_events: list[dict[str, Any]],
    to_events: list[dict[str, Any]],
    entry: dt.date,
    sold: dt.date,
) -> bool:
    copied = {(e["event"], e["event_date"]) for e in to_events}
    return any(
        e["event_date"] is not None
        and entry < e["event_date"] <= sold
        and e["event"] not in _EXIT_EVENTS
        and (e["event"], e["event_date"]) not in copied
        for e in from_events
    )


def _arriving_farm(
    farm_a: str,
    farm_b: str,
    key: str,
    transfers: set[tuple[str, str]],
    purchase_by_farm_key: dict[tuple[str, str], dict[str, Any]],
) -> str | None:
    if (farm_a, farm_b) in transfers:
        return farm_b
    if (farm_b, farm_a) in transfers:
        return farm_a
    purchase_a = purchase_by_farm_key.get((farm_a, key))
    purchase_b = purchase_by_farm_key.get((farm_b, key))
    if purchase_a and not purchase_b:
        return farm_a
    if purchase_b and not purchase_a:
        return farm_b
    if purchase_a and purchase_b and purchase_a["edat"] != purchase_b["edat"]:
        return farm_a if purchase_a["edat"] > purchase_b["edat"] else farm_b
    return None


def derive_transfer_adjustments(
    db: Session, purchases: list[dict[str, Any]], import_time: dt.datetime
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Set accrual_date (and stock group) on purchase mappings; return event adjustments."""
    purchase_by_farm_key: dict[tuple[str, str], dict[str, Any]] = {}
    for purchase in purchases:
        purchase["accrual_date"] = purchase["edat"]
        purchase_by_farm_key[(purchase["farm"], etag_key(purchase["etag"]))] = purchase

    adjustments: list[dict[str, Any]] = []
    summary = {"sales_moved": 0, "purchases_moved": 0, "freshes_ignored": 0}
    inventory_keys = _fetch_inventory_keys(db)

    for key, events in _fetch_cross_group_events(db).items():
        by_farm: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            by_farm[str(event["farm"])].append(event)

        transfers: set[tuple[str, str]] = set()
        used_sales: set[int] = set()
        used_purchases: set[int] = set()
        for _days, sale, from_farm, to_farm, purchase in _transfer_candidates(
            key, by_farm, purchase_by_farm_key, inventory_keys
        ):
            if sale["id"] in used_sales or id(purchase) in used_purchases:
                continue
            used_sales.add(sale["id"])
            used_purchases.add(id(purchase))
            transfers.add((from_farm, to_farm))
            entry = purchase["edat"]
            sold = sale["event_date"]
            moved_out = _leaving_pen_date(from_farm, by_farm[from_farm], entry, sold)
            if moved_out is not None:
                if moved_out != entry:
                    purchase["accrual_date"] = moved_out
                    summary["purchases_moved"] += 1
                if moved_out != sold:
                    adjustments.append(
                        {
                            "cow_event_id": sale["id"],
                            "kind": ADJUST_SALE_MOVED,
                            "farm": from_farm,
                            "other_farm": to_farm,
                            "etag": sale["etag"],
                            "event_date": sold,
                            "accrual_date": moved_out,
                            "import_timestamp": import_time,
                        }
                    )
                    summary["sales_moved"] += 1
                continue
            if entry == sold:
                continue
            if entry > sold or _still_on_leaving_farm(
                by_farm[from_farm], by_farm[to_farm], entry, sold
            ):
                purchase["accrual_date"] = sold
                summary["purchases_moved"] += 1
                continue
            adjustments.append(
                {
                    "cow_event_id": sale["id"],
                    "kind": ADJUST_SALE_MOVED,
                    "farm": from_farm,
                    "other_farm": to_farm,
                    "etag": sale["etag"],
                    "event_date": sold,
                    "accrual_date": entry,
                    "import_timestamp": import_time,
                }
            )
            summary["sales_moved"] += 1

        freshes = sorted(
            (e for e in events if e["event"] == "FRESH" and e["lact"] == 1 and e["event_date"]),
            key=lambda e: e["event_date"],
        )
        ignored: set[int] = set()
        for index, first in enumerate(freshes):
            for second in freshes[index + 1 :]:
                if (second["event_date"] - first["event_date"]).days > FRESH_MATCH_DAYS:
                    break
                farm_a, farm_b = str(first["farm"]), str(second["farm"])
                if _dairy_group(farm_a) == _dairy_group(farm_b):
                    continue
                arriving = _arriving_farm(farm_a, farm_b, key, transfers, purchase_by_farm_key)
                if arriving is None:
                    continue
                if any(e["lact"] == 0 for e in by_farm[arriving]):
                    continue
                dropped = first if arriving == farm_a else second
                kept = second if dropped is first else first
                if dropped["id"] in ignored or kept["id"] in ignored:
                    continue
                ignored.add(dropped["id"])
                purchase = purchase_by_farm_key.get((arriving, key))
                if purchase is not None and purchase["stock_group"] == STOCK_GROUP_YOUNGSTOCK:
                    purchase["stock_group"] = STOCK_GROUP_COWS
                adjustments.append(
                    {
                        "cow_event_id": dropped["id"],
                        "kind": ADJUST_FRESH_IGNORED,
                        "farm": arriving,
                        "other_farm": str(kept["farm"]),
                        "etag": dropped["etag"],
                        "event_date": dropped["event_date"],
                        "accrual_date": None,
                        "import_timestamp": import_time,
                    }
                )
                summary["freshes_ignored"] += 1

    return adjustments, summary
