"""Monthly Brassington invoice check against on-farm deaths.

OFS collections are left off, the same way as the passport checklist.
Age bands follow the invoice. Ages between those bands are kept so every
death still has a row. A month can be archived once the quantities are agreed.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.database import (
    FallenStockInvoiceArchive,
    FallenStockInvoiceQty,
    FallenStockStillborn,
)
from services.fallen_stock import _load_deaths
from services.farms import HERD_FARM_OPTIONS

# Completed calendar months: 0 is the first month of life.
# 4-6 starts at 4 months. Over 48 starts on the 48-month birthday.
AGE_BANDS: tuple[tuple[str, str, int | None, int | None], ...] = (
    ("0-1", "0-1 month", 0, 1),
    ("1-4", "1-4 months", 1, 4),
    ("4-6", "4-6 months", 4, 6),
    ("6-24", "6-24 months", 6, 24),
    ("24-48", "24-48 months", 24, 48),
    ("over-48", "Over 48 months", 48, None),
    ("unknown", "No birth date", None, None),
)
BAND_IDS = {band[0] for band in AGE_BANDS}
STILLBORN_BAND = "0-1"


def completed_months(born: dt.date, on: dt.date) -> int | None:
    """Whole months from birth to the death. None when the death is before birth."""
    months = (on.year - born.year) * 12 + (on.month - born.month)
    if on.day < born.day:
        months -= 1
    if months < 0:
        return None
    return months


def band_for_age(months: int | None) -> str:
    if months is None:
        return "unknown"
    for band_id, _label, start, end in AGE_BANDS:
        if start is None:
            continue
        if months >= start and (end is None or months < end):
            return band_id
    return "unknown"


def parse_month(value: str | None) -> dt.date | None:
    text = (value or "").strip()
    try:
        year_text, month_text = text.split("-", 1)
        year = int(year_text)
        month = int(month_text)
        return dt.date(year, month, 1)
    except (TypeError, ValueError):
        return None


def _next_month(month: dt.date) -> dt.date:
    if month.month == 12:
        return dt.date(month.year + 1, 1, 1)
    return dt.date(month.year, month.month + 1, 1)


def _empty_grid() -> dict[str, dict[str, int]]:
    return {band_id: {farm: 0 for farm in HERD_FARM_OPTIONS} for band_id, *_rest in AGE_BANDS}


def _blank_invoice() -> dict[str, dict[str, int | None]]:
    return {band_id: {farm: None for farm in HERD_FARM_OPTIONS} for band_id, *_rest in AGE_BANDS}


def _live_month(db: Session, month: dt.date) -> dict[str, Any]:
    end = _next_month(month)
    counts = _empty_grid()
    for event in _load_deaths(db, list(HERD_FARM_OPTIONS)):
        event_date = event.event_date
        if event_date is None or event_date < month or event_date >= end:
            continue
        born = event.bdat
        if isinstance(born, dt.datetime):
            born = born.date()
        band = band_for_age(completed_months(born, event_date) if born else None)
        farm = event.farm
        if farm in counts[band]:
            counts[band][farm] += 1

    stillborns = db.scalars(
        select(FallenStockStillborn)
        .where(FallenStockStillborn.born_on >= month, FallenStockStillborn.born_on < end)
        .order_by(FallenStockStillborn.born_on, FallenStockStillborn.farm, FallenStockStillborn.id)
    ).all()
    stillborn_rows = []
    for row in stillborns:
        if row.farm in counts[STILLBORN_BAND]:
            counts[STILLBORN_BAND][row.farm] += 1
        stillborn_rows.append(
            {
                "id": row.id,
                "farm": row.farm,
                "born_on": row.born_on.isoformat(),
                "note": row.note or "",
            }
        )

    invoice = _blank_invoice()
    quantities = db.scalars(
        select(FallenStockInvoiceQty).where(FallenStockInvoiceQty.month == month)
    ).all()
    for row in quantities:
        if row.band in invoice and row.farm in invoice[row.band]:
            invoice[row.band][row.farm] = row.quantity

    return {
        "month": month.isoformat()[:7],
        "archived": False,
        "archived_on": None,
        "bands": [{"id": band_id, "label": label} for band_id, label, *_rest in AGE_BANDS],
        "farms": list(HERD_FARM_OPTIONS),
        "counts": counts,
        "invoice": invoice,
        "stillborns": stillborn_rows,
    }


def _archive_row(db: Session, month: dt.date) -> FallenStockInvoiceArchive | None:
    return db.scalar(
        select(FallenStockInvoiceArchive).where(FallenStockInvoiceArchive.month == month)
    )


def invoice_month(db: Session, month: dt.date) -> dict[str, Any]:
    archived = _archive_row(db, month)
    if archived is None:
        return _live_month(db, month)
    payload = json.loads(archived.snapshot)
    payload["archived"] = True
    payload["archived_on"] = archived.archived_on.isoformat()
    return payload


def archived_months(db: Session) -> list[dict[str, str]]:
    rows = db.scalars(
        select(FallenStockInvoiceArchive).order_by(FallenStockInvoiceArchive.month.desc())
    ).all()
    return [
        {"month": row.month.isoformat()[:7], "archived_on": row.archived_on.isoformat()}
        for row in rows
    ]


def _require_open(db: Session, month: dt.date) -> None:
    if _archive_row(db, month) is not None:
        raise ValueError("That month is archived.")


def set_invoice_quantity(
    db: Session,
    *,
    month: dt.date,
    farm: str,
    band: str,
    quantity: int | None,
) -> None:
    if farm not in HERD_FARM_OPTIONS:
        raise ValueError("Choose a farm.")
    if band not in BAND_IDS:
        raise ValueError("Choose an age band.")
    if quantity is not None and quantity < 0:
        raise ValueError("Quantity cannot be negative.")
    _require_open(db, month)
    row = db.scalar(
        select(FallenStockInvoiceQty).where(
            FallenStockInvoiceQty.month == month,
            FallenStockInvoiceQty.farm == farm,
            FallenStockInvoiceQty.band == band,
        )
    )
    if quantity is None:
        if row is not None:
            db.delete(row)
        return
    if row is None:
        db.add(FallenStockInvoiceQty(month=month, farm=farm, band=band, quantity=quantity))
    else:
        row.quantity = quantity


def add_stillborn(db: Session, *, farm: str, born_on: dt.date, note: str) -> dict[str, Any]:
    if farm not in HERD_FARM_OPTIONS:
        raise ValueError("Choose a farm.")
    month = born_on.replace(day=1)
    _require_open(db, month)
    row = FallenStockStillborn(farm=farm, born_on=born_on, note=(note or "").strip()[:255])
    db.add(row)
    db.flush()
    return {"id": row.id, "farm": row.farm, "born_on": row.born_on.isoformat(), "note": row.note}


def delete_stillborn(db: Session, stillborn_id: int) -> None:
    row = db.get(FallenStockStillborn, stillborn_id)
    if row is None:
        raise ValueError("That stillborn calf is no longer on the list.")
    _require_open(db, row.born_on.replace(day=1))
    db.delete(row)


def archive_invoice_month(
    db: Session,
    month: dt.date,
    user_id: int | None,
    quantities: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if _archive_row(db, month) is not None:
        raise ValueError("That month is already archived.")
    for item in quantities or []:
        if not isinstance(item, dict):
            continue
        raw_qty = item.get("quantity")
        quantity = None if raw_qty is None or raw_qty == "" else int(raw_qty)
        set_invoice_quantity(
            db,
            month=month,
            farm=str(item.get("farm") or "").strip().upper(),
            band=str(item.get("band") or "").strip(),
            quantity=quantity,
        )
    payload = _live_month(db, month)
    payload["archived"] = True
    payload["archived_on"] = dt.date.today().isoformat()
    db.add(
        FallenStockInvoiceArchive(
            month=month,
            archived_on=dt.date.today(),
            archived_by_user_id=user_id,
            snapshot=json.dumps(payload),
        )
    )
    return payload


def unarchive_invoice_month(db: Session, month: dt.date) -> None:
    row = _archive_row(db, month)
    if row is None:
        raise ValueError("That month is not archived.")
    db.delete(row)
