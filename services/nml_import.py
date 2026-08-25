"""Import NML milk-quality PDFs from Outlook into nml_milk_results."""

from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path
from typing import Any

from sqlalchemy import select

from services.database import NmlMilkResult, get_session
from services.nml_email import NML_LOOKBACK_DAYS, NmlPdfEmailService
from services.nml_pdf import normalize_sample_id, parse_nml_pdf

logger = logging.getLogger(__name__)

DATE_WINDOW_DAYS = 1
_SAMPLE_FIELDS = (
    "butterfat_pct",
    "protein_pct",
    "scc",
    "bactoscan",
    "fpd",
    "antibiotic_pass",
    "urea_pct",
)
_META_FIELDS = ("farm", "milk_buyer", "report_month", "report_date")


def nml_is_configured() -> bool:
    local = (os.environ.get("LOCAL_NML_DIR") or "").strip()
    mailbox = (os.environ.get("OUTLOOK_MAILBOX") or "").strip()
    return bool(local) or bool(mailbox)


def format_nml_summary(result: dict[str, Any]) -> str:
    return (
        f"NML: {result.get('files_processed', 0)} report(s), "
        f"{result.get('rows_linked', result.get('rows_updated', 0))} linked to collections, "
        f"{result.get('rows_inserted', 0)} unmatched, "
        f"{result.get('rows_updated', 0)} updated"
    )


def _iter_local_pdfs() -> list[dict[str, Any]]:
    folder = Path(os.environ.get("LOCAL_NML_DIR", "").strip())
    if not folder.is_dir():
        raise FileNotFoundError(f"LOCAL_NML_DIR not found: {folder}")
    sources: list[dict[str, Any]] = []
    for path in sorted(folder.rglob("*.pdf")):
        if path.name.startswith("~$"):
            continue
        sources.append(
            {
                "content": path.read_bytes(),
                "source_file": path.name,
                "message_id": None,
            }
        )
    return sources


def _known_message_ids() -> set[str]:
    with get_session() as session:
        rows = session.query(NmlMilkResult.source_message_id).distinct().all()
    return {row[0] for row in rows if row[0]}


def import_nml_results(
    *,
    full_history: bool = False,
    days: int | None = None,
    since: dt.date | dt.datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    mailbox = (os.environ.get("OUTLOOK_MAILBOX") or "").strip()
    local_dir = (os.environ.get("LOCAL_NML_DIR") or "").strip()
    if force:
        if not mailbox:
            raise ValueError("Set OUTLOOK_MAILBOX to fetch NML emails.")
    elif not nml_is_configured():
        raise ValueError(
            "NML import is not configured. Set OUTLOOK_MAILBOX or LOCAL_NML_DIR."
        )

    if since is not None:
        if isinstance(since, dt.datetime):
            since_at = since if since.tzinfo else since.replace(tzinfo=dt.timezone.utc)
        else:
            since_at = dt.datetime(since.year, since.month, since.day, tzinfo=dt.timezone.utc)
    elif days is not None and days > 0:
        since_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    elif full_history:
        since_at = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
    else:
        since_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=NML_LOOKBACK_DAYS)

    lookback = max(1, (dt.datetime.now(dt.timezone.utc) - since_at).days)
    fetch_top = min(2000, max(50, lookback * 8))

    if force or not local_dir:
        # Cron/normal import skips Outlook message IDs already saved.
        # The dashboard force button passes skip_ids=empty.
        skip_ids: set[str] = set() if force else _known_message_ids()
        sources = NmlPdfEmailService().fetch_pdfs(
            since=since_at,
            skip_message_ids=skip_ids,
            top=fetch_top,
        )
    else:
        sources = _iter_local_pdfs()
    return _import_sources(sources)


def import_nml_pdf_paths(paths: list[Path]) -> dict[str, Any]:
    """Import already-downloaded NML PDFs (e.g. the daily emailed reports)."""
    sources = [
        {"content": path.read_bytes(), "source_file": path.name, "message_id": None}
        for path in paths
        if path.is_file() and path.suffix.lower() == ".pdf"
    ]
    return _import_sources(sources)


def _sample_key(producer_ref: str, sample_date: dt.date, sample_id: str) -> tuple[str, dt.date, str]:
    return (producer_ref, sample_date, normalize_sample_id(sample_id))


def _import_sources(sources: list[dict[str, Any]]) -> dict[str, Any]:
    parsed_by_key: dict[tuple[str, dt.date, str], dict[str, Any]] = {}
    files_processed = 0
    files_skipped = 0
    warnings: list[str] = []

    for source in sources:
        source_file = source.get("source_file") or "unknown"
        try:
            result = parse_nml_pdf(source["content"])
        except Exception:
            files_skipped += 1
            warnings.append(f"{source_file}: could not read PDF")
            logger.exception("Failed to parse NML PDF %s", source_file)
            continue

        metadata = result["metadata"]
        producer_ref = (metadata.get("producer_ref") or "").strip()
        if not producer_ref or not result["samples"]:
            files_skipped += 1
            if not producer_ref:
                warnings.append(f"{source_file}: no producer reference found")
            else:
                warnings.append(f"{source_file}: no sample rows found")
            continue

        files_processed += 1
        farm = metadata.get("farm")
        if farm is None:
            warnings.append(
                f"{source_file}: unknown producer reference {producer_ref}"
            )
        for sample in result["samples"]:
            sample_id = normalize_sample_id(sample["sample_id"])
            key = _sample_key(producer_ref, sample["sample_date"], sample_id)
            record: dict[str, Any] = {
                "producer_ref": producer_ref,
                "sample_date": sample["sample_date"],
                "sample_id": sample_id,
                "farm": farm,
                "milk_buyer": metadata.get("milk_buyer"),
                "report_month": metadata.get("report_month"),
                "report_date": metadata.get("report_date"),
                "source": "nml",
                "source_message_id": source.get("message_id"),
                "source_file": source_file,
            }
            for field in _SAMPLE_FIELDS:
                record[field] = sample.get(field)
            parsed_by_key[key] = record

    with get_session() as session:
        inserted, updated, linked, merged = _upsert(session, parsed_by_key)

    return {
        "files_processed": files_processed,
        "files_skipped": files_skipped,
        "rows_inserted": inserted,
        "rows_updated": updated,
        "rows_linked": linked,
        "orphans_merged": merged,
        "rows_total": inserted + updated,
        "warnings": warnings,
        "imported_at": dt.datetime.now().isoformat(timespec="seconds"),
    }


def _has_volume(row: NmlMilkResult) -> bool:
    return row.litres_load is not None and float(row.litres_load) > 0


def _row_index(rows: list[NmlMilkResult]) -> dict[tuple[str, str], list[NmlMilkResult]]:
    index: dict[tuple[str, str], list[NmlMilkResult]] = {}
    for row in rows:
        key = (row.producer_ref, normalize_sample_id(row.sample_id))
        index.setdefault(key, []).append(row)
    return index


def _best_collection(
    index: dict[tuple[str, str], list[NmlMilkResult]],
    *,
    producer_ref: str,
    sample_date: dt.date,
    sample_id: str,
    exclude: NmlMilkResult | None = None,
) -> NmlMilkResult | None:
    """Match exact sample number to a collection whose sold date is within ±1 day."""
    key = (producer_ref, normalize_sample_id(sample_id))
    candidates: list[NmlMilkResult] = []
    for row in index.get(key, []):
        if exclude is not None and row is exclude:
            continue
        if row.sample_date is None:
            continue
        if abs((row.sample_date - sample_date).days) > DATE_WINDOW_DAYS:
            continue
        candidates.append(row)
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda row: (
            0 if _has_volume(row) else 1,
            abs((row.sample_date - sample_date).days),
            row.id or 0,
        ),
    )


def _apply_quality(row: NmlMilkResult, record: dict[str, Any]) -> None:
    for field in _SAMPLE_FIELDS:
        value = record.get(field)
        if value is not None:
            setattr(row, field, value)
    for field in _META_FIELDS:
        value = record.get(field)
        if value is not None:
            setattr(row, field, value)
    if record.get("source_message_id"):
        row.source_message_id = record["source_message_id"]
    if record.get("source_file"):
        row.source_file = record["source_file"]
    if _has_volume(row):
        row.nml_matched = True
    row.imported_at = dt.datetime.now(dt.timezone.utc)


def _drop_orphan(
    session,
    index: dict[tuple[str, str], list[NmlMilkResult]],
    orphan: NmlMilkResult,
) -> None:
    if _has_volume(orphan):
        return
    key = (orphan.producer_ref, normalize_sample_id(orphan.sample_id))
    session.delete(orphan)
    bucket = index.get(key) or []
    index[key] = [row for row in bucket if row is not orphan]


def _merge_orphan_nml(
    session,
    index: dict[tuple[str, str], list[NmlMilkResult]],
) -> int:
    """Copy leftover lab-only rows onto a matching collection and remove the orphan."""
    merged = 0
    orphans = [
        row
        for rows in index.values()
        for row in list(rows)
        if not _has_volume(row)
    ]
    for orphan in orphans:
        match = _best_collection(
            index,
            producer_ref=orphan.producer_ref,
            sample_date=orphan.sample_date,
            sample_id=orphan.sample_id,
            exclude=orphan,
        )
        if match is None or match is orphan or not _has_volume(match):
            continue
        record = {field: getattr(orphan, field) for field in (*_SAMPLE_FIELDS, *_META_FIELDS)}
        record["source_message_id"] = orphan.source_message_id
        record["source_file"] = orphan.source_file
        _apply_quality(match, record)
        _drop_orphan(session, index, orphan)
        merged += 1
    return merged


def _upsert(
    session,
    parsed_by_key: dict[tuple[str, dt.date, str], dict[str, Any]],
) -> tuple[int, int, int, int]:
    existing_rows = list(session.scalars(select(NmlMilkResult)).all())
    index = _row_index(existing_rows)

    inserted = 0
    updated = 0
    linked = 0

    for record in parsed_by_key.values():
        producer_ref = record["producer_ref"]
        sample_date = record["sample_date"]
        sample_id = record["sample_id"]
        row = _best_collection(
            index,
            producer_ref=producer_ref,
            sample_date=sample_date,
            sample_id=sample_id,
        )
        if row is None:
            new_row = NmlMilkResult(**record)
            session.add(new_row)
            index.setdefault((producer_ref, sample_id), []).append(new_row)
            inserted += 1
            continue
        _apply_quality(row, record)
        updated += 1
        if row.nml_matched:
            linked += 1
        key = (producer_ref, sample_id)
        for other in list(index.get(key, [])):
            if other is row or other.sample_date is None:
                continue
            if abs((other.sample_date - sample_date).days) > DATE_WINDOW_DAYS:
                continue
            if not _has_volume(other):
                _drop_orphan(session, index, other)

    session.flush()
    merged = _merge_orphan_nml(session, index)
    session.commit()
    return (inserted, updated, linked, merged)
