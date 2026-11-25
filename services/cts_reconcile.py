"""Sync CTS cattle-on-holding and reconcile against herd inventory."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from services.cts_config import (
    cts_farm_option_values,
    cts_herd_farms_for,
    cts_is_ui_farm,
    cts_normalize_farm_key,
)
from services.database import CowEvent, CtsOnHolding, CtsSyncRun, HerdInventory
from services.cts_client import (
    CtsError,
    cts_status,
    list_cattle_on_holding,
    normalize_cts_etag,
)
from services.cts_movements import (
    active_awaiting_cts_etags,
    archive_confirmed_movements,
    requeue_stale_awaiting_movements,
)

logger = logging.getLogger(__name__)

_EXIT_EVENTS = ("SOLD", "DIED")


def _age_days(dob: dt.date | None, *, as_of: dt.date | None = None) -> int | None:
    if dob is None:
        return None
    days = ((as_of or dt.date.today()) - dob).days
    return days if days >= 0 else None


def _age_months(dob: dt.date | None, *, as_of: dt.date | None = None) -> int | None:
    days = _age_days(dob, as_of=as_of)
    if days is None:
        return None
    return days // 30


def _latest_import_at(db: Session, model: type, farms: tuple[str, ...] | str) -> dt.datetime | None:
    farm_keys = cts_herd_farms_for(farms) if isinstance(farms, str) else farms
    return db.scalar(
        select(func.max(model.import_timestamp)).where(model.farm.in_(farm_keys))
    )


def cts_only_awaiting_events_days(
    db: Session,
    farm: str,
    *,
    as_of: dt.date | None = None,
) -> int | None:
    """Days to wait for a SOLD/DIED on a CTS-only animal (pending event).

    Inventory often drops a sold animal before the next events pull. Age from
    the last events import while inventory is newer; otherwise age from the
    last inventory import (how long they have been confirmed off the herd).
    Returns None when inventory has never been imported.
    """
    today = as_of or dt.date.today()
    herd_farms = cts_herd_farms_for(farm)
    events_at = _latest_import_at(db, CowEvent, herd_farms)
    inventory_at = _latest_import_at(db, HerdInventory, herd_farms)
    if inventory_at is None:
        return None
    if events_at is None or inventory_at > events_at:
        if events_at is None:
            return 0
        return _age_days(events_at.date(), as_of=today) or 0
    return _age_days(inventory_at.date(), as_of=today) or 0


def _inventory_etags(db: Session, farm: str) -> dict[str, dict[str, Any]]:
    """Map normalized etag -> inventory summary for a CTS farm (unions shared herds)."""
    as_of = dt.date.today()
    herd_farms = cts_herd_farms_for(farm)
    rows = db.execute(
        select(
            HerdInventory.etag,
            HerdInventory.cow_id,
            HerdInventory.sbrd,
            HerdInventory.gender,
            HerdInventory.bdat,
            HerdInventory.category,
            HerdInventory.farm,
        ).where(HerdInventory.farm.in_(herd_farms))
    ).all()
    out: dict[str, dict[str, Any]] = {}
    for etag, cow_id, sbrd, gender, bdat, category, inv_farm in rows:
        key = normalize_cts_etag(etag)
        if not key:
            continue
        # Prefer first row; duplicates rare
        out.setdefault(
            key,
            {
                "etag": key,
                "cow_id": cow_id or "",
                "breed": (sbrd or "").strip().upper(),
                "gender": gender or "",
                "dob": bdat.isoformat() if bdat else None,
                "category": category or "",
                "age_months": _age_months(bdat, as_of=as_of),
                "age_days": _age_days(bdat, as_of=as_of),
                "farm": (inv_farm or "").strip().upper(),
            },
        )
    return out


def _cts_rows(db: Session, farm: str) -> list[CtsOnHolding]:
    farm_key = cts_normalize_farm_key(farm)
    return list(
        db.scalars(
            select(CtsOnHolding)
            .where(CtsOnHolding.farm == farm_key)
            .order_by(CtsOnHolding.etag)
        )
    )


def _exit_by_etag(db: Session, farm: str) -> dict[str, dict[str, Any]]:
    """Latest SOLD/DIED event per normalized etag for a CTS farm (unions shared herds)."""
    as_of = dt.date.today()
    herd_farms = cts_herd_farms_for(farm)
    rows = db.execute(
        select(CowEvent.etag, CowEvent.event, CowEvent.event_date).where(
            CowEvent.farm.in_(herd_farms),
            CowEvent.event.in_(_EXIT_EVENTS),
            CowEvent.event_date.isnot(None),
        )
    ).all()
    out: dict[str, dict[str, Any]] = {}
    for etag, event, event_date in rows:
        key = normalize_cts_etag(etag)
        if not key or event_date is None:
            continue
        prev = out.get(key)
        if prev is not None and prev["_exit_date"] >= event_date:
            continue
        days = (as_of - event_date).days
        out[key] = {
            "_exit_date": event_date,
            "exit_event": event,
            "exit_date": event_date.isoformat(),
            "days_since_exit": days if days >= 0 else None,
        }
    # Drop internal compare helper before returning.
    for payload in out.values():
        payload.pop("_exit_date", None)
    return out


def reconcile_farm(db: Session, farm: str) -> dict[str, Any]:
    """Compare latest CTS snapshot to herd_inventory for one CTS UI farm."""
    farm_key = cts_normalize_farm_key(farm)
    herd_farms = cts_herd_farms_for(farm_key)
    cts_animals = _cts_rows(db, farm_key)
    inv = _inventory_etags(db, farm_key)
    exits = _exit_by_etag(db, farm_key)

    cts_by_etag: dict[str, CtsOnHolding] = {}
    for row in cts_animals:
        key = normalize_cts_etag(row.etag)
        if key:
            cts_by_etag.setdefault(key, row)
    cts_keys = set(cts_by_etag)
    inv_keys = set(inv)

    matched_keys = sorted(cts_keys & inv_keys)
    cts_only_keys = sorted(cts_keys - inv_keys)
    inv_only_keys = sorted(inv_keys - cts_keys)

    last_sync = db.scalar(
        select(CtsSyncRun)
        .where(CtsSyncRun.farm == farm_key, CtsSyncRun.status == "ok")
        .order_by(CtsSyncRun.finished_at.desc())
        .limit(1)
    )
    synced_at = None
    if cts_animals:
        synced_at = max(
            (row.synced_at for row in cts_animals if row.synced_at),
            default=None,
        )
    if synced_at is None and last_sync and last_sync.finished_at:
        synced_at = last_sync.finished_at

    inventory_imported_at = db.scalar(
        select(func.max(HerdInventory.import_timestamp)).where(
            HerdInventory.farm.in_(herd_farms)
        )
    )

    as_of = dt.date.today()
    awaiting_events_days = cts_only_awaiting_events_days(db, farm_key, as_of=as_of)
    awaiting_cts = active_awaiting_cts_etags(db, farm_key)

    def _cts_dict(key: str, row: CtsOnHolding) -> dict[str, Any]:
        exit_info = exits.get(key) or {}
        exit_event = exit_info.get("exit_event")
        days_since_exit = exit_info.get("days_since_exit")
        awaiting_events = (
            exit_event is None
            and days_since_exit is None
            and awaiting_events_days is not None
        )
        return {
            "etag": key,
            "breed": row.breed,
            "sex": row.sex,
            "dob": row.dob.isoformat() if row.dob else None,
            "on_date": row.on_date.isoformat() if row.on_date else None,
            "age_months": _age_months(row.dob, as_of=as_of),
            "age_days": _age_days(row.dob, as_of=as_of),
            "exit_event": exit_event,
            "exit_date": exit_info.get("exit_date"),
            "days_since_exit": days_since_exit,
            "awaiting_events": awaiting_events,
            "awaiting_events_days": awaiting_events_days if awaiting_events else None,
            "awaiting_cts": key in awaiting_cts,
        }

    inv_only_rows = []
    for key in inv_only_keys:
        row = dict(inv[key])
        row["awaiting_cts"] = key in awaiting_cts
        inv_only_rows.append(row)

    return {
        "farm": farm_key,
        "synced_at": synced_at.isoformat() if synced_at else None,
        "inventory_imported_at": (
            inventory_imported_at.isoformat() if inventory_imported_at else None
        ),
        "cts_count": len(cts_keys),
        "inventory_count": len(inv_keys),
        "matched_count": len(matched_keys),
        "cts_only_count": len(cts_only_keys),
        "inventory_only_count": len(inv_only_keys),
        # Only mismatches are returned as row lists (matched can be thousands).
        "cts_only": [_cts_dict(k, cts_by_etag[k]) for k in cts_only_keys],
        "inventory_only": inv_only_rows,
        "last_sync": (
            {
                "id": last_sync.id,
                "status": last_sync.status,
                "animal_count": last_sync.animal_count,
                "finished_at": (
                    last_sync.finished_at.isoformat() if last_sync.finished_at else None
                ),
                "error_message": last_sync.error_message,
            }
            if last_sync
            else None
        ),
    }


def replace_on_holding_snapshot(
    db: Session,
    *,
    farm: str,
    animals: list,
    sync_run_id: int | None,
    synced_at: dt.datetime | None = None,
) -> int:
    """Replace CTS on-holding rows for a farm with a new snapshot."""
    farm_key = farm.upper()
    when = synced_at or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    db.execute(delete(CtsOnHolding).where(CtsOnHolding.farm == farm_key))
    rows = [
        CtsOnHolding(
            farm=farm_key,
            etag=animal.etag,
            breed=animal.breed or "",
            sex=animal.sex or "",
            dob=animal.dob,
            on_date=animal.on_date,
            synced_at=when,
            sync_run_id=sync_run_id,
        )
        for animal in animals
        if animal.etag
    ]
    db.add_all(rows)
    db.flush()
    return len(rows)


def sync_farm(
    db: Session,
    farm: str,
    *,
    source: str = "manual",
) -> dict[str, Any]:
    """Pull cattle on holding from CTS and store snapshot, then reconcile."""
    farm_key = cts_normalize_farm_key(farm)
    if not cts_is_ui_farm(farm_key):
        raise CtsError(f"Unsupported farm: {farm}")

    run = CtsSyncRun(
        farm=farm_key,
        status="running",
        animal_count=0,
        source=source,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    run_id = run.id

    try:
        animals = list_cattle_on_holding(farm_key)
        count = replace_on_holding_snapshot(
            db,
            farm=farm_key,
            animals=animals,
            sync_run_id=run_id,
        )
        run = db.get(CtsSyncRun, run_id)
        if run is not None:
            run.status = "ok"
            run.animal_count = count
            run.finished_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
            run.error_message = None
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        run = db.get(CtsSyncRun, run_id)
        if run is not None:
            run.status = "error"
            run.animal_count = 0
            run.finished_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
            run.error_message = f"{type(exc).__name__}: {exc}"
            db.commit()
        logger.exception("CTS sync failed farm=%s", farm_key)
        raise CtsError(str(exc)) from exc

    archived = archive_confirmed_movements(db, farm_key)
    requeue = requeue_stale_awaiting_movements(db, farm_key)
    result = reconcile_farm(db, farm_key)
    result["archived_confirmed"] = archived.get("archived_count", 0)
    result["requeued_stale_awaiting"] = requeue.get("requeued_count", 0)
    return result


def sync_farms(
    db: Session,
    farms: list[str] | None = None,
    *,
    source: str = "manual",
) -> dict[str, Any]:
    """Sync one or more farms; skip unconfigured with warnings."""
    status = cts_status()
    if not status["ddts_configured"]:
        raise CtsError(
            "CTS DDTS is not configured. Set CTS_DDTS_USERNAME and CTS_DDTS_PASSWORD."
        )

    requested = [
        cts_normalize_farm_key(f) for f in (farms or list(status["ready_farms"]))
    ]
    # Deduplicate after ALH/BNK normalize so shared holding is synced once.
    seen: set[str] = set()
    ordered: list[str] = []
    for farm in requested:
        if farm in seen:
            continue
        seen.add(farm)
        ordered.append(farm)
    requested = ordered
    if not requested:
        requested = list(cts_farm_option_values())

    results: list[dict[str, Any]] = []
    warnings: list[str] = []
    for farm in requested:
        if farm not in status["farms"] or not status["farms"][farm]:
            parts = (status.get("farm_parts") or {}).get(farm) or {}
            missing = [
                name
                for name, ok in (
                    ("username", parts.get("username")),
                    ("password", parts.get("password")),
                    ("holding", parts.get("holding")),
                )
                if not ok
            ]
            detail = (
                f"missing {', '.join(missing)}"
                if missing
                else "credentials/holding incomplete"
            )
            warnings.append(
                f"{farm}: CTS {detail} — skipped "
                f"(set CTS_CTWS_USERNAME_{farm}, CTS_CTWS_PASSWORD_{farm}, "
                f"CTS_HOLDING_{farm} on the web service and redeploy)"
            )
            continue
        try:
            results.append(sync_farm(db, farm, source=source))
        except CtsError as exc:
            warnings.append(f"{farm}: {exc}")

    return {
        "ok": bool(results) and not any(
            w for w in warnings if ": " in w and "skipped" not in w
        ),
        "status": status,
        "results": results,
        "warnings": warnings,
    }


def reconcile_farms(
    db: Session,
    farms: list[str] | None = None,
) -> dict[str, Any]:
    """Reconcile from stored snapshots only (no live CTS call)."""
    raw = [cts_normalize_farm_key(f) for f in (farms or list(cts_farm_option_values()))]
    seen: set[str] = set()
    targets: list[str] = []
    for farm in raw:
        if farm in seen:
            continue
        seen.add(farm)
        targets.append(farm)
    results = [reconcile_farm(db, farm) for farm in targets]
    return {
        "status": cts_status(),
        "results": results,
        "snapshot_counts": {
            farm: db.scalar(
                select(func.count())
                .select_from(CtsOnHolding)
                .where(CtsOnHolding.farm == farm)
            )
            or 0
            for farm in targets
        },
    }
