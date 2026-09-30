"""Import local cattle remittance PDFs into cattle_sale_lines."""

from __future__ import annotations

import datetime as dt
import gc
import hashlib
import json
import logging
import os
import re
import sys
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
INCOMING_DIR = LOCAL_SALES_DIR / "incoming"
QUEUE_PATH = LOCAL_SALES_DIR / "cron_queue.json"
OCR_RESULT_PATH = LOCAL_SALES_DIR / "ocr_result.json"
# These buyers send scanned pages. OCR runs in a fresh process so it does not
# sit on top of the database libraries and blow the 512MB cron.
SCANNED_PARSER_HINTS = frozenset({"neilds", "market_drayton"})


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


def _as_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and value:
        try:
            return dt.date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _as_datetime(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return dt.datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _empty_import_summary() -> dict[str, Any]:
    return {
        "files_processed": 0,
        "files_skipped": 0,
        "rows_inserted": 0,
        "rows_updated": 0,
        "rows_total": 0,
        "warnings": [],
        "skipped_files": [],
    }


def _merge_import_summary(total: dict[str, Any], part: dict[str, Any]) -> None:
    for key in (
        "files_processed",
        "files_skipped",
        "rows_inserted",
        "rows_updated",
        "rows_total",
    ):
        total[key] += part.get(key) or 0
    total["warnings"].extend(part.get("warnings") or [])
    total["skipped_files"].extend(part.get("skipped_files") or [])


def _import_one_result(
    db: Session,
    result: dict[str, Any],
    source: dict[str, Any],
) -> dict[str, Any]:
    summary = _empty_import_summary()
    source_file = source.get("source_file") or "unknown.pdf"
    farm = result.get("farm")
    sale_date = _as_date(result.get("sale_date"))
    lines = list(result.get("lines") or [])
    for warning in result.get("warnings") or []:
        summary["warnings"].append(f"{source_file}: {warning}")
    if not lines:
        summary["files_skipped"] = 1
        summary["skipped_files"].append(f"{source_file}: no animal lines found")
        return summary

    message_id = source.get("message_id") or "local-file"
    received_at = _as_datetime(source.get("received_at")) or dt.datetime.now()
    parsed_by_key: dict[tuple[str, str, dt.date], dict[str, Any]] = {}
    for line in lines:
        line_sale_date = _as_date(line.get("kill_date")) or sale_date
        if not line_sale_date:
            summary["warnings"].append(
                f"{source_file}: no kill date for {line.get('etag', 'unknown tag')}"
            )
            continue
        etag = normalize_etag(line.get("etag"))
        if not etag:
            continue
        line_farm = farm or _farm_for_etag(db, etag, line_sale_date)
        if not line_farm:
            summary["warnings"].append(f"{source_file}: could not determine farm for {etag}")
            continue
        parsed_by_key[(line_farm, etag, line_sale_date)] = {
            "farm": line_farm,
            "etag": etag,
            "sale_date": line_sale_date,
            "cold_weight_kg": line["cold_weight_kg"],
            "reject_kg": line.get("reject_kg"),
            "kill_date": _as_date(line.get("kill_date")) or line_sale_date,
            "amount_gbp": line["amount_gbp"],
            "buyer": result.get("buyer") or "Pickstock",
            "source_message_id": message_id,
            "source_file": source_file,
            "source_received": received_at,
        }
    if not parsed_by_key:
        summary["files_skipped"] = 1
        summary["skipped_files"].append(f"{source_file}: no usable animal lines")
        return summary
    inserted, updated = _upsert(db, parsed_by_key)
    if inserted or updated:
        db.flush()
    summary["files_processed"] = 1
    summary["rows_inserted"] = inserted
    summary["rows_updated"] = updated
    summary["rows_total"] = inserted + updated
    return summary


def import_parsed_cattle_sale(
    db: Session,
    parsed: dict[str, Any],
    source: dict[str, Any],
) -> dict[str, Any]:
    return _import_one_result(db, parsed, source)


def import_cattle_sale_sources(
    db: Session,
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    summary = _empty_import_summary()
    for source in sources:
        source_file = source.get("source_file") or "unknown.pdf"
        content = source.get("content")
        if not content:
            summary["files_skipped"] += 1
            summary["skipped_files"].append(f"{source_file}: empty PDF")
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
            summary["files_skipped"] += 1
            summary["skipped_files"].append(f"Could not read PDF: {source_file} ({exc})")
            continue
        _merge_import_summary(summary, _import_one_result(db, result, source))
    return summary


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


def _load_queue() -> dict[str, Any] | None:
    if not QUEUE_PATH.is_file():
        return None
    try:
        payload = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("Could not read cattle-sale cron queue")
        return None
    if not isinstance(payload, dict):
        return None
    payload.setdefault("pending", [])
    payload.setdefault("summary", _empty_import_summary())
    return payload


def _save_queue(payload: dict[str, Any]) -> None:
    LOCAL_SALES_DIR.mkdir(parents=True, exist_ok=True)
    QUEUE_PATH.write_text(json.dumps(payload), encoding="utf-8")


def _clear_queue() -> None:
    for path in (QUEUE_PATH, OCR_RESULT_PATH):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Could not remove %s", path)


def _incoming_pdf_path(message_id: str, source_file: str) -> Path:
    digest = hashlib.sha1(f"{message_id}|{source_file}".encode()).hexdigest()[:12]
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", source_file)[:80] or "remittance.pdf"
    return INCOMING_DIR / f"{digest}_{safe}"


def _exec_scanned_ocr(item: dict[str, Any]) -> None:
    """Replace this process with a small OCR job, then resume the queue."""
    script = PROJECT_ROOT / "scripts" / "ocr_cattle_sale_pdf.py"
    resume = PROJECT_ROOT / "scripts" / "sync_nml_emails.py"
    logger.info("Starting OCR process for %s", item.get("source_file"))
    os.execv(
        sys.executable,
        [
            sys.executable,
            str(script),
            "--pdf",
            item["path"],
            "--hint",
            item.get("parser_hint") or "neilds",
            "--source-file",
            item.get("source_file") or "remittance.pdf",
            "--out",
            str(OCR_RESULT_PATH),
            "--resume",
            str(resume),
        ],
    )


def _apply_ocr_result(db: Session, payload: dict[str, Any]) -> None:
    if not OCR_RESULT_PATH.is_file():
        return
    pending = payload.get("pending") or []
    if not pending:
        OCR_RESULT_PATH.unlink(missing_ok=True)
        return
    try:
        parsed_payload = json.loads(OCR_RESULT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("Could not read OCR result")
        OCR_RESULT_PATH.unlink(missing_ok=True)
        return
    item = pending[0]
    source = {
        "source_file": item.get("source_file"),
        "message_id": item.get("message_id"),
        "received_at": item.get("received_at"),
        "parser_hint": item.get("parser_hint"),
    }
    try:
        result = import_parsed_cattle_sale(db, parsed_payload.get("parsed") or {}, source)
        db.commit()
    except Exception:
        logger.exception("Could not save OCR result for %s", item.get("source_file"))
        db.rollback()
        result = _empty_import_summary()
        result["files_skipped"] = 1
        result["skipped_files"] = [f"{item.get('source_file')}: could not save OCR result"]
    _merge_import_summary(payload["summary"], result)
    logger.info(
        "Cattle-sale %s: %s inserted, %s updated",
        item.get("source_file"),
        result.get("rows_inserted") or 0,
        result.get("rows_updated") or 0,
    )
    pdf_path = Path(item.get("path") or "")
    if pdf_path.is_file():
        pdf_path.unlink(missing_ok=True)
    pending.pop(0)
    payload["pending"] = pending
    _save_queue(payload)
    OCR_RESULT_PATH.unlink(missing_ok=True)


def _download_cattle_sale_queue(db: Session, *, days: int | None) -> dict[str, Any] | None:
    from services.cattle_sales_email import CattleSalePdfEmailService

    since = None
    top = 40
    if days is not None and days > 0:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
        top = min(250, max(40, days))
    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    pending: list[dict[str, Any]] = []
    try:
        pdfs = CattleSalePdfEmailService().iter_pdfs(
            since=since,
            skip_message_ids=_known_sale_message_ids(db),
            skip_filenames=_known_sale_filenames(db),
            top=top,
        )
    except Exception:
        logger.exception("Outlook cattle-sale fetch failed")
        return None
    for item in pdfs:
        name = Path(item.get("source_file") or "remittance.pdf").name
        content = item.get("content") or b""
        item["content"] = b""
        destination = _incoming_pdf_path(item.get("message_id") or name, name)
        try:
            destination.write_bytes(content)
        except Exception:
            logger.exception("Could not save cattle-sale PDF %s", name)
            del content
            continue
        del content
        received = item.get("received_at")
        pending.append(
            {
                "path": str(destination),
                "source_file": name,
                "message_id": item.get("message_id") or "outlook",
                "received_at": received.isoformat() if isinstance(received, dt.datetime) else received,
                "parser_hint": item.get("parser_hint"),
                "ocr_attempts": 0,
            }
        )
        gc.collect()
    if not pending:
        return None
    payload = {"pending": pending, "summary": _empty_import_summary()}
    _save_queue(payload)
    logger.info("Cattle-sale queue: %s PDF(s) saved for import", len(pending))
    return payload


def _process_cattle_sale_queue(db: Session, payload: dict[str, Any]) -> dict[str, Any] | None:
    _apply_ocr_result(db, payload)
    while payload.get("pending"):
        item = payload["pending"][0]
        hint = (item.get("parser_hint") or "").strip().lower()
        pdf_path = Path(item.get("path") or "")
        if hint in SCANNED_PARSER_HINTS and os.environ.get("CATTLE_SALES_CRON") == "1":
            attempts = int(item.get("ocr_attempts") or 0)
            if attempts >= 1 and not OCR_RESULT_PATH.is_file():
                logger.warning("Skipping scanned remittance after a failed OCR: %s", item.get("source_file"))
                payload["summary"]["files_skipped"] += 1
                payload["summary"]["skipped_files"].append(
                    f"{item.get('source_file')}: scanned remittance OCR did not finish"
                )
                if pdf_path.is_file():
                    pdf_path.unlink(missing_ok=True)
                payload["pending"].pop(0)
                _save_queue(payload)
                continue
            item["ocr_attempts"] = attempts + 1
            _save_queue(payload)
            _exec_scanned_ocr(item)
        if not pdf_path.is_file():
            payload["summary"]["files_skipped"] += 1
            payload["summary"]["skipped_files"].append(f"{item.get('source_file')}: file missing")
            payload["pending"].pop(0)
            _save_queue(payload)
            continue
        content = pdf_path.read_bytes()
        try:
            result = import_cattle_sale_sources(
                db,
                [
                    {
                        "content": content,
                        "source_file": item.get("source_file"),
                        "message_id": item.get("message_id"),
                        "received_at": _as_datetime(item.get("received_at")),
                        "parser_hint": hint,
                    }
                ],
            )
            db.commit()
        except Exception:
            logger.exception("Cattle-sale import failed for %s", item.get("source_file"))
            db.rollback()
            result = _empty_import_summary()
            result["files_skipped"] = 1
            result["skipped_files"] = [f"{item.get('source_file')}: import failed"]
        finally:
            del content
            gc.collect()
        _merge_import_summary(payload["summary"], result)
        logger.info(
            "Cattle-sale %s: %s inserted, %s updated",
            item.get("source_file"),
            result.get("rows_inserted") or 0,
            result.get("rows_updated") or 0,
        )
        pdf_path.unlink(missing_ok=True)
        payload["pending"].pop(0)
        _save_queue(payload)
    summary = payload.get("summary") or _empty_import_summary()
    _clear_queue()
    if not summary["files_processed"] and not summary["files_skipped"]:
        return None
    return summary


def sync_outlook_cattle_sales(db: Session, *, days: int | None = None) -> dict[str, Any] | None:
    from services.cattle_sales_email import outlook_cattle_sales_configured

    payload = _load_queue()
    if payload is None:
        if not outlook_cattle_sales_configured():
            return None
        payload = _download_cattle_sale_queue(db, days=days)
        if payload is None:
            return None
    return _process_cattle_sale_queue(db, payload)


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
