"""Link Milk Flow rows to Rotary Entry ID rows for lag-phase timing."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

MATCH_WINDOW = timedelta(minutes=5)


def parse_hms(value: str | None) -> timedelta | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    parts = text.split(":")
    try:
        if len(parts) == 3:
            h, m, s = (int(float(p)) for p in parts)
            return timedelta(hours=h, minutes=m, seconds=s)
        if len(parts) == 2:
            m, s = (int(float(p)) for p in parts)
            return timedelta(minutes=m, seconds=s)
    except ValueError:
        return None
    return None


def _normalize_shift(raw: str | None) -> str:
    return (raw or "").strip().lower()


def _time_delta_seconds(start: timedelta, identification: timedelta) -> float:
    """Signed lag in seconds (milking start − identification), allowing midnight wrap."""
    day = 24 * 3600
    raw = (start - identification).total_seconds()
    # Prefer the smallest absolute interpretation within ±12h.
    candidates = (raw, raw - day, raw + day)
    return min(candidates, key=lambda v: abs(v))


def match_milk_flow_to_entry_ids(
    milk_rows: list[Any],
    entry_rows: list[Any],
    *,
    window: timedelta = MATCH_WINDOW,
) -> list[dict[str, Any]]:
    """
    1:1 match milk-flow rows to rotary entry IDs.

    Rules:
    - same cow number
    - same milking date (caller should already scope by date)
    - same shift when the entry row has a shift value
    - |identification_time − cow_milking_start_time| ≤ window (default 5 minutes)

    Returns list of match dicts with lag_seconds (start − identification).
    """
    window_secs = window.total_seconds()

    # Index entries by cow for fast lookup.
    by_cow: dict[str, list[tuple[int, Any, timedelta]]] = {}
    for idx, entry in enumerate(entry_rows):
        id_raw = (
            entry["identification_time"]
            if isinstance(entry, dict)
            else entry.identification_time
        )
        id_td = parse_hms(id_raw)
        if id_td is None:
            continue
        cow = str(
            entry["cow_number"] if isinstance(entry, dict) else entry.cow_number
        ).strip()
        by_cow.setdefault(cow, []).append((idx, entry, id_td))

    used_entry_indexes: set[int] = set()
    matches: list[dict[str, Any]] = []

    # Stable order: earlier milking starts first so earlier cows claim nearer IDs.
    ordered_milk = []
    for milk in milk_rows:
        start_raw = (
            milk["cow_milking_start_time"]
            if isinstance(milk, dict)
            else milk.cow_milking_start_time
        )
        start_td = parse_hms(start_raw)
        if start_td is None:
            continue
        ordered_milk.append((start_td, milk))
    ordered_milk.sort(key=lambda item: item[0].total_seconds())

    for start_td, milk in ordered_milk:
        cow = str(
            milk["cow_number"] if isinstance(milk, dict) else milk.cow_number
        ).strip()
        milk_shift = _normalize_shift(
            milk["shift"] if isinstance(milk, dict) else milk.shift
        )

        candidates = []
        for idx, entry, id_td in by_cow.get(cow, []):
            if idx in used_entry_indexes:
                continue
            entry_shift = _normalize_shift(
                entry["shift"] if isinstance(entry, dict) else entry.shift
            )
            if entry_shift and milk_shift and entry_shift != milk_shift:
                continue
            lag = _time_delta_seconds(start_td, id_td)
            if abs(lag) > window_secs:
                continue
            candidates.append((abs(lag), lag, idx, entry, id_td))

        if not candidates:
            continue

        candidates.sort(key=lambda item: (item[0], item[1]))
        abs_lag, lag, idx, entry, id_td = candidates[0]
        used_entry_indexes.add(idx)
        matches.append(
            {
                "milk": milk,
                "entry": entry,
                "lag_seconds": lag,
                "abs_lag_seconds": abs_lag,
                "cow_number": cow,
                "identification_time": (
                    entry["identification_time"]
                    if isinstance(entry, dict)
                    else entry.identification_time
                ),
                "cow_milking_start_time": (
                    milk["cow_milking_start_time"]
                    if isinstance(milk, dict)
                    else milk.cow_milking_start_time
                ),
            }
        )

    return matches
