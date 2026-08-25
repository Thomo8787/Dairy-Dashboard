"""Home-dashboard production averages (7-day / 30d) per farm.

Windows end yesterday (UK), so today's incomplete tanker volumes are excluded.
The headline figure is a plain 7-day calendar mean (yesterday and the six days
before). Daily quality points are litre-weighted from tanker loads. Days in the
window with no figure are skipped.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

from services.farms import HERD_FARM_OPTIONS
from services.nml_results import list_nml_results

_UK = ZoneInfo("Europe/London")

_LOOKBACK_PAD_DAYS = 45
_SHORT_WINDOW_DAYS = 7
_LONG_WINDOW_DAYS = 30


def _uk_today() -> dt.date:
    return dt.datetime.now(_UK).date()


def _parse_day(value: str | dt.date | None) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _mean(values: list[float], dp: int) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), dp)


def _metric_window(
    points: list[dict[str, Any]],
    *,
    key: str,
    days: int,
    dp: int,
    window_end: dt.date,
    as_int: bool = False,
) -> dict[str, Any]:
    """Mean of ``key`` over ``days`` calendar days ending on ``window_end``."""
    start = window_end - dt.timedelta(days=days - 1)
    values = [
        float(point[key])
        for point in points
        if point.get(key) is not None
        and (day := _parse_day(point.get("date"))) is not None
        and start <= day <= window_end
    ]
    if as_int:
        value: float | int | None = round(sum(values) / len(values)) if values else None
    else:
        value = _mean(values, dp)
    return {
        "days": days,
        "from": start.isoformat(),
        "to": window_end.isoformat(),
        "days_with_data": len(values),
        "value": value,
        "window_end": window_end.isoformat(),
    }


def _bundle_for_days(
    points: list[dict[str, Any]],
    days: int,
    *,
    window_end: dt.date,
) -> dict[str, Any]:
    def metric_fn(pts, *, key, dp, as_int=False):
        return _metric_window(
            pts,
            key=key,
            days=days,
            dp=dp,
            window_end=window_end,
            as_int=as_int,
        )

    volume = metric_fn(points, key="volume_litres", dp=0, as_int=True)
    per_cow = metric_fn(points, key="litres_per_cow", dp=1)
    fat = metric_fn(points, key="butterfat_pct", dp=2)
    protein = metric_fn(points, key="protein_pct", dp=2)
    bactoscan = metric_fn(points, key="bactoscan", dp=0, as_int=True)
    scc = metric_fn(points, key="scc", dp=0, as_int=True)
    temp = metric_fn(points, key="temp_c", dp=1)
    return {
        "days": days,
        "from": volume["from"],
        "to": volume["to"],
        "days_with_volume": volume["days_with_data"],
        "window_end": volume["window_end"],
        "milk_per_cow": per_cow["value"],
        "milk_per_day": volume["value"],
        "butterfat_pct": fat["value"],
        "protein_pct": protein["value"],
        "bactoscan": bactoscan["value"],
        "scc": scc["value"],
        "milk_temp": temp["value"],
        "windows": {
            "milk_per_day": {
                "from": volume["from"],
                "to": volume["to"],
                "days_with_data": volume["days_with_data"],
            },
            "milk_per_cow": {
                "from": per_cow["from"],
                "to": per_cow["to"],
                "days_with_data": per_cow["days_with_data"],
            },
            "butterfat_pct": {
                "from": fat["from"],
                "to": fat["to"],
                "days_with_data": fat["days_with_data"],
            },
            "protein_pct": {
                "from": protein["from"],
                "to": protein["to"],
                "days_with_data": protein["days_with_data"],
            },
            "bactoscan": {
                "from": bactoscan["from"],
                "to": bactoscan["to"],
                "days_with_data": bactoscan["days_with_data"],
            },
            "scc": {
                "from": scc["from"],
                "to": scc["to"],
                "days_with_data": scc["days_with_data"],
            },
            "milk_temp": {
                "from": temp["from"],
                "to": temp["to"],
                "days_with_data": temp["days_with_data"],
            },
        },
    }


def _annotate_volume_and_cows(trend: dict[str, list[dict[str, Any]]]) -> None:
    for farm_points in trend.values():
        for point in farm_points:
            litres = point.get("litres") or point.get("volume_litres")
            point["volume_litres"] = litres if litres else None
            if point.get("litres_per_cow") is not None:
                continue
            cows = point.get("cows_in_milk")
            if litres and cows and cows > 0:
                point["litres_per_cow"] = round(float(litres) / cows, 2)
            else:
                point["litres_per_cow"] = None


def get_production_summary(*, as_of: dt.date | None = None) -> dict[str, Any]:
    """Return per-farm 7-day / 30-day production averages ending yesterday."""
    today = as_of or _uk_today()
    completed_through = today - dt.timedelta(days=1)
    date_from = completed_through - dt.timedelta(days=_LOOKBACK_PAD_DAYS)
    payload = list_nml_results(
        farms=list(HERD_FARM_OPTIONS),
        date_from=date_from,
        date_to=completed_through,
    )
    trend = payload.get("trend") or {}
    _annotate_volume_and_cows(trend)

    farms_out: list[dict[str, Any]] = []
    for farm in HERD_FARM_OPTIONS:
        points = list(trend.get(farm) or [])
        d7 = _bundle_for_days(points, _SHORT_WINDOW_DAYS, window_end=completed_through)
        d30 = _bundle_for_days(points, _LONG_WINDOW_DAYS, window_end=completed_through)
        farms_out.append(
            {
                "farm": farm,
                "window_end": completed_through.isoformat(),
                "d7": d7,
                "d30": d30,
            }
        )

    return {
        "as_of": today.isoformat(),
        "completed_through": completed_through.isoformat(),
        "href": "/milk-quality/collections",
        "farms": farms_out,
    }
