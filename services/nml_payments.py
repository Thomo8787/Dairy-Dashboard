"""Loads Müller selected for payment, from the Results Notification PDF."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select

from services.database import NmlPaymentLoad, get_session
from services.farms import HERD_FARM_OPTIONS


def save_payment_loads(records: list[dict[str, Any]]) -> int:
    """Insert or update payment loads. Returns how many rows were written."""
    if not records:
        return 0
    written = 0
    with get_session() as db:
        for record in records:
            producer_ref = (record.get("producer_ref") or "").strip()
            sample_date = record.get("sample_date")
            sample_id = (record.get("sample_id") or "").strip()
            if not producer_ref or sample_date is None or not sample_id:
                continue
            row = db.scalar(
                select(NmlPaymentLoad).where(
                    NmlPaymentLoad.producer_ref == producer_ref,
                    NmlPaymentLoad.sample_date == sample_date,
                    NmlPaymentLoad.sample_id == sample_id,
                )
            )
            if row is None:
                row = NmlPaymentLoad(
                    producer_ref=producer_ref,
                    sample_date=sample_date,
                    sample_id=sample_id,
                )
                db.add(row)
            row.farm = record.get("farm")
            row.vat_order = record.get("vat_order")
            row.butterfat_pct = record.get("butterfat_pct")
            row.protein_pct = record.get("protein_pct")
            row.scc = record.get("scc")
            row.bactoscan = record.get("bactoscan")
            row.urea_pct = record.get("urea_pct")
            row.fpd = record.get("fpd")
            row.antibiotic_pass = record.get("antibiotic_pass")
            row.report_month = record.get("report_month")
            row.report_date = record.get("report_date")
            row.source_message_id = record.get("source_message_id")
            row.source_file = record.get("source_file")
            row.imported_at = dt.datetime.now(dt.timezone.utc)
            written += 1
    return written


def list_payment_loads(
    *,
    farms: list[str] | None = None,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
) -> dict[str, Any]:
    """Payment loads for the selected farms, oldest date first."""
    selected = [code.upper() for code in (farms or list(HERD_FARM_OPTIONS)) if code]
    if not selected:
        selected = list(HERD_FARM_OPTIONS)
    with get_session() as db:
        query = select(NmlPaymentLoad).where(NmlPaymentLoad.farm.in_(selected))
        if date_from is not None:
            query = query.where(NmlPaymentLoad.sample_date >= date_from)
        if date_to is not None:
            query = query.where(NmlPaymentLoad.sample_date <= date_to)
        rows = db.scalars(query.order_by(NmlPaymentLoad.sample_date, NmlPaymentLoad.sample_id)).all()
        payload = [_row_out(row) for row in rows]
    return {
        "rows": payload,
        "total": len(payload),
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
    }


def _row_out(row: NmlPaymentLoad) -> dict[str, Any]:
    return {
        "farm": row.farm,
        "sample_date": row.sample_date.isoformat() if row.sample_date else None,
        "sample_id": row.sample_id,
        "vat_order": row.vat_order or "",
        "butterfat_pct": row.butterfat_pct,
        "protein_pct": row.protein_pct,
        "scc": row.scc,
        "bactoscan": row.bactoscan,
        "urea_pct": row.urea_pct,
        "fpd": row.fpd,
        "antibiotic_pass": row.antibiotic_pass,
    }
