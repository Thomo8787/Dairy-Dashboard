"""Why a Home production tile is red.

Thresholds match the tile colours in templates/home.html. Comments use the
daily totals and the individual loads behind those averages: a missing
collection, a light day, or one score that is nothing like the rest of the week.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from typing import Any

from services.production_summary import (
    _LONG_WINDOW_DAYS,
    _SHORT_WINDOW_DAYS,
    _TWO_COLLECTION_FARMS,
    _counts_for_milk_per_cow,
    _daily_metric_value,
    _parse_day,
    _single_load_is_full_day,
)

_MILK_DROP = 0.045
_SCC_LEVEL = 180
_SCC_RISE = 0.30
_FAT_LOW = 3.6
_BACTO_HIGH = 30
_TEMP_HIGH = 4.0
# A day this far under the usual volume, with the usual number of loads, looks small.
_LIGHT_VOLUME = 0.80
# One load this far under a two-load day is a missing collection, not a full tanker.
_MISSING_VOLUME = 0.70

_LABELS = {
    "milk_per_cow": "Milk / cow",
    "butterfat_pct": "Fat %",
    "bactoscan": "BactoScan",
    "scc": "SCC",
    "milk_temp": "Milk temp",
    "weighbridge": "Weighbridge",
    "collection": "Collection",
}

# ALH may be up to 1,000 L apart. Every other farm has to match exactly.
_WEIGHBRIDGE_TOLERANCE_L = 1000
_PARKES_FARM = "PRK"
_PARKES_DROP = 0.10


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _half_up(value: float, dp: int) -> float:
    factor = 10**dp
    return math.floor(value * factor + 0.5) / factor


def _fmt_num(value: float, dp: int) -> str:
    rounded = int(round(value)) if dp <= 0 else _half_up(value, dp)
    if dp <= 0:
        return f"{int(rounded):,}"
    return f"{rounded:,.{dp}f}"


def _fmt_litres(value: float) -> str:
    return f"{int(round(value)):,}"


def _fmt_date(day: dt.date) -> str:
    return f"{day.strftime('%a')} {day.day} {day.strftime('%b')}"


def _join_and(parts: list[str]) -> str:
    parts = list(parts)
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _fmt_range(values: list[float], dp: int) -> str:
    shown = [_fmt_num(value, dp) for value in values]
    lo = min(shown, key=lambda text: float(text.replace(",", "")))
    hi = max(shown, key=lambda text: float(text.replace(",", "")))
    if lo == hi:
        return lo
    return f"{lo}–{hi}"


def _load_word(count: int) -> str:
    names = {0: "no loads", 1: "one load", 2: "two loads"}
    return names.get(count, f"{count} loads")


def _window_start(window_end: dt.date, days: int) -> dt.date:
    return window_end - dt.timedelta(days=days - 1)


def _points_between(
    points: list[dict[str, Any]],
    start: dt.date,
    end: dt.date,
) -> list[dict[str, Any]]:
    chosen: list[tuple[dt.date, dict[str, Any]]] = []
    for point in points:
        day = _parse_day(point.get("date"))
        if day is None or day < start or day > end:
            continue
        chosen.append((day, point))
    chosen.sort(key=lambda item: item[0])
    return [point for _day, point in chosen]


def _point_day(point: dict[str, Any]) -> dt.date | None:
    return _parse_day(point.get("date"))


def _point_volume(point: dict[str, Any]) -> float | None:
    volume = _num(point.get("volume_litres"))
    if volume is None or volume <= 0:
        return None
    return volume


def _point_count(point: dict[str, Any]) -> int:
    try:
        return int(point.get("collection_count") or 0)
    except (TypeError, ValueError):
        return 0


def _calendar_values(
    points: list[dict[str, Any]],
    *,
    window_end: dt.date,
    days: int,
    key: str,
    dp: int,
) -> list[tuple[dt.date, float | None]]:
    start = _window_start(window_end, days)
    by_date: dict[dt.date, dict[str, Any]] = {}
    for point in points:
        day = _point_day(point)
        if day is not None:
            by_date[day] = point
    series: list[tuple[dt.date, float | None]] = []
    for offset in range(days):
        day = start + dt.timedelta(days=offset)
        value = _daily_metric_value(by_date.get(day), key, dp=dp)
        series.append((day, None if value is None else float(value)))
    return series


def _milk_per_cow_red(recent: Any, baseline: Any) -> bool:
    d7 = _num(recent)
    d30 = _num(baseline)
    if d7 is None or d30 is None or d30 <= 0:
        return False
    return (d30 - d7) / d30 > _MILK_DROP


def _scc_red(recent: Any, baseline: Any) -> bool:
    d7 = _num(recent)
    d30 = _num(baseline)
    if d7 is None or d30 is None or d30 <= 0:
        return False
    return d7 > _SCC_LEVEL and (d7 - d30) / d30 > _SCC_RISE


def _fat_red(recent: Any) -> bool:
    d7 = _num(recent)
    if d7 is None:
        return False
    return d7 <= _FAT_LOW


def _bactoscan_red(average: Any, daily_values: list[float | None]) -> bool:
    avg = _num(average)
    if avg is not None and avg >= _BACTO_HIGH:
        return True
    recent = list(daily_values)[-3:]
    if len(recent) < 3:
        return False
    return all(value is not None and value >= _BACTO_HIGH for value in recent)


def _temp_red(daily_values: list[float | None]) -> bool:
    recent = list(daily_values)[-4:]
    if len(recent) < 4:
        return False
    temps: list[float] = []
    for value in recent:
        if value is None:
            return False
        temps.append(_half_up(value, 1))
    rising = all(temps[index] > temps[index - 1] for index in range(1, len(temps)))
    return rising and temps[-1] >= _TEMP_HIGH


def _cow_is_down(point: dict[str, Any], baseline: Any) -> bool:
    value = _num(point.get("litres_per_cow"))
    base = _num(baseline)
    if value is None or base is None or base <= 0:
        return False
    return (base - value) / base > _MILK_DROP


def _usual_volume(points: list[dict[str, Any]]) -> float | None:
    volumes = [volume for point in points if (volume := _point_volume(point)) is not None]
    if not volumes:
        return None
    return float(statistics.median(volumes))


def _usual_count(points: list[dict[str, Any]]) -> int:
    counts = [_point_count(point) for point in points if _point_volume(point) is not None]
    counts = [count for count in counts if count > 0]
    if not counts:
        return 0
    tally: dict[int, int] = {}
    for count in counts:
        tally[count] = tally.get(count, 0) + 1
    return max(tally, key=lambda count: (tally[count], count))


def _is_missing_load(
    point: dict[str, Any],
    usual_count: int,
    daily_average: float | None,
) -> bool:
    """One load where this farm normally has two, and it is not a full day's volume."""
    if usual_count < 2:
        return False
    count = _point_count(point)
    if count <= 0 or count >= usual_count:
        return False
    return not _single_load_is_full_day(point, daily_average)


def _is_light_day(point: dict[str, Any], usual_volume: float | None, usual_count: int) -> bool:
    volume = _point_volume(point)
    if volume is None or usual_volume is None or usual_volume <= 0:
        return False
    if _point_count(point) < usual_count:
        return False
    return volume <= usual_volume * _LIGHT_VOLUME


def _day_count_phrase(count: int, total: int) -> str:
    if count == total and total == 7:
        return "all of the last 7 days"
    if count == total:
        return f"all {total} days in this figure"
    if total == 7:
        return f"{count} of the last 7 days"
    return f"{count} of the {total} days in this figure"


def _milk_per_cow_comment(
    farm: str,
    points: list[dict[str, Any]],
    *,
    window_end: dt.date,
    baseline: Any,
    daily_average: float | None,
    two_collections: bool,
) -> str:
    start7 = _window_start(window_end, _SHORT_WINDOW_DAYS)
    days7 = _points_between(points, start7, window_end)
    days30 = _points_between(points, _window_start(window_end, _LONG_WINDOW_DAYS), window_end)
    usual_volume = _usual_volume(days30)
    usual_count = _usual_count(days30)
    if two_collections:
        counted = [point for point in days7 if _counts_for_milk_per_cow(point, daily_average)]
    else:
        counted = [point for point in days7 if _num(point.get("litres_per_cow")) is not None]

    missing = [point for point in days7 if _is_missing_load(point, usual_count, daily_average)]
    missing_down = [point for point in missing if point in counted and _cow_is_down(point, baseline)]
    light = [
        point
        for point in counted
        if point not in missing and _is_light_day(point, usual_volume, usual_count) and _cow_is_down(point, baseline)
    ]
    other_down = [
        point
        for point in counted
        if point not in missing_down and point not in light and _cow_is_down(point, baseline)
    ]
    excluded = [point for point in missing if point not in counted]

    if missing_down:
        return _missing_comment(
            farm,
            missing_down,
            usual_volume,
            usual_count,
            other_down,
            [point for point in counted if point not in missing_down],
        )
    if light and len(light) >= len(other_down):
        return _light_comment(
            light,
            usual_volume,
            other_down,
            [point for point in counted if point not in light],
        )
    down = [point for point in counted if _cow_is_down(point, baseline)]
    return _yield_comment(down, counted, baseline, excluded)


def _missing_comment(
    farm: str,
    days: list[dict[str, Any]],
    usual_volume: float | None,
    usual_count: int,
    others_down: list[dict[str, Any]],
    others: list[dict[str, Any]],
) -> str:
    usual = ""
    if usual_volume is not None and usual_count >= 2:
        usual = (
            f" A usual {farm} day is about {_fmt_litres(usual_volume)} L"
            f" from {_load_word(usual_count)}."
        )
    if len(days) == 1:
        point = days[0]
        day = _point_day(point)
        volume = _point_volume(point) or 0
        count = _point_count(point)
        date = _fmt_date(day) if day else "One day"
        clearly_short = usual_volume is not None and volume <= usual_volume * _MISSING_VOLUME
        if clearly_short:
            lead = f"{date} was {_fmt_litres(volume)} L from {_load_word(count)}."
        else:
            lead = (
                f"{date} had {_load_word(count)} ({_fmt_litres(volume)} L)"
                f" rather than the usual {_load_word(usual_count)}."
            )
        return lead + usual + _others_clause(others_down, others, "one missing load")
    parts = []
    for point in days:
        day = _point_day(point)
        volume = _point_volume(point) or 0
        date = _fmt_date(day) if day else "One day"
        parts.append(f"{date} ({_fmt_litres(volume)} L, {_load_word(_point_count(point))})")
    return (
        f"{_join_and(parts)} are short of a full day.{usual}"
        " Those days are pulling milk per cow down."
    )


def _others_clause(others_down: list[dict[str, Any]], others: list[dict[str, Any]], cause: str) -> str:
    if not others:
        return ""
    if not others_down:
        return (
            f" The other days are close to the 30-day milk per cow,"
            f" so this figure is being pulled down by {cause}."
        )
    return f" Milk per cow is also down on {len(others_down)} of the other {len(others)} days."


def _light_comment(
    days: list[dict[str, Any]],
    usual_volume: float | None,
    others_down: list[dict[str, Any]],
    others: list[dict[str, Any]],
) -> str:
    usual = _fmt_litres(usual_volume) if usual_volume is not None else None
    if len(days) == 1:
        point = days[0]
        day = _point_day(point)
        date = _fmt_date(day) if day else "One day"
        volume = _fmt_litres(_point_volume(point) or 0)
        if usual:
            lead = f"{date} was {volume} L against a usual day of about {usual} L."
        else:
            lead = f"{date} was {volume} L, lighter than the other recent days."
        return (
            lead
            + " The loads are there, so that day looks smaller than expected."
            + _others_clause(others_down, others, "that light day")
        )
    parts = []
    for point in days:
        day = _point_day(point)
        date = _fmt_date(day) if day else "One day"
        parts.append(f"{date} was {_fmt_litres(_point_volume(point) or 0)} L")
    usual_bit = f"a usual {usual} L" if usual else "the usual day"
    return (
        f"{len(days)} days look smaller than {usual_bit}, and the loads are there: {_join_and(parts)}."
        + _others_clause(others_down, others, "those light days")
    )


def _yield_comment(
    down_days: list[dict[str, Any]],
    counted: list[dict[str, Any]],
    baseline: Any,
    excluded: list[dict[str, Any]],
) -> str:
    base = _num(baseline)
    base_text = _fmt_num(base, 1) if base is not None else None
    excluded_dates = []
    for point in excluded:
        day = _point_day(point)
        if day is not None:
            excluded_dates.append(_fmt_date(day))
    excluded_clause = ""
    if excluded_dates:
        excluded_clause = f" A short collection on {_join_and(excluded_dates)} is not included in this figure."

    named = down_days or counted
    if named and len(named) <= 2 and base_text:
        parts = []
        for point in named:
            day = _point_day(point)
            value = _num(point.get("litres_per_cow"))
            if day is None or value is None:
                continue
            parts.append(f"{_fmt_date(day)} ({_fmt_num(value, 1)})")
        if parts:
            tail = excluded_clause or " The loads for those days are there."
            return f"Milk per cow was low on {_join_and(parts)}, against a 30-day average of {base_text}.{tail}"

    if counted and down_days:
        phrase = _day_count_phrase(len(down_days), len(counted))
        if excluded_clause:
            return f"Milk per cow is down on {phrase}.{excluded_clause}"
        return f"Milk per cow is down on {phrase}, not because a load is missing."

    if base_text:
        return f"Milk per cow is below the 30-day average of {base_text}.{excluded_clause}"
    return "Milk per cow is below its 30-day average."


def _loads_for(
    loads: list[dict[str, Any]],
    farm: str,
    start: dt.date,
    end: dt.date,
) -> list[dict[str, Any]]:
    chosen = []
    for row in loads:
        if str(row.get("farm") or "").strip().upper() != farm:
            continue
        day = _parse_day(row.get("sample_date"))
        if day is None or day < start or day > end:
            continue
        chosen.append(row)
    return chosen


def _metric_loads(loads: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    ready = []
    for row in loads:
        if row.get("quality_estimated"):
            continue
        if _num(row.get(key)) is None:
            continue
        ready.append(row)
    return ready


def _lone_extreme(
    loads: list[dict[str, Any]],
    key: str,
    *,
    line: float,
    high: bool,
    gap: float,
    ratio: float,
) -> tuple[dict[str, Any], list[float]] | None:
    if len(loads) < 2:
        return None
    def value_of(row: dict[str, Any]) -> float:
        return float(row[key])

    peak = max(loads, key=value_of) if high else min(loads, key=value_of)
    peak_value = value_of(peak)
    rest = [value_of(row) for row in loads if row is not peak]
    if not rest:
        return None
    if high:
        if peak_value < line or any(value >= line for value in rest):
            return None
    else:
        if peak_value > line or any(value <= line for value in rest):
            return None
    middle = float(statistics.median(rest))
    distance = (peak_value - middle) if high else (middle - peak_value)
    scaled = peak_value >= middle * ratio if high else (middle > 0 and peak_value <= middle / ratio)
    if distance < gap and not scaled:
        return None
    return peak, rest


def _sample_gap_sentence(day_loads: list[dict[str, Any]], spike: dict[str, Any]) -> str:
    """A tanker that arrived but has no BactoScan result."""
    missing = [
        row
        for row in day_loads
        if row is not spike and row.get("sample_missing") and _num(row.get("bactoscan")) is None
    ]
    if not missing:
        return ""
    if len(missing) == 1:
        return " The other load that day had no sample."
    return " Other loads that day had no sample."


def _high_days(series: list[tuple[dt.date, float | None]], line: float) -> list[tuple[dt.date, float]]:
    return [(day, value) for day, value in series if value is not None and value >= line]


def _low_days(series: list[tuple[dt.date, float | None]], line: float) -> list[tuple[dt.date, float]]:
    return [(day, value) for day, value in series if value is not None and value <= line]


def _present(series: list[tuple[dt.date, float | None]]) -> list[tuple[dt.date, float]]:
    return [(day, value) for day, value in series if value is not None]


def _fmt_dated(pairs: list[tuple[dt.date, float]], dp: int) -> str:
    return _join_and(f"{_fmt_date(day)} ({_fmt_num(value, dp)})" for day, value in pairs)


def _bactoscan_comment(
    points: list[dict[str, Any]],
    loads: list[dict[str, Any]],
    *,
    window_end: dt.date,
    average: Any,
) -> str:
    series = _calendar_values(points, window_end=window_end, days=_SHORT_WINDOW_DAYS, key="bactoscan", dp=0)
    scored = _metric_loads(loads, "bactoscan")
    lone = _lone_extreme(scored, "bactoscan", line=_BACTO_HIGH, high=True, gap=15, ratio=1.8)
    if lone is not None:
        spike, rest = lone
        day = _parse_day(spike.get("sample_date"))
        when = _fmt_date(day) if day else "one day"
        same_day = [row for row in loads if _parse_day(row.get("sample_date")) == day]
        loads_that_day = [
            row
            for row in same_day
            if _num(row.get("litres_load")) or _num(row.get("litres_weighbridge"))
        ]
        if len(loads_that_day) >= 2:
            opener = f"One of the loads on {when} scored {_fmt_num(float(spike['bactoscan']), 0)}."
        else:
            opener = f"The load on {when} scored {_fmt_num(float(spike['bactoscan']), 0)}."
        return (
            opener
            + _sample_gap_sentence(same_day, spike)
            + f" The other loads this week scored {_fmt_range(rest, 0)},"
            " so the 7-day figure is a single high score."
        )

    high = _high_days(series, _BACTO_HIGH)
    quiet = [(day, value) for day, value in _present(series) if value < _BACTO_HIGH]
    if len(high) == 1:
        day, value = high[0]
        others = f" The other days were {_fmt_range([item[1] for item in quiet], 0)}." if quiet else ""
        same_day = [row for row in scored if _parse_day(row.get("sample_date")) == day]
        both = ""
        if len(same_day) >= 2 and all(float(row["bactoscan"]) >= _BACTO_HIGH for row in same_day):
            scores = _join_and(_fmt_num(float(row["bactoscan"]), 0) for row in same_day)
            both = f" Both loads that day were high ({scores})."
        return (
            f"BactoScan was {_fmt_num(value, 0)} on {_fmt_date(day)}.{both}{others}"
            " The 7-day figure is one high day rather than a high week."
        )

    last3 = series[-3:]
    earlier = [value for _day, value in series[:-3] if value is not None and value >= _BACTO_HIGH]
    if len(last3) == 3 and all(value is not None and value >= _BACTO_HIGH for _day, value in last3) and not earlier:
        listed = _fmt_dated([(day, value) for day, value in last3 if value is not None], 0)
        earlier_vals = [value for _day, value in series[:-3] if value is not None]
        tail = f" Earlier in the week they were {_fmt_range(earlier_vals, 0)}." if earlier_vals else ""
        return f"The last three days were all {_BACTO_HIGH} or higher: {listed}.{tail}"

    if len(high) >= 2:
        return (
            f"BactoScan was {_BACTO_HIGH} or higher on {_fmt_dated(high, 0)},"
            " so this is a run of high results rather than one bad load."
        )

    avg = _num(average)
    if avg is not None:
        return f"The 7-day BactoScan average is {_fmt_num(avg, 0)}."
    return "BactoScan is on the alert line."


def _scc_comment(
    points: list[dict[str, Any]],
    loads: list[dict[str, Any]],
    *,
    window_end: dt.date,
    recent: Any,
    baseline: Any,
) -> str:
    series = _calendar_values(points, window_end=window_end, days=_SHORT_WINDOW_DAYS, key="scc", dp=0)
    scored = _metric_loads(loads, "scc")
    d7 = _num(recent)
    d30 = _num(baseline)
    rise = ""
    if d7 is not None and d30 is not None and d30 > 0:
        pct = int(round((d7 - d30) / d30 * 100))
        rise = f" The week is {pct}% above the 30-day average of {_fmt_num(d30, 0)}."

    lone = _lone_extreme(scored, "scc", line=_SCC_LEVEL, high=True, gap=80, ratio=1.5)
    if lone is not None:
        spike, rest = lone
        day = _parse_day(spike.get("sample_date"))
        when = _fmt_date(day) if day else "one day"
        return (
            f"One load on {when} had an SCC of {_fmt_num(float(spike['scc']), 0)}."
            f" The other loads this week were {_fmt_range(rest, 0)}."
            f"{rise}"
        )

    high = [(day, value) for day, value in _present(series) if value > _SCC_LEVEL]
    quiet = [value for _day, value in _present(series) if value <= _SCC_LEVEL]
    if len(high) == 1:
        day, value = high[0]
        others = f" The other days were {_fmt_range(quiet, 0)}." if quiet else ""
        return f"SCC was {_fmt_num(value, 0)} on {_fmt_date(day)}.{others}{rise}"
    if len(high) >= 2:
        return (
            f"SCC was above {_SCC_LEVEL} on {_day_count_phrase(len(high), len(_present(series)) or len(high))}."
            f"{rise}"
        )
    return f"SCC is above {_SCC_LEVEL}.{rise}"


def _fat_comment(
    points: list[dict[str, Any]],
    loads: list[dict[str, Any]],
    *,
    window_end: dt.date,
) -> str:
    series = _calendar_values(points, window_end=window_end, days=_SHORT_WINDOW_DAYS, key="butterfat_pct", dp=2)
    scored = _metric_loads(loads, "butterfat_pct")
    lone = _lone_extreme(scored, "butterfat_pct", line=_FAT_LOW, high=False, gap=0.25, ratio=1.08)
    if lone is not None:
        spike, rest = lone
        day = _parse_day(spike.get("sample_date"))
        when = _fmt_date(day) if day else "one day"
        return (
            f"One load on {when} was {_fmt_num(float(spike['butterfat_pct']), 2)}% fat."
            f" The other loads this week were {_fmt_range(rest, 2)}%,"
            " so the 7-day average is one low load."
        )

    low = _low_days(series, _FAT_LOW)
    quiet = [value for _day, value in _present(series) if value > _FAT_LOW]
    if len(low) == 1:
        day, value = low[0]
        others = f" The other days were {_fmt_range(quiet, 2)}%." if quiet else ""
        return (
            f"Fat was {_fmt_num(value, 2)}% on {_fmt_date(day)}.{others}"
            " The 7-day average is being pulled down by one day."
        )
    if len(low) >= 2:
        values = [value for _day, value in low]
        return (
            f"Fat was {_FAT_LOW:.1f}% or below on {_day_count_phrase(len(low), len(_present(series)) or len(low))}"
            f" ({_fmt_range(values, 2)}%)."
        )
    return f"Fat is at or below {_FAT_LOW:.1f}%."


def _temp_comment(points: list[dict[str, Any]], *, window_end: dt.date) -> str:
    series = _calendar_values(points, window_end=window_end, days=4, key="temp_c", dp=1)
    readings = [(day, _half_up(value, 1)) for day, value in series if value is not None]
    if len(readings) >= 2:
        listed = _join_and(f"{_fmt_date(day)} {_fmt_num(value, 1)}°C" for day, value in readings)
        last = readings[-1][1]
        return f"Milk temperature rose each day and is now {_fmt_num(last, 1)}°C: {listed}."
    return "Milk temperature has been rising and is at 4.0°C or above."


def _farm_loads(loads: list[dict[str, Any]], farm: str, window_end: dt.date) -> list[dict[str, Any]]:
    return _loads_for(loads, farm, _window_start(window_end, _SHORT_WINDOW_DAYS), window_end)


def build_production_issues(
    *,
    farms: list[dict[str, Any]],
    trend: dict[str, list[dict[str, Any]]],
    loads: list[dict[str, Any]],
    window_end: dt.date,
) -> list[dict[str, Any]]:
    """Red production tiles, in farm order, with a comment for each."""
    issues: list[dict[str, Any]] = []
    by_farm = {str(row.get("farm") or "").upper(): row for row in farms}
    for farm, bundle in by_farm.items():
        points = list(trend.get(farm) or [])
        d7 = bundle.get("d7") or {}
        d30 = bundle.get("d30") or {}
        week_loads = _farm_loads(loads, farm, window_end)
        bactoscan_series = [
            value
            for _day, value in _calendar_values(
                points, window_end=window_end, days=_SHORT_WINDOW_DAYS, key="bactoscan", dp=0
            )
        ]
        temp_series = [
            value
            for _day, value in _calendar_values(
                points, window_end=window_end, days=4, key="temp_c", dp=1
            )
        ]
        flags = (
            ("milk_per_cow", _milk_per_cow_red(d7.get("milk_per_cow"), d30.get("milk_per_cow"))),
            ("butterfat_pct", _fat_red(d7.get("butterfat_pct"))),
            ("bactoscan", _bactoscan_red(d7.get("bactoscan"), bactoscan_series)),
            ("scc", _scc_red(d7.get("scc"), d30.get("scc"))),
            ("milk_temp", _temp_red(temp_series)),
        )
        for metric, is_red in flags:
            if not is_red:
                continue
            if metric == "milk_per_cow":
                comment = _milk_per_cow_comment(
                    farm,
                    points,
                    window_end=window_end,
                    baseline=d30.get("milk_per_cow"),
                    daily_average=_num(d7.get("milk_per_day")),
                    two_collections=farm in _TWO_COLLECTION_FARMS,
                )
            elif metric == "butterfat_pct":
                comment = _fat_comment(points, week_loads, window_end=window_end)
            elif metric == "bactoscan":
                comment = _bactoscan_comment(
                    points, week_loads, window_end=window_end, average=d7.get("bactoscan")
                )
            elif metric == "scc":
                comment = _scc_comment(
                    points,
                    week_loads,
                    window_end=window_end,
                    recent=d7.get("scc"),
                    baseline=d30.get("scc"),
                )
            else:
                comment = _temp_comment(points, window_end=window_end)
            issues.append(
                {
                    "farm": farm,
                    "metric": metric,
                    "label": _LABELS[metric],
                    "value": d7.get(metric),
                    "baseline": d30.get(metric),
                    "comment": comment,
                    "href": f"/milk-quality/collections?farm={farm}",
                }
            )
    return issues


def _rounded_litres(value: Any) -> int | None:
    number = _num(value)
    if number is None or number <= 0:
        return None
    return int(math.floor(number + 0.5))


def _weighbridge_gap(farm: str, litres: int, bridge: int) -> int | None:
    gap = abs(litres - bridge)
    if farm == "ALH":
        return gap if gap > _WEIGHBRIDGE_TOLERANCE_L else None
    if litres != bridge:
        return gap
    return None


def _load_gap_phrase(day: dt.date, load: Any, litres: int, bridge: int) -> str:
    load_bit = ""
    if load is not None and load != "":
        try:
            load_bit = f" load {int(load)}"
        except (TypeError, ValueError):
            load_bit = f" load {load}"
    return (
        f"{_fmt_date(day)}{load_bit} "
        f"(weighbridge {_fmt_litres(bridge)}, litres {_fmt_litres(litres)})"
    )


def _previous_month_start(today: dt.date) -> dt.date:
    """First day of the month before ``today``."""
    this_month = today.replace(day=1)
    return (this_month - dt.timedelta(days=1)).replace(day=1)


def _month_span(start: dt.date, today: dt.date) -> str:
    opening = start.strftime("%B")
    closing = today.strftime("%B")
    if opening == closing:
        return opening
    return f"{opening} and {closing}"


def _weighbridge_issues(
    rows: list[dict[str, Any]],
    *,
    start: dt.date,
    cutoff: dt.date,
    today: dt.date,
) -> list[dict[str, Any]]:
    """Loads in this month and the previous one whose weighbridge still does not agree.

    The last 7 days are left out, so a load still waiting on Müller is not listed.
    Parkes has no weighbridge. ALH is allowed 1,000 L. A missing weighbridge is
    not a mismatch: there is nothing to compare.
    """
    span = _month_span(start, today)
    found: dict[str, list[tuple[dt.date, Any, int, int, int]]] = {}
    for row in rows:
        farm = str(row.get("farm") or "").strip().upper()
        if not farm or farm == _PARKES_FARM:
            continue
        day = _parse_day(row.get("sample_date"))
        if day is None or day < start or day >= cutoff:
            continue
        if row.get("manual_match"):
            continue
        litres = _rounded_litres(row.get("litres_load"))
        bridge = _rounded_litres(row.get("litres_weighbridge"))
        if litres is None or bridge is None:
            continue
        gap = _weighbridge_gap(farm, litres, bridge)
        if gap is None:
            continue
        found.setdefault(farm, []).append((day, row.get("load_number"), litres, bridge, gap))

    issues: list[dict[str, Any]] = []
    for farm in ("ALH", "BNK", "SFR", "COF"):
        loads = found.get(farm) or []
        if not loads:
            continue
        loads.sort(key=lambda item: (item[0], item[1] if isinstance(item[1], int) else 0))
        largest = max(gap for _day, _load, _litres, _bridge, gap in loads)
        listed = _join_and(
            _load_gap_phrase(day, load, litres, bridge)
            for day, load, litres, bridge, _gap in loads
        )
        count = len(loads)
        loads_word = "load" if count == 1 else "loads"
        if farm == "ALH":
            verb = "is" if count == 1 else "are"
            lead = f"{count} {loads_word} in {span} {verb} more than 1,000 L from the litres"
        else:
            verb = "does not match" if count == 1 else "do not match"
            lead = f"{count} {loads_word} in {span} {verb} the litres"
        issues.append(
            {
                "farm": farm,
                "metric": "weighbridge",
                "label": _LABELS["weighbridge"],
                "value": count,
                "baseline": largest,
                "value_text": f"{count:,}",
                "baseline_text": f"{largest:,} L",
                "value_title": f"Loads in {span}, older than 7 days, whose weighbridge does not agree",
                "baseline_title": "Largest gap between weighbridge and litres",
                "comment": f"{lead}: {listed}.",
                "href": f"/milk-quality/collections?farm={farm}",
            }
        )
    return issues


def _parkes_drop_issue(rows: list[dict[str, Any]], *, yesterday: dt.date) -> dict[str, Any] | None:
    """A Parkes day more than 10% under the previous collection day."""
    totals: dict[dt.date, int] = {}
    for row in rows:
        if str(row.get("farm") or "").strip().upper() != _PARKES_FARM:
            continue
        day = _parse_day(row.get("sample_date"))
        if day is None:
            continue
        litres = _rounded_litres(row.get("litres_load"))
        if litres is None:
            litres = _rounded_litres(row.get("litres_weighbridge"))
        if litres is None:
            continue
        totals[day] = totals.get(day, 0) + litres

    days = [day for day in sorted(totals) if day <= yesterday]
    drops: list[tuple[dt.date, dt.date, int, int]] = []
    for previous, day in zip(days, days[1:]):
        before = totals[previous]
        current = totals[day]
        if before <= 0:
            continue
        if (before - current) / before > _PARKES_DROP:
            drops.append((day, previous, current, before))
    if not drops:
        return None

    def phrase(day: dt.date, previous: dt.date, current: int, before: int) -> str:
        percent = int(round(100 * (before - current) / before))
        return (
            f"{_fmt_date(day)} was {_fmt_litres(current)} L, {percent}% below "
            f"{_fmt_date(previous)} ({_fmt_litres(before)} L)"
        )

    if len(drops) == 1:
        day, previous, current, before = drops[0]
        return {
            "farm": _PARKES_FARM,
            "metric": "collection",
            "label": _LABELS["collection"],
            "value": current,
            "baseline": before,
            "value_text": f"{current:,}",
            "baseline_text": f"{before:,}",
            "value_title": f"Litres on {_fmt_date(day)}",
            "baseline_title": f"Litres on the previous collection day, {_fmt_date(previous)}",
            "comment": f"{phrase(day, previous, current, before)}.",
            "href": "/milk-quality/collections?farm=PRK",
        }

    worst = max(drops, key=lambda item: (item[3] - item[2]) / item[3])
    worst_percent = int(round(100 * (worst[3] - worst[2]) / worst[3]))
    return {
        "farm": _PARKES_FARM,
        "metric": "collection",
        "label": _LABELS["collection"],
        "value": len(drops),
        "baseline": worst_percent,
        "value_text": f"{len(drops):,}",
        "baseline_text": f"{worst_percent}%",
        "value_title": "Days more than 10% below the previous collection day",
        "baseline_title": "Largest drop against the previous day",
        "comment": f"{_join_and(phrase(*item) for item in drops)}.",
        "href": "/milk-quality/collections?farm=PRK",
    }


def collection_issues(rows: list[dict[str, Any]], *, today: dt.date) -> list[dict[str, Any]]:
    """Weighbridge gaps from this month and the last, and a short Parkes day."""
    cutoff = today - dt.timedelta(days=7)
    yesterday = today - dt.timedelta(days=1)
    issues = _weighbridge_issues(
        rows,
        start=_previous_month_start(today),
        cutoff=cutoff,
        today=today,
    )
    parkes = _parkes_drop_issue(rows, yesterday=yesterday)
    if parkes is not None:
        issues.append(parkes)
    return issues
