"""Run events + births + inventory imports from DCEXPORT OneDrive folders."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from services.herd_birth_import import import_herd_births
from services.herd_events_import import import_cow_events
from services.herd_inventory_import import import_herd_inventory
from services.herd_onedrive import discover_dcexport_files, herd_import_configured

logger = logging.getLogger(__name__)


def import_herd_exports(db: Session, *, force: bool = False) -> dict[str, Any]:
    if not herd_import_configured():
        raise ValueError(
            "Herd import is not configured. Set ONEDRIVE_SHARE_URL / ONEDRIVE_USER "
            "or LOCAL_HERD_EXPORT_DIR."
        )

    discovered = discover_dcexport_files()
    farms_found = sorted({item["farm"] for item in discovered})
    logger.info(
        "DCEXPORT discovery: %s file(s) for farms %s",
        len(discovered),
        ", ".join(farms_found) or "(none)",
    )

    events = import_cow_events(db, force=force)
    births = import_herd_births(db, force=force)
    inventory = import_herd_inventory(db, force=force)
    return {
        "farms_found": farms_found,
        "files": [
            {"farm": item["farm"], "kind": item["kind"], "path": item["relative_path"]}
            for item in discovered
        ],
        "events": events,
        "births": births,
        "inventory": inventory,
    }


def format_herd_import_summary(result: dict[str, Any]) -> str:
    farms = result.get("farms_found") or []
    farm_label = ", ".join(farms) if farms else "no DCEXPORT folders"
    events = result.get("events") or {}
    births = result.get("births") or {}
    inventory = result.get("inventory") or {}
    return (
        f"Herd import ({farm_label}) — "
        f"events {events.get('rows_imported', 0)} rows "
        f"({', '.join(events.get('farms_imported') or ['none'])}); "
        f"births {births.get('rows_imported', 0)}; "
        f"inventory {inventory.get('rows_imported', 0)}."
    )
