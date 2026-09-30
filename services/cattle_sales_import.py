"""Import local cattle remittance PDFs into cattle_sale_lines."""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.blade_pdf import looks_like_blade_pdf, parse_blade_pdf
from services.cattle_sale_pdf import normalize_etag
from services.cattle_sales import EVENT_MATCH_WINDOW_DAYS, cattle_sales_exit_event_clause
from services.database import CattleSaleLine, CowEvent
from services.farms import HERD_FARM_OPTIONS
from services.market_drayton_pdf import looks_like_market_drayton_pdf, parse_market_drayton_pdf
from services.neilds_pdf import looks_like_neilds_pdf, parse_neilds_pdf
from services.pickstock_pdf import looks_like_pickstock_pdf, parse_pickstock_pdf
from services.warrendale_pdf import looks_like_warrendale_pdf, parse_warrendale_pdf

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_SALES_DIR = PROJECT_ROOT / "data" / "cattle_sales"


def _parse_sale_pdf(
    content: bytes,
    *,
    mailbox_farm: str | None,
    source_file: str | None,
    parser_hint: str | None = None,
) -> dict[str, Any]:
    from services.cattle_sale_pdf import extract_pdf_text

    text = extract_pdf_text(content)
    name = source_file or ""
    hint = (parser_hint or "").strip().lower()
    if hint == "neilds":
        return parse_neilds_pdf(content, mailbox_farm=mailbox_farm, source_file=source_file)
    if hint == "pickstock":
        return parse_pickstock_pdf(content, mailbox_farm=mailbox_farm, source_file=source_file)
    if hint in {"gamechanger", "blade"}:
        return parse_blade_pdf(content, mailbox_farm=mailbox_farm, source_file=source_file)
    if hint in {"market_drayton", "drayton"}:
        return parse_market_drayton_pdf(
            content, mailbox_farm=mailbox_farm, source_file=source_file
        )
    if looks_like_warrendale_pdf(text, name) or looks_like_warrendale_pdf("", name):
        return parse_warrendale_pdf(
            content,
            mailbox_farm=mailbox_farm,
            source_file=source_file,
        )
    if looks_like_blade_pdf(text, name) or looks_like_blade_pdf("", name):
        return parse_blade_pdf(
            content,
            mailbox_farm=mailbox_farm,
            source_file=source_file,
        )
    if looks_like_market_drayton_pdf(text, name) or looks_like_market_drayton_pdf("", name):
        return parse_market_drayton_pdf(
            content,
            mailbox_farm=mailbox_farm,
            source_file=source_file,
        )
    if looks_like_neilds_pdf(text, name) or looks_like_neilds_pdf("", name):
        return parse_neilds_pdf(
            content,
            mailbox_farm=mailbox_farm,
            source_file=source_file,
        )
    if looks_like_pickstock_pdf(text, name):
        return parse_pickstock_pdf(
            content,
            mailbox_farm=mailbox_farm,
            source_file=source_file,
        )
    return parse_pickstock_pdf(
        content,
        mailbox_farm=mailbox_farm,
        source_file=source_file,
    )


def _farm_from_filename(name: str) -> str | None:
    name_low = name.lower()
    markers = (
        ("aston lower hall", "ALH"),
        ("alh", "ALH"),
        ("bank farm", "BNK"),
        ("bnk", "BNK"),
        ("park hall", "SFR"),
        ("sfr", "SFR"),
        ("parkes", "PRK"),
        ("prk", "PRK"),
        ("cherry orchard", "COF"),
        ("cof", "COF"),
    )
    for marker, farm in markers:
        if marker in name_low:
            return farm
    return None


def _iter_local_pdfs() -> list[Path]:
    paths: list[Path] = []
    if LOCAL_SALES_DIR.is_dir():
        paths.extend(sorted(LOCAL_SALES_DIR.glob("*.pdf")))
    for pattern in (
        "FPF*.pdf",
        "FPF*.PDF",
        "T13*.pdf",
        "T13*.PDF",
        "T304*.pdf",
        "T304*.PDF",
        "*Sellers List*.pdf",
        "*Sellers List*.PDF",
        "PaymentAdvice*.pdf",
        "PaymentAdvice*.PDF",
        "PurchaseOrder*.pdf",
        "PurchaseOrder*.PDF",
        "PO26-*.pdf",
        "PO26-*.PDF",
    ):
        paths.extend(PROJECT_ROOT.glob(pattern))
    unique: dict[str, Path] = {}
    for path in paths:
        if path.name.startswith("~$"):
            continue
        unique[path.resolve().as_posix().lower()] = path
    return list(unique.values())


def _farm_for_etag(
    db: Session,
    etag: str,
    sale_date: dt.date,
) -> str | None:
    window_start = sale_date - dt.timedelta(days=EVENT_MATCH_WINDOW_DAYS)
    window_end = sale_date + dt.timedelta(days=EVENT_MATCH_WINDOW_DAYS)
    rows = db.scalars(
        select(CowEvent).where(
            cattle_sales_exit_event_clause(),
            CowEvent.event_date.isnot(None),
            CowEvent.event_date >= window_start,
            CowEvent.event_date <= window_end,
        )
    ).all()
    best: CowEvent | None = None
    best_delta: int | None = None
    for row in rows:
        if normalize_etag(row.etag) != etag or row.event_date is None:
            continue
        delta = abs((row.event_date - sale_date).days)
        if best is None or best_delta is None or delta < best_delta:
            best = row
            best_delta = delta
    if best is None:
        fallback = db.scalars(
            select(CowEvent)
            .where(CowEvent.etag == etag)
            .order_by(CowEvent.event_date.desc())
        ).first()
        best = fallback
    if best is None:
        return None
    farm = (best.farm or "").strip().upper()
    return farm if farm in HERD_FARM_OPTIONS else None


def _sale_values_match(row: CattleSaleLine, record: dict[str, Any]) -> bool:
    if abs((row.cold_weight_kg or 0.0) - float(record["cold_weight_kg"] or 0.0)) >= 0.005:
        return False
    if abs((row.amount_gbp or 0.0) - float(record["amount_gbp"] or 0.0)) >= 0.005:
        return False
    row_reject = row.reject_kg
    new_reject = record.get("reject_kg")
    if row_reject is None and new_reject is None:
        pass
    elif row_reject is None or new_reject is None:
        return False
    elif abs(float(row_reject) - float(new_reject)) >= 0.005:
        return False
    return row.kill_date == record.get("kill_date")


def _upsert(
    db: Session,
    parsed_by_key: dict[tuple[str, str, dt.date], dict[str, Any]],
) -> tuple[int, int]:
    if not parsed_by_key:
        return (0, 0)

    farms = {key[0] for key in parsed_by_key}
    etags = {key[1] for key in parsed_by_key}
    existing_rows = db.scalars(
        select(CattleSaleLine).where(
            CattleSaleLine.farm.in_(farms),
            CattleSaleLine.etag.in_(etags),
        )
    ).all()
    existing_by_key = {
        (row.farm, row.etag, row.sale_date): row for row in existing_rows
    }

    inserted = 0
    updated = 0
    for key, record in parsed_by_key.items():
        row = existing_by_key.get(key)
        if row is None:
            db.add(CattleSaleLine(**record))
            inserted += 1
            continue
        if _sale_values_match(row, record):
            if record.get("buyer") and row.buyer != record.get("buyer"):
                row.buyer = record.get("buyer")
            message_id = record.get("source_message_id")
            if message_id and message_id != "local-file":
                row.source_message_id = message_id
            continue
        for field in (
            "cold_weight_kg",
            "reject_kg",
            "kill_date",
            "amount_gbp",
            "buyer",
            "source_message_id",
            "source_file",
            "source_received",
        ):
            setattr(row, field, record.get(field))
        updated += 1
    return (inserted, updated)


def import_cattle_sale_sources(
    db: Session,
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    parsed_by_key: dict[tuple[str, str, dt.date], dict[str, Any]] = {}
    warnings: list[str] = []
    skipped_files: list[str] = []
    files_processed = 0
    files_skipped = 0
    now = dt.datetime.now()

    for source in sources:
        source_file = source.get("source_file") or "unknown.pdf"
        content = source.get("content")
        if not content:
            skipped_files.append(f"{source_file}: empty PDF")
            files_skipped += 1
            continue
        try:
            result = _parse_sale_pdf(
                content,
                mailbox_farm=source.get("mailbox_farm") or _farm_from_filename(source_file),
                source_file=source_file,
                parser_hint=source.get("parser_hint"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Could not parse cattle sale PDF %s", source_file)
            skipped_files.append(f"Could not read PDF: {source_file} ({exc})")
            files_skipped += 1
            continue

        farm = result.get("farm")
        sale_date = result.get("sale_date")
        lines = list(result.get("lines") or [])
        for warning in result.get("warnings") or []:
            warnings.append(f"{source_file}: {warning}")

        if not lines:
            skipped_files.append(f"{source_file}: no animal lines found")
            files_skipped += 1
            continue

        message_id = source.get("message_id") or "local-file"
        received_at = source.get("received_at") or now
        ingested = False
        for line in lines:
            line_sale_date = line.get("kill_date") or sale_date
            if not line_sale_date:
                warnings.append(f"{source_file}: no kill date for {line.get('etag', 'unknown tag')}")
                continue
            etag = normalize_etag(line.get("etag"))
            if not etag:
                continue
            line_farm = farm or _farm_for_etag(db, etag, line_sale_date)
            if not line_farm:
                warnings.append(f"{source_file}: could not determine farm for {etag}")
                continue
            key = (line_farm, etag, line_sale_date)
            parsed_by_key[key] = {
                "farm": line_farm,
                "etag": etag,
                "sale_date": line_sale_date,
                "cold_weight_kg": line["cold_weight_kg"],
                "reject_kg": line.get("reject_kg"),
                "kill_date": line.get("kill_date") or line_sale_date,
                "amount_gbp": line["amount_gbp"],
                "buyer": result.get("buyer") or "Pickstock",
                "source_message_id": message_id,
                "source_file": source_file,
                "source_received": received_at,
            }
            ingested = True

        if ingested:
            files_processed += 1
        else:
            files_skipped += 1
            skipped_files.append(f"{source_file}: no usable animal lines")

    inserted, updated = _upsert(db, parsed_by_key)
    if inserted or updated:
        db.flush()
    return {
        "files_processed": files_processed,
        "files_skipped": files_skipped,
        "rows_inserted": inserted,
        "rows_updated": updated,
        "rows_total": inserted + updated,
        "warnings": warnings,
        "skipped_files": skipped_files,
    }


def import_local_cattle_sale_pdfs(db: Session) -> dict[str, Any]:
    sources = [
        {
            "content": path.read_bytes(),
            "source_file": path.name,
            "message_id": "local-file",
        }
        for path in _iter_local_pdfs()
    ]
    return import_cattle_sale_sources(db, sources)


def _known_sale_message_ids(db: Session) -> set[str]:
    rows = db.scalars(select(CattleSaleLine.source_message_id)).all()
    return {row for row in rows if row and row != "local-file"}


def _known_sale_filenames(db: Session) -> set[str]:
    rows = db.scalars(select(CattleSaleLine.source_file)).all()
    return {row for row in rows if row}


def sync_outlook_cattle_sales(db: Session) -> dict[str, Any] | None:
    from services.cattle_sales_email import (
        CattleSalePdfEmailService,
        outlook_cattle_sales_configured,
    )

    if not outlook_cattle_sales_configured():
        return None
    try:
        found = CattleSalePdfEmailService().fetch_pdfs(
            skip_message_ids=_known_sale_message_ids(db),
            skip_filenames=_known_sale_filenames(db),
        )
    except Exception:
        logger.exception("Outlook cattle-sale fetch failed")
        return None
    if not found:
        return None

    LOCAL_SALES_DIR.mkdir(parents=True, exist_ok=True)
    sources: list[dict[str, Any]] = []
    for item in found:
        name = Path(item["source_file"]).name
        destination = LOCAL_SALES_DIR / name
        destination.write_bytes(item["content"])
        sources.append(
            {
                "content": item["content"],
                "source_file": name,
                "message_id": item.get("message_id") or "outlook",
                "received_at": item.get("received_at"),
                "parser_hint": item.get("parser_hint"),
            }
        )
    return import_cattle_sale_sources(db, sources)


def ensure_local_cattle_sales(db: Session) -> dict[str, Any] | None:
    """Import local remittance PDFs, and Outlook remittance PDFs when configured."""
    outlook_result = sync_outlook_cattle_sales(db)
    files = _iter_local_pdfs()
    if not files:
        return outlook_result
    names = {path.name for path in files}
    existing = {
        row
        for row in db.scalars(
            select(CattleSaleLine.source_file).where(CattleSaleLine.source_file.in_(names))
        ).all()
        if row
    }
    if names <= existing:
        return outlook_result
    local_result = import_local_cattle_sale_pdfs(db)
    if outlook_result is None:
        return local_result
    return {
        "files_processed": (outlook_result.get("files_processed") or 0)
        + (local_result.get("files_processed") or 0),
        "files_skipped": (outlook_result.get("files_skipped") or 0)
        + (local_result.get("files_skipped") or 0),
        "rows_inserted": (outlook_result.get("rows_inserted") or 0)
        + (local_result.get("rows_inserted") or 0),
        "rows_updated": (outlook_result.get("rows_updated") or 0)
        + (local_result.get("rows_updated") or 0),
        "rows_total": (outlook_result.get("rows_total") or 0)
        + (local_result.get("rows_total") or 0),
        "warnings": list(outlook_result.get("warnings") or [])
        + list(local_result.get("warnings") or []),
        "skipped_files": list(outlook_result.get("skipped_files") or [])
        + list(local_result.get("skipped_files") or []),
    }
