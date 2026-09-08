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


def _has_quality(row: NmlMilkResult) -> bool:
    return any(
        getattr(row, field) is not None
        for field in ("butterfat_pct", "protein_pct", "scc", "bactoscan", "fpd", "urea_pct")
    )


def _looks_like_placeholder_sample(sample_id: str | None) -> bool:
    text = (sample_id or "").strip().upper()
    return bool(text) and text[0] == "L" and text[1:].isdigit()


def _is_unmatched_volume(row: NmlMilkResult) -> bool:
    """Farm ticket with volume that still needs NML quality linked."""
    if not _has_volume(row):
        return False
    if _has_quality(row):
        return False
    if row.sample_missing:
        return True
    return _looks_like_placeholder_sample(row.sample_id)


def _row_index(rows: list[NmlMilkResult]) -> dict[tuple[str, str], list[NmlMilkResult]]:
    index: dict[tuple[str, str], list[NmlMilkResult]] = {}
    for row in rows:
        key = (row.producer_ref, normalize_sample_id(row.sample_id))
        index.setdefault(key, []).append(row)
    return index


def _rekey_row(
    index: dict[tuple[str, str], list[NmlMilkResult]],
    row: NmlMilkResult,
    old_sample_id: str,
    new_sample_id: str,
) -> None:
    old_key = (row.producer_ref, normalize_sample_id(old_sample_id))
    new_key = (row.producer_ref, normalize_sample_id(new_sample_id))
    if old_key == new_key:
        return
    bucket = index.get(old_key) or []
    index[old_key] = [item for item in bucket if item is not row]
    if not index[old_key]:
        index.pop(old_key, None)
    index.setdefault(new_key, []).append(row)


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


def _best_unmatched_volume(
    index: dict[tuple[str, str], list[NmlMilkResult]],
    *,
    producer_ref: str,
    sample_date: dt.date,
    exclude: NmlMilkResult | None = None,
) -> NmlMilkResult | None:
    """
    When the farm ticket had no sample number yet (stored as L1/L2), match NML
    quality onto the next unmatched volume row for that producer within ±1 day.
    """
    candidates: list[NmlMilkResult] = []
    for rows in index.values():
        for row in rows:
            if exclude is not None and row is exclude:
                continue
            if row.producer_ref != producer_ref:
                continue
            if row.sample_date is None:
                continue
            if abs((row.sample_date - sample_date).days) > DATE_WINDOW_DAYS:
                continue
            if not _is_unmatched_volume(row):
                continue
            candidates.append(row)
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda row: (
            abs((row.sample_date - sample_date).days),
            row.load_number if row.load_number is not None else 999,
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


def _attach_quality_to_collection(
    index: dict[tuple[str, str], list[NmlMilkResult]],
    row: NmlMilkResult,
    record: dict[str, Any],
    *,
    session=None,
) -> None:
    """Apply NML fields and adopt the real sample id when the ticket was a placeholder."""
    old_sample = row.sample_id
    new_sample = normalize_sample_id(record.get("sample_id") or old_sample)
    # Drop lab-only rows that own this sample id, and flush deletes before
    # updating — otherwise UNIQUE(producer_ref, sample_date, sample_id) fails.
    if session is not None and new_sample:
        key = (row.producer_ref, new_sample)
        deleted_any = False
        for other in list(index.get(key, [])):
            if other is row:
                continue
            if not _has_volume(other):
                _drop_orphan(session, index, other)
                deleted_any = True
        if deleted_any:
            session.flush()
    _apply_quality(row, record)
    if new_sample and (
        row.sample_missing
        or _looks_like_placeholder_sample(old_sample)
        or normalize_sample_id(old_sample) != new_sample
    ):
        # If another volume row already holds this sample on the same date, skip rekey.
        if session is not None and row.sample_date is not None:
            clash = next(
                (
                    other
                    for other in (index.get((row.producer_ref, new_sample)) or [])
                    if other is not row
                    and other.sample_date == row.sample_date
                    and _has_volume(other)
                ),
                None,
            )
            if clash is not None:
                return
        row.sample_id = new_sample
        row.sample_missing = False
        _rekey_row(index, row, old_sample, new_sample)


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
    if not index[key]:
        index.pop(key, None)


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
            match = _best_unmatched_volume(
                index,
                producer_ref=orphan.producer_ref,
                sample_date=orphan.sample_date,
                exclude=orphan,
            )
        if match is None or match is orphan or not _has_volume(match):
            continue
        record = {field: getattr(orphan, field) for field in (*_SAMPLE_FIELDS, *_META_FIELDS)}
        record["sample_id"] = orphan.sample_id
        record["source_message_id"] = orphan.source_message_id
        record["source_file"] = orphan.source_file
        # Adopting the orphan's sample id also deletes this lab-only row.
        _attach_quality_to_collection(index, match, record, session=session)
        if orphan in (index.get((orphan.producer_ref, normalize_sample_id(orphan.sample_id))) or []):
            _drop_orphan(session, index, orphan)
        merged += 1
    return merged


def rematch_orphan_nml_results() -> dict[str, int]:
    """Link existing lab-only NML rows onto unmatched farm volume tickets."""
    from sqlalchemy.exc import IntegrityError

    with get_session() as session:
        try:
            rows = list(session.scalars(select(NmlMilkResult)).all())
            index = _row_index(rows)
            merged = _merge_orphan_nml(session, index)
            session.flush()
        except IntegrityError:
            session.rollback()
            logger.exception("NML orphan rematch failed unique constraint; leaving rows unchanged")
            return {"orphans_merged": 0}
    return {"orphans_merged": merged}


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
        linked_via_placeholder = False
        if row is None:
            row = _best_unmatched_volume(
                index,
                producer_ref=producer_ref,
                sample_date=sample_date,
            )
            linked_via_placeholder = row is not None
        if row is None:
            new_row = NmlMilkResult(**record)
            session.add(new_row)
            index.setdefault((producer_ref, normalize_sample_id(sample_id)), []).append(new_row)
            inserted += 1
            continue
        _attach_quality_to_collection(index, row, record, session=session)
        updated += 1
        if row.nml_matched or linked_via_placeholder:
            linked += 1
        key = (producer_ref, normalize_sample_id(sample_id))
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
