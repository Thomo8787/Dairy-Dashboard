"""Home-dashboard BCMS / CTS reconcile health status."""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.cts_config import cts_farm_option_values, cts_is_ui_farm, cts_normalize_farm_key
from services.database import StockPurchaseAnimal
from services.cts_client import normalize_cts_etag
from services.cts_movements import active_awaiting_cts_etags
from services.cts_reconcile import reconcile_farm

HealthLevel = Literal["green", "yellow", "red", "unknown"]

# Sales entered in DairyComp after the events export can drop out of inventory
# before the next events pull. CTS-only with no SOLD/DIED is a pending event:
# green for 0–1 days, red from day 2.

# Per-kind send-urgency thresholds: (yellow_at_days, red_at_days).
_URGENCY_THRESHOLDS: dict[str, tuple[int, int]] = {
    "death": (3, 6),
    "birth": (3, 6),
    "sale": (2, 3),
    "move_on": (2, 3),
    "cts_only_awaiting_events": (2, 2),
}

_STATUS_RANK = {"green": 0, "yellow": 1, "red": 2}


def _purchases_by_etag(db: Session, farm: str) -> dict[str, dt.date]:
    out: dict[str, dt.date] = {}
    rows = db.scalars(
        select(StockPurchaseAnimal)
        .where(StockPurchaseAnimal.farm == farm.upper())
        .order_by(StockPurchaseAnimal.edat.desc())
    ).all()
    for row in rows:
        key = normalize_cts_etag(row.etag)
        if key and row.edat is not None and key not in out:
            out[key] = row.edat
    return out


def _age_days(value: dt.date | None, *, as_of: dt.date) -> int | None:
    if value is None:
        return None
    days = (as_of - value).days
    return days if days >= 0 else None


def _exit_kind(exit_event: str | None) -> str:
    if (exit_event or "").strip().upper() == "DIED":
        return "death"
    return "sale"


def _farm_mismatch_ages(
    db: Session,
    farm: str,
    recon: dict[str, Any],
    *,
    as_of: dt.date,
) -> list[dict[str, Any]]:
    """Classify each CTS↔inventory mismatch with an event age in days.

    Mismatches already reported to BCMS (Awaiting CTS) are excluded so the home
    widget turns green after a successful send, before overnight holding sync.
    """
    purchases = _purchases_by_etag(db, farm)
    awaiting_cts = active_awaiting_cts_etags(db, farm)
    items: list[dict[str, Any]] = []

    for row in recon.get("cts_only") or []:
        etag = normalize_cts_etag(row.get("etag"))
        if etag and etag in awaiting_cts:
            continue
        days = row.get("days_since_exit")
        if days is not None:
            items.append(
                {
                    "farm": farm,
                    "etag": etag or row.get("etag"),
                    "kind": _exit_kind(row.get("exit_event")),
                    "days": int(days),
                    "explainable": True,
                }
            )
        elif row.get("awaiting_events"):
            pending_days = row.get("awaiting_events_days")
            items.append(
                {
                    "farm": farm,
                    "etag": etag or row.get("etag"),
                    "kind": "cts_only_awaiting_events",
                    "days": int(pending_days) if pending_days is not None else 0,
                    "explainable": True,
                }
            )
        else:
            items.append(
                {
                    "farm": farm,
                    "etag": etag or row.get("etag"),
                    "kind": "cts_only_unexplained",
                    "days": None,
                    "explainable": False,
                }
            )

    for row in recon.get("inventory_only") or []:
        etag = normalize_cts_etag(row.get("etag")) or (row.get("etag") or "")
        if etag and normalize_cts_etag(etag) in awaiting_cts:
            continue
        purchase_date = purchases.get(normalize_cts_etag(etag) or etag)
        if purchase_date is not None:
            days = _age_days(purchase_date, as_of=as_of)
            items.append(
                {
                    "farm": farm,
                    "etag": etag,
                    "kind": "move_on",
                    "days": days,
                    "explainable": days is not None,
                }
            )
            continue
        days = row.get("age_days")
        if days is not None:
            items.append(
                {
                    "farm": farm,
                    "etag": etag,
                    "kind": "birth",
                    "days": int(days),
                    "explainable": True,
                }
            )
        else:
            items.append(
                {
                    "farm": farm,
                    "etag": etag,
                    "kind": "inventory_only_unexplained",
                    "days": None,
                    "explainable": False,
                }
            )

    return items


def _item_status(kind: str, days: int) -> HealthLevel:
    yellow_at, red_at = _URGENCY_THRESHOLDS.get(kind, (3, 3))
    if days >= red_at:
        return "red"
    if days >= yellow_at:
        return "yellow"
    return "green"


def _status_from_ages(items: list[dict[str, Any]]) -> tuple[HealthLevel, str, int | None]:
    """Return (level, label, max_days) from send-urgency bands per mismatch kind."""
    if not items:
        return "green", "Healthy", None

    unexplained = [i for i in items if not i.get("explainable") or i.get("days") is None]
    if unexplained:
        return "red", "Unhealthy", None

    worst: HealthLevel = "green"
    max_days = 0
    for item in items:
        days = int(item["days"])
        max_days = max(max_days, days)
        level = _item_status(str(item.get("kind") or ""), days)
        if _STATUS_RANK[level] > _STATUS_RANK[worst]:
            worst = level

    if worst == "green":
        return "green", "Healthy", max_days
    if worst == "yellow":
        return "yellow", "Attention", max_days
    return "red", "Unhealthy", max_days


def get_bcms_health(
    db: Session,
    *,
    farms: list[str] | None = None,
    as_of: dt.date | None = None,
) -> dict[str, Any]:
    """Aggregate BCMS reconcile health for the home dashboard widget."""
    today = as_of or dt.date.today()
    raw = [cts_normalize_farm_key(f) for f in (farms or list(cts_farm_option_values()))]
    seen: set[str] = set()
    farm_keys: list[str] = []
    for farm in raw:
        if farm in seen or not cts_is_ui_farm(farm):
            continue
        seen.add(farm)
        farm_keys.append(farm)

    farm_summaries: list[dict[str, Any]] = []
    all_items: list[dict[str, Any]] = []
    any_snapshot = False

    for farm in farm_keys:
        recon = reconcile_farm(db, farm)
        items = _farm_mismatch_ages(db, farm, recon, as_of=today)
        level, label, max_days = _status_from_ages(items)
        cts_count = int(recon.get("cts_count") or 0)
        inv_count = int(recon.get("inventory_count") or 0)
        if cts_count > 0:
            any_snapshot = True
        farm_summaries.append(
            {
                "farm": farm,
                "status": level,
                "label": label,
                "max_days": max_days,
                "mismatch_count": len(items),
                "cts_only_count": recon.get("cts_only_count") or 0,
                "inventory_only_count": recon.get("inventory_only_count") or 0,
                "matched_count": recon.get("matched_count") or 0,
                "synced_at": recon.get("synced_at"),
                "has_snapshot": cts_count > 0,
                "inventory_count": inv_count,
            }
        )
        all_items.extend(items)

    if not any_snapshot and not all_items:
        # No CTS data pulled yet — don't pretend everything is fine.
        overall: HealthLevel = "unknown"
        overall_label = "No CTS sync"
        overall_max: int | None = None
    else:
        overall, overall_label, overall_max = _status_from_ages(all_items)

    return {
        "status": overall,
        "label": overall_label,
        "max_days": overall_max,
        "mismatch_count": len(all_items),
        "href": "/bcms/record-movements",
        "farms": farm_summaries,
        "as_of": today.isoformat(),
    }
