"""Derive purchased animals from cow events (EDAT != BDAT)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, delete, func, select
from sqlalchemy.orm import Session

from services.database import (
    STOCK_GROUP_BEEF,
    STOCK_GROUP_COWS,
    STOCK_GROUP_YOUNGSTOCK,
    CowEvent,
    StockPurchaseAnimal,
)
from services.herd_import_utils import CATEGORY_DAIRY, category_from_birth


def classify_purchase_stock_group(
    lact: int | None,
    cbrd: int | None,
    gndr: str | None,
    *,
    edat=None,
    fdat=None,
    min_lact: int | None = None,
) -> str:
    """Classify a purchased animal using status at arrival, not current lactation."""
    if category_from_birth(cbrd, gndr) != CATEGORY_DAIRY:
        return STOCK_GROUP_BEEF

    arrival_lact = min_lact if min_lact is not None else lact
    if arrival_lact == 0:
        return STOCK_GROUP_YOUNGSTOCK

    # First freshening on this farm on/after arrival => purchased heifer.
    if lact == 1 and edat is not None and fdat is not None and fdat >= edat:
        return STOCK_GROUP_YOUNGSTOCK

    if lact is not None and lact > 0:
        return STOCK_GROUP_COWS
    return STOCK_GROUP_YOUNGSTOCK


def _purchase_event_filter():
    return and_(
        CowEvent.etag.isnot(None),
        CowEvent.edat.isnot(None),
        CowEvent.bdat.isnot(None),
        CowEvent.edat != CowEvent.bdat,
    )


def _fetch_purchase_source_rows(db: Session) -> list[CowEvent]:
    purchase_filter = _purchase_event_filter()

    first_event = (
        select(
            CowEvent.farm,
            CowEvent.etag,
            func.min(CowEvent.event_date).label("first_event_date"),
        )
        .where(purchase_filter)
        .group_by(CowEvent.farm, CowEvent.etag)
        .subquery("first_event")
    )

    first_row = (
        select(func.min(CowEvent.id).label("event_id"))
        .join(
            first_event,
            and_(
                CowEvent.farm == first_event.c.farm,
                CowEvent.etag == first_event.c.etag,
                CowEvent.event_date == first_event.c.first_event_date,
            ),
        )
        .where(purchase_filter)
        .group_by(CowEvent.farm, CowEvent.etag)
        .subquery("first_row")
    )

    return list(
        db.scalars(select(CowEvent).where(CowEvent.id.in_(select(first_row.c.event_id)))).all()
    )


def _fetch_min_lact_by_purchase(db: Session) -> dict[tuple[str, str], int | None]:
    rows = db.execute(
        select(
            CowEvent.farm,
            CowEvent.etag,
            func.min(CowEvent.lact),
        )
        .where(_purchase_event_filter())
        .group_by(CowEvent.farm, CowEvent.etag)
    ).all()
    return {
        (str(farm), str(etag)): int(min_lact) if min_lact is not None else None
        for farm, etag, min_lact in rows
    }


def rebuild_stock_purchases(db: Session) -> dict[str, Any]:
    """Replace derived purchase animals from current cow_events."""
    import_time = datetime.now(timezone.utc)
    source_rows = _fetch_purchase_source_rows(db)
    min_lact_by_animal = _fetch_min_lact_by_purchase(db)

    mappings: list[dict[str, Any]] = []
    by_farm: dict[str, int] = {}
    by_stock_group: dict[str, int] = {}

    for row in source_rows:
        farm = str(row.farm)
        etag = str(row.etag)
        stock_group = classify_purchase_stock_group(
            row.lact,
            row.cbrd,
            row.gndr,
            edat=row.edat,
            fdat=row.fdat,
            min_lact=min_lact_by_animal.get((farm, etag)),
        )
        mappings.append(
            {
                "farm": farm,
                "etag": etag,
                "edat": row.edat,
                "bdat": row.bdat,
                "lact": row.lact,
                "cbrd": row.cbrd,
                "gndr": row.gndr,
                "stock_group": stock_group,
                "import_timestamp": import_time,
            }
        )
        by_farm[farm] = by_farm.get(farm, 0) + 1
        by_stock_group[stock_group] = by_stock_group.get(stock_group, 0) + 1

    db.execute(delete(StockPurchaseAnimal))
    if mappings:
        db.bulk_insert_mappings(StockPurchaseAnimal, mappings)

    return {
        "rows_imported": len(mappings),
        "farm_counts": by_farm,
        "stock_group_counts": by_stock_group,
        "imported_at": import_time.isoformat(timespec="seconds"),
    }


def ensure_stock_purchases(db: Session) -> dict[str, Any] | None:
    """Rebuild purchases when the table is empty but candidate events exist."""
    existing = db.scalar(select(func.count()).select_from(StockPurchaseAnimal)) or 0
    if existing:
        return None
    candidate = db.scalar(select(CowEvent.id).where(_purchase_event_filter()).limit(1))
    if candidate is None:
        return None
    return rebuild_stock_purchases(db)
