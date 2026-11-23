"""Daily milk-per-cow table for the Milk Quality page.

One row per farm and day. Total milk is the sum of ticket litres. Fat %,
protein %, and SCC are litre-weighted when there is more than one load.
Milk per cow is total milk divided by cows in milk for that day.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from services.farms import HERD_FARM_OPTIONS
from services.nml_results import list_nml_results


def _round_or_none(value: Any, digits: int) -> float | int | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if digits <= 0:
        return int(round(number))
    return round(number, digits)


def list_milk_per_cow(
    *,
    farms: list[str] | None = None,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
) -> dict[str, Any]:
    """Return one daily summary row per farm, newest date first."""
    payload = list_nml_results(farms=farms, date_from=date_from, date_to=date_to)
    trend = payload.get("trend") or {}
    selected = [code.upper() for code in (farms or list(HERD_FARM_OPTIONS)) if code]
    if not selected:
        selected = list(HERD_FARM_OPTIONS)

    rows: list[dict[str, Any]] = []
    for farm in selected:
        for point in trend.get(farm) or []:
            day = point.get("date")
            if not day:
                continue
            total_milk = _round_or_none(point.get("volume_litres") or point.get("litres"), 0)
            cows = _round_or_none(point.get("cows_in_milk"), 0)
            milk_per_cow = point.get("litres_per_cow")
            if milk_per_cow is None and total_milk and cows and cows > 0:
                milk_per_cow = round(float(total_milk) / float(cows), 1)
            else:
                milk_per_cow = _round_or_none(milk_per_cow, 1)
            rows.append(
                {
                    "farm": farm,
                    "sample_date": day,
                    "milk_per_cow": milk_per_cow,
                    "total_cows": cows,
                    "total_milk": total_milk,
                    "butterfat_pct": _round_or_none(point.get("butterfat_pct"), 2),
                    "protein_pct": _round_or_none(point.get("protein_pct"), 2),
                    "scc": _round_or_none(point.get("scc"), 0),
                    "collection_count": int(point.get("collection_count") or 0),
                }
            )

    rows.sort(key=lambda row: (row["sample_date"], row["farm"]), reverse=True)
    return {
        "rows": rows,
        "total": len(rows),
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
    }
