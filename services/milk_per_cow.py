"""Daily milk-per-cow table for the Milk Quality page.

One row per farm and day. Total milk is the sum of ticket litres. Fat %,
protein %, SCC, Bactoscan, and urea are litre-weighted when there is more
than one load. The 4-day average is the mean of this farm's last up to four
days with milk (including today), and milk per cow is that average divided
by cows in milk on that day.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from services.farms import HERD_FARM_OPTIONS
from services.nml_results import list_nml_results

_FOUR_DAY_WINDOW = 4
_LOOKBACK_PAD_DAYS = _FOUR_DAY_WINDOW - 1


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


def _parse_day(value: Any) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _four_day_average(history: list[float]) -> float | None:
    """Mean of the last up to four daily totals (newest last)."""
    recent = history[-_FOUR_DAY_WINDOW:]
    if not recent:
        return None
    return round(sum(recent) / len(recent), 0)


def list_milk_per_cow(
    *,
    farms: list[str] | None = None,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
) -> dict[str, Any]:
    """Return one daily summary row per farm, newest date first."""
    load_from = date_from - dt.timedelta(days=_LOOKBACK_PAD_DAYS) if date_from else None
    payload = list_nml_results(farms=farms, date_from=load_from, date_to=date_to)
    trend = payload.get("trend") or {}
    selected = [code.upper() for code in (farms or list(HERD_FARM_OPTIONS)) if code]
    if not selected:
        selected = list(HERD_FARM_OPTIONS)

    rows: list[dict[str, Any]] = []
    for farm in selected:
        points = sorted(
            (point for point in (trend.get(farm) or []) if point.get("date")),
            key=lambda point: str(point["date"]),
        )
        milk_history: list[float] = []
        for point in points:
            day = _parse_day(point.get("date"))
            if day is None:
                continue
            total_milk = _round_or_none(point.get("volume_litres") or point.get("litres"), 0)
            if total_milk is None or float(total_milk) <= 0:
                continue
            milk_history.append(float(total_milk))
            four_day_avg = _four_day_average(milk_history)
            if date_from and day < date_from:
                continue
            if date_to and day > date_to:
                continue
            cows = _round_or_none(point.get("cows_in_milk"), 0)
            milk_per_cow = None
            if four_day_avg is not None and cows is not None and cows > 0:
                milk_per_cow = round(float(four_day_avg) / float(cows), 1)
            rows.append(
                {
                    "farm": farm,
                    "sample_date": day.isoformat(),
                    "total_milk": total_milk,
                    "four_day_avg": four_day_avg,
                    "total_cows": cows,
                    "milk_per_cow": milk_per_cow,
                    "butterfat_pct": _round_or_none(point.get("butterfat_pct"), 2),
                    "protein_pct": _round_or_none(point.get("protein_pct"), 2),
                    "scc": _round_or_none(point.get("scc"), 0),
                    "bactoscan": _round_or_none(point.get("bactoscan"), 0),
                    "urea_pct": _round_or_none(point.get("urea_pct"), 3),
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
