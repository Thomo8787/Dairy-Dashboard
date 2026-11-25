"""Red farm conception-rate tiles for the Home Issues tab.

The tile uses the same window and the same success rule as the Fertility
screen: services from 30 days before the PD cut-off through that cut-off,
against the 90 days before, and red when the recent rate is under 90% of
the earlier one. A bull or technician is named only when they have at least
15 services in that window and their rate is at least 10 points below the
farm. Weather is the Nantwich area for those same dates. Heat is the
fertility effect that is worth mentioning.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.breeding_sires import normalize_sire_code
from services.conception_matches import NOT_RECORDED, load_matches, match_map
from services.database import CowEvent, HerdInventory
from services.events_common import (
    _PREGNANT_RC,
    _add_inventory_conception,
    _animal_identifier,
    _build_fertility_outcomes,
    _build_youngstock_fertility,
    _coerce_date,
    _conception_windows,
    _format_tile_day,
    _last_main_pd_day,
    _pd_lag_days,
    _pd_session_min,
    _walk_lactation,
    _with_fdat_fresh,
)
from services.farms import HERD_FARM_OPTIONS

# High-yielding Holsteins start to feel heat stress around this temperature-
# humidity index. Built from the day's maximum temperature and its mean humidity.
_HEAT_THI = 72
_SEVERE_THI = 78
# A 30-day window is too short to blame a handful of serves. 15 is enough
# to name someone without treating one unlucky run as the cause.
_MIN_NAMED_SERVICES = 15
_CLEAR_GAP = 10
# The herds sit around Nantwich. A few miles apart, so one reading covers a heat spell.
_NANTWICH = (53.1039, -2.5379)
_WEATHER_URL = "https://archive-api.open-meteo.com/v1/archive"

_weather_cache: dict[tuple[str, str], str] = {}


def _sire_name(remark: str | None) -> str:
    code = normalize_sire_code(remark)
    if not code:
        return ""
    return code.upper()


def _tech_name(value: str | None) -> str:
    return (value or "").strip()


def _thi(temp_c: float, rh: float) -> float:
    return (1.8 * temp_c + 32) - ((0.55 - 0.0055 * rh) * (1.8 * temp_c - 26))


def _rate(success: int, total: int) -> int | None:
    if total <= 0:
        return None
    return int(round(100.0 * success / total))


def _weather_sentence(samples: list[tuple[float | None, float | None]]) -> str:
    """Say whether heat in the window could have pulled conception down."""
    temps = [temp for temp, _rh in samples if temp is not None]
    if not temps:
        return "Weather for this window could not be checked."
    peak = max(temps)
    heat_days = 0
    severe_days = 0
    for temp, rh in samples:
        if temp is None:
            continue
        # Mean humidity with the day's maximum overstates the index, so a
        # mild day is not called heat stress unless it actually reached 25°C.
        if temp < 25:
            continue
        if rh is None:
            hot = True
            harsh = temp >= 28
        else:
            index = _thi(temp, rh)
            hot = index >= _HEAT_THI
            harsh = temp >= 28 or index >= _SEVERE_THI
        if hot:
            heat_days += 1
        if harsh:
            severe_days += 1
    peak_text = f"{round(peak):.0f}"
    if severe_days or heat_days >= 3:
        day_word = "day" if heat_days == 1 else "days"
        return (
            f"Around Nantwich, {heat_days} {day_word} in this window were hot enough for heat stress "
            f"(peak {peak_text}°C). Heat around service can pull conception down."
        )
    if heat_days:
        day_word = "day" if heat_days == 1 else "days"
        return (
            f"Around Nantwich, {heat_days} {day_word} got hot enough for heat stress "
            f"(peak {peak_text}°C). That can affect the serves on those days, though most of the window was cooler."
        )
    return (
        f"Around Nantwich the weather was mild in this window (peak {peak_text}°C), "
        "so heat does not look like the cause."
    )


def _fetch_weather(start: dt.date, end: dt.date) -> str:
    key = (start.isoformat(), end.isoformat())
    cached = _weather_cache.get(key)
    if cached is not None:
        return cached
    lat, lon = _NANTWICH
    try:
        response = requests.get(
            _WEATHER_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "daily": "temperature_2m_max,relative_humidity_2m_mean",
                "timezone": "Europe/London",
            },
            timeout=(3, 8),
        )
        response.raise_for_status()
        daily = response.json().get("daily") or {}
        temps = daily.get("temperature_2m_max") or []
        humidity = daily.get("relative_humidity_2m_mean") or []
        samples = [
            (temps[index] if index < len(temps) else None, humidity[index] if index < len(humidity) else None)
            for index in range(max(len(temps), len(humidity)))
        ]
        sentence = _weather_sentence(samples)
    except (requests.RequestException, ValueError, TypeError):
        sentence = "Weather for this window could not be checked."
    _weather_cache[key] = sentence
    return sentence


def _is_bull_service(name: str) -> bool:
    """DairyComp records a natural bull service as the technician, not a person."""
    text = name.strip().casefold()
    return text.startswith("b:") or text in {"bull", "natural"}


def _label(name: str, aliases: dict[str, str]) -> str:
    if not name or name.casefold() == NOT_RECORDED.casefold():
        return ""
    return aliases.get(name, name)


def _named_counts(
    rows: list[tuple[str, str, bool]],
    index: int,
    aliases: dict[str, str],
    *,
    people_only: bool = False,
) -> dict[str, list[int]]:
    totals: dict[str, list[int]] = {}
    for row in rows:
        label = _label(row[index], aliases)
        if not label or (people_only and _is_bull_service(label)):
            continue
        bucket = totals.setdefault(label, [0, 0])
        bucket[1] += 1
        if row[2]:
            bucket[0] += 1
    return totals


def _poor(totals: dict[str, list[int]], farm_rate: int) -> list[tuple[str, int, int, int]]:
    """Names at least 10 points under the farm, with enough services to say so."""
    found: list[tuple[int, int, str, int]] = []
    for name, (success, total) in totals.items():
        if total < _MIN_NAMED_SERVICES:
            continue
        rate = _rate(success, total)
        if rate is None or rate > farm_rate - _CLEAR_GAP:
            continue
        found.append((rate, -total, name, success))
    found.sort()
    return [(name, rate, success, -total) for rate, total, name, success in found]


def _same_services(
    rows: list[tuple[str, str, bool]],
    sire: str,
    technician: str,
    sire_aliases: dict[str, str],
    tech_aliases: dict[str, str],
) -> bool:
    """True when this bull and this technician are the same serves."""
    sire_total = tech_total = both = 0
    for raw_sire, raw_tech, _success in rows:
        sire_label = _label(raw_sire, sire_aliases)
        tech_label = _label(raw_tech, tech_aliases)
        is_sire = sire_label == sire
        is_tech = tech_label == technician
        if is_sire:
            sire_total += 1
        if is_tech:
            tech_total += 1
        if is_sire and is_tech:
            both += 1
    return sire_total > 0 and sire_total == tech_total == both


def _people_comment(
    rows: list[tuple[str, str, bool]],
    farm_rate: int,
    sire_aliases: dict[str, str],
    tech_aliases: dict[str, str],
) -> str:
    farm_success = sum(1 for _sire, _tech, success in rows if success)
    farm_total = len(rows)
    sires = _poor(_named_counts(rows, 0, sire_aliases), farm_rate)
    techs = _poor(_named_counts(rows, 1, tech_aliases, people_only=True), farm_rate)
    if sires and techs and _same_services(rows, sires[0][0], techs[0][0], sire_aliases, tech_aliases):
        techs = techs[1:]
    chosen: list[tuple[str, str, int, int, int]] = []
    if sires and techs:
        chosen.append(("Bull", *sires[0]))
        chosen.append(("Technician", *techs[0]))
    elif sires:
        chosen.extend(("Bull", *row) for row in sires[:2])
    elif techs:
        chosen.extend(("Technician", *row) for row in techs[:2])
    if not chosen:
        return (
            "No bull or technician with at least 15 services in this window "
            "was clearly below the farm."
        )

    def phrase(kind: str, name: str, rate: int, _success: int, total: int) -> str:
        return f"{kind} {name} was {rate}% across {total} services"

    if len(chosen) == 1:
        kind, name, rate, success, total = chosen[0]
        sentence = f"{phrase(kind, name, rate, success, total)} in this window."
        rest_total = farm_total - total
        rest_success = farm_success - success
        rest_rate = _rate(rest_success, rest_total)
        if rest_rate is None:
            return sentence
        return f"{sentence} The other services were {rest_rate}%."
    first, second = chosen
    _kind, first_name, first_rate, first_success, first_total = first
    second_kind, second_name, second_rate, second_success, second_total = second
    if first[0] == second[0]:
        sentence = (
            f"{first[0]} {first_name} was {first_rate}% across {first_total} services in this window, "
            f"and {second_name} was {second_rate}% across {second_total} services."
        )
        rest_rate = _rate(farm_success - first_success - second_success, farm_total - first_total - second_total)
        if rest_rate is None:
            return sentence
        return f"{sentence} The other services were {rest_rate}%."
    return (
        f"{phrase(*first)} in this window, and "
        f"{phrase(second_kind.lower(), second_name, second_rate, second_success, second_total)}."
    )


def _window_services(
    db: Session,
    farms: list[str],
    today: dt.date,
    *,
    lact_min: int | None,
    lact_eq: int | None,
    windows: dict[str, tuple[dt.date, dt.date]],
) -> dict[str, list[tuple[str, str, bool]]]:
    """Services in each farm's recent conception window, with the tile's success rule."""
    query = (
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
        .where(CowEvent.event.in_(("FRESH", "BRED", "PREG", "OPEN", "ABORT", "SOLD", "DIED")))
        .where(CowEvent.event_date.isnot(None))
        .where(CowEvent.farm.in_(farms))
    )
    if lact_min is not None:
        query = query.where(CowEvent.lact >= lact_min)
    if lact_eq is not None:
        query = query.where(CowEvent.lact == lact_eq)
    grouped: dict[tuple, list[tuple[dt.date, str]]] = {}
    bred: dict[tuple, dict[dt.date, tuple[str, str]]] = {}
    fresh_dates: dict[tuple, dt.date] = {}
    for farm, cow_id, etag, lact, event, event_date, remark, technician, fdat in db.execute(query).all():
        animal = _animal_identifier(cow_id, etag)
        day = _coerce_date(event_date)
        if farm not in windows or animal is None or lact is None or not isinstance(day, dt.date):
            continue
        key = (farm, animal, int(lact))
        grouped.setdefault(key, []).append((day, event))
        birth_fresh = _coerce_date(fdat)
        if isinstance(birth_fresh, dt.date):
            fresh_dates.setdefault(key, birth_fresh)
        if event == "BRED":
            slot = bred.setdefault(key, {})
            current = slot.get(day)
            sire = _sire_name(remark)
            tech = _tech_name(technician)
            if current is None:
                slot[day] = (sire, tech)
            else:
                slot[day] = (sire or current[0], tech or current[1])

    pregnant: dict[tuple, float | None] = {}
    inv = (
        select(
            HerdInventory.farm,
            HerdInventory.cow_id,
            HerdInventory.etag,
            HerdInventory.lact,
            HerdInventory.dcc,
            HerdInventory.fdat,
        )
        .where(HerdInventory.rc.in_(_PREGNANT_RC))
        .where(HerdInventory.farm.in_(farms))
    )
    if lact_min is not None:
        inv = inv.where(HerdInventory.lact >= lact_min)
    if lact_eq is not None:
        inv = inv.where(HerdInventory.lact == lact_eq)
    for farm, cow_id, etag, lact, dcc, fdat in db.execute(inv).all():
        animal = _animal_identifier(cow_id, etag)
        if farm not in windows or animal is None or lact is None:
            continue
        key = (farm, animal, int(lact))
        pregnant[key] = dcc
        birth_fresh = _coerce_date(fdat)
        if isinstance(birth_fresh, dt.date):
            fresh_dates.setdefault(key, birth_fresh)

    by_farm: dict[str, list[tuple[str, str, bool]]] = {farm: [] for farm in windows}
    for key in set(grouped) | set(pregnant):
        farm = key[0]
        recent_start, cut_off = windows[farm]
        events = grouped.get(key, [])
        if lact_eq == 0:
            walked = events
        else:
            walked = _with_fdat_fresh(events, fresh_dates.get(key))
        _fresh, _exit, services, conceptions = _walk_lactation(walked, require_fresh=False)
        _add_inventory_conception(conceptions, services, today=today, dcc=pregnant.get(key))
        conceived = {service for service, _confirmed, _ended in conceptions}
        seen: set[dt.date] = set()
        names = bred.get(key, {})
        for service in services:
            if service in seen or service < recent_start or service > cut_off:
                continue
            seen.add(service)
            sire, tech = names.get(service, ("", ""))
            by_farm[farm].append((sire, tech, service in conceived))
    return by_farm


def _issue(
    *,
    farm: str,
    shown: int,
    baseline: int,
    value_title: str,
    baseline_title: str,
    href: str,
    start: dt.date,
    end: dt.date,
    rows: list[tuple[str, str, bool]],
    service_word: str,
    sire_aliases: dict[str, str],
    tech_aliases: dict[str, str],
) -> dict[str, Any]:
    lead = (
        f"{shown}% across {len(rows)} {service_word} from {_format_tile_day(start)} "
        f"to {_format_tile_day(end)}, against {baseline}% in the 90 days before."
    )
    people = _people_comment(rows, shown, sire_aliases, tech_aliases)
    weather = _fetch_weather(start, end)
    return {
        "farm": farm,
        "metric": "farm_conception",
        "label": "Conception rate",
        "value": shown,
        "baseline": baseline,
        "value_text": f"{shown}%",
        "baseline_text": f"{baseline}%",
        "value_title": value_title,
        "baseline_title": baseline_title,
        "comment": f"{lead} {people} {weather}",
        "href": href,
    }


def farm_conception_issues(db: Session, today: dt.date | None = None) -> list[dict[str, Any]]:
    """Farm and youngstock conception tiles that are red on Home."""
    today = today or dt.date.today()
    farms = list(HERD_FARM_OPTIONS)
    outcomes = _build_fertility_outcomes(db, farms, today)
    young = _build_youngstock_fertility(db, farms, today)
    red = [farm for farm in farms if outcomes.get(farm, {}).get("conception_rate_status") == "red"]
    young_red = young.get("conception_rate_status") == "red"
    if not red and not young_red:
        return []

    sire_aliases = match_map(load_matches(db, "sire"))
    tech_aliases = match_map(load_matches(db, "technician"))
    issues: list[dict[str, Any]] = []
    if red:
        windows = {
            farm: _conception_windows(
                today,
                _last_main_pd_day(db, farms=[farm], lact_min=1, min_checks=_pd_session_min(farm)),
                _pd_lag_days(farm),
            )
            for farm in red
        }
        served = _window_services(
            db,
            red,
            today,
            lact_min=1,
            lact_eq=None,
            windows={farm: (window[0], window[1]) for farm, window in windows.items()},
        )
        for farm in red:
            row = outcomes[farm]
            shown = row.get("conception_rate")
            baseline = row.get("conception_rate_avg_120")
            if not isinstance(shown, int) or not isinstance(baseline, int):
                continue
            recent_start, cut_off, _prior, _last_pd = windows[farm]
            issues.append(
                _issue(
                    farm=farm,
                    shown=shown,
                    baseline=baseline,
                    value_title=row.get("conception_rate_title") or "",
                    baseline_title=row.get("conception_rate_avg_120_title") or "",
                    href=f"/fertility/monthly?farm={farm}",
                    start=recent_start,
                    end=cut_off,
                    rows=served.get(farm, []),
                    service_word="services",
                    sire_aliases=sire_aliases,
                    tech_aliases=tech_aliases,
                )
            )
    if young_red:
        recent_start, cut_off, _prior, _last_pd = _conception_windows(
            today,
            _last_main_pd_day(db, farms=farms, lact_eq=0, min_checks=_pd_session_min(None)),
            _pd_lag_days(None),
        )
        served = _window_services(
            db,
            farms,
            today,
            lact_min=None,
            lact_eq=0,
            windows={farm: (recent_start, cut_off) for farm in farms},
        )
        rows: list[tuple[str, str, bool]] = []
        for farm in farms:
            rows.extend(served.get(farm, []))
        shown = young.get("conception_rate")
        baseline = young.get("conception_rate_avg_120")
        if isinstance(shown, int) and isinstance(baseline, int):
            issues.append(
                _issue(
                    farm="Youngstock",
                    shown=shown,
                    baseline=baseline,
                    value_title=young.get("conception_rate_title") or "",
                    baseline_title=young.get("conception_rate_avg_120_title") or "",
                    href="/events/breedings",
                    start=recent_start,
                    end=cut_off,
                    rows=rows,
                    service_word="heifer services",
                    sire_aliases=sire_aliases,
                    tech_aliases=tech_aliases,
                )
            )
    return issues
