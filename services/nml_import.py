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


def _needs_sample_autofill(row: NmlMilkResult) -> bool:
    """True when volume exists but the worker left sample blank (or L1/L2 placeholder)."""
    if not _has_volume(row):
        return False
    if row.sample_missing:
        return True
    return _looks_like_placeholder_sample(row.sample_id)


def _has_manual_sample(row: NmlMilkResult) -> bool:
    """Worker-entered sample number — never overwritten by volume pairing."""
    return _has_volume(row) and not _needs_sample_autofill(row)


def _quality_fingerprint(row: NmlMilkResult) -> tuple[Any, ...]:
    return tuple(getattr(row, field) for field in _SAMPLE_FIELDS)


def _row_index(rows: list[NmlMilkResult]) -> dict[tuple[str, str], list[NmlMilkResult]]:
    index: dict[tuple[str, str], list[NmlMilkResult]] = {}
    for row in rows:
        key = (row.producer_ref, normalize_sample_id(row.sample_id))
        index.setdefault(key, []).append(row)
    return index


def _iter_index_rows(
    index: dict[tuple[str, str], list[NmlMilkResult]],
) -> list[NmlMilkResult]:
    return [row for rows in index.values() for row in list(rows)]


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


def _sample_sort_key(value: str) -> tuple[int, int | str]:
    key = normalize_sample_id(value) or "0"
    try:
        return (0, int(key))
    except ValueError:
        return (1, key)


def _pair_samples_to_loads_by_volume(
    volumes: list[float],
    samples: list[str],
) -> list[str] | None:
    """Lowest sample number → highest volume. None unless counts match (all-or-nothing)."""
    if not volumes or len(volumes) != len(samples):
        return None
    ranked_samples = sorted(samples, key=_sample_sort_key)
    load_order = sorted(range(len(volumes)), key=lambda i: (-volumes[i], i))
    assigned = [""] * len(volumes)
    for rank, load_idx in enumerate(load_order):
        assigned[load_idx] = ranked_samples[rank]
    return assigned


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


def _orphan_record(orphan: NmlMilkResult) -> dict[str, Any]:
    record = {field: getattr(orphan, field) for field in (*_SAMPLE_FIELDS, *_META_FIELDS)}
    record["sample_id"] = orphan.sample_id
    record["source_message_id"] = orphan.source_message_id
    record["source_file"] = orphan.source_file
    return record


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


def _volume_holds_sample(
    index: dict[tuple[str, str], list[NmlMilkResult]],
    *,
    producer_ref: str,
    sample_date: dt.date | None,
    sample_id: str,
    exclude: NmlMilkResult,
) -> NmlMilkResult | None:
    if sample_date is None or not sample_id:
        return None
    for other in index.get((producer_ref, normalize_sample_id(sample_id))) or []:
        if other is exclude:
            continue
        if other.sample_date != sample_date:
            continue
        if _has_volume(other) and _has_manual_sample(other):
            return other
    return None


def _attach_quality_to_collection(
    index: dict[tuple[str, str], list[NmlMilkResult]],
    row: NmlMilkResult,
    record: dict[str, Any],
    *,
    session=None,
) -> bool:
    """Apply NML fields; adopt sample id only when the ticket was blank/placeholder.

    Manual sample numbers are never overwritten. Returns False if attach was skipped.
    """
    old_sample = row.sample_id
    new_sample = normalize_sample_id(record.get("sample_id") or old_sample)
    adopt_sample = bool(
        new_sample
        and (
            row.sample_missing
            or _looks_like_placeholder_sample(old_sample)
            or normalize_sample_id(old_sample) != new_sample
        )
    )
    # Never steal a sample id already held by a manually entered volume row.
    if adopt_sample and _volume_holds_sample(
        index,
        producer_ref=row.producer_ref,
        sample_date=row.sample_date,
        sample_id=new_sample,
        exclude=row,
    ):
        return False

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
    if adopt_sample:
        row.sample_id = new_sample
        row.sample_missing = False
        _rekey_row(index, row, old_sample, new_sample)
    return True


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


def _still_in_index(
    index: dict[tuple[str, str], list[NmlMilkResult]],
    orphan: NmlMilkResult,
) -> bool:
    key = (orphan.producer_ref, normalize_sample_id(orphan.sample_id))
    return orphan in (index.get(key) or [])


def _link_orphan_to_row(
    session,
    index: dict[tuple[str, str], list[NmlMilkResult]],
    orphan: NmlMilkResult,
    match: NmlMilkResult,
) -> bool:
    if match is None or match is orphan or not _has_volume(match):
        return False
    if not _attach_quality_to_collection(
        index, match, _orphan_record(orphan), session=session
    ):
        return False
    if _still_in_index(index, orphan):
        _drop_orphan(session, index, orphan)
    return True


def _merge_by_exact_sample(
    session,
    index: dict[tuple[str, str], list[NmlMilkResult]],
) -> int:
    merged = 0
    for orphan in [row for row in _iter_index_rows(index) if not _has_volume(row)]:
        if not _still_in_index(index, orphan):
            continue
        match = _best_collection(
            index,
            producer_ref=orphan.producer_ref,
            sample_date=orphan.sample_date,
            sample_id=orphan.sample_id,
            exclude=orphan,
        )
        if match is None or not _has_volume(match):
            continue
        # Exact sample match may update a manual ticket; do not require autofill.
        if _link_orphan_to_row(session, index, orphan, match):
            merged += 1
    return merged


def _merge_by_quality_fingerprint(
    session,
    index: dict[tuple[str, str], list[NmlMilkResult]],
) -> int:
    """Repair half-merges: blank volume already has this orphan's quality values."""
    merged = 0
    for orphan in [row for row in _iter_index_rows(index) if not _has_volume(row)]:
        if not _still_in_index(index, orphan) or not _has_quality(orphan):
            continue
        fingerprint = _quality_fingerprint(orphan)
        if all(value is None for value in fingerprint):
            continue
        candidates = [
            row
            for row in _iter_index_rows(index)
            if row is not orphan
            and _needs_sample_autofill(row)
            and row.producer_ref == orphan.producer_ref
            and row.sample_date == orphan.sample_date
            and _has_quality(row)
            and _quality_fingerprint(row) == fingerprint
        ]
        if len(candidates) != 1:
            continue
        if _link_orphan_to_row(session, index, orphan, candidates[0]):
            merged += 1
    return merged


def _merge_by_volume_pairing(
    session,
    index: dict[tuple[str, str], list[NmlMilkResult]],
) -> int:
    """GAD rule: same farm/date, blank loads only, lowest sample → highest litres."""
    from collections import defaultdict

    by_day: dict[tuple[str, dt.date], list[NmlMilkResult]] = defaultdict(list)
    for row in _iter_index_rows(index):
        if row.producer_ref and row.sample_date is not None:
            by_day[(row.producer_ref, row.sample_date)].append(row)

    merged = 0
    for (_producer_ref, _day), rows in by_day.items():
        blanks = [row for row in rows if _needs_sample_autofill(row)]
        if not blanks:
            continue
        used_samples = {
            normalize_sample_id(row.sample_id)
            for row in rows
            if _has_manual_sample(row)
        }
        available = [
            row
            for row in rows
            if not _has_volume(row)
            and normalize_sample_id(row.sample_id) not in used_samples
            and _still_in_index(index, row)
        ]
        assigned = _pair_samples_to_loads_by_volume(
            [float(row.litres_load or 0) for row in blanks],
            [row.sample_id for row in available],
        )
        if not assigned:
            continue
        orphan_by_sample = {
            normalize_sample_id(row.sample_id): row for row in available
        }
        for load, sample_id in zip(blanks, assigned):
            orphan = orphan_by_sample.get(normalize_sample_id(sample_id))
            if orphan is None or not _still_in_index(index, orphan):
                continue
            if _link_orphan_to_row(session, index, orphan, load):
                merged += 1
    return merged


def _merge_orphan_nml(
    session,
    index: dict[tuple[str, str], list[NmlMilkResult]],
) -> int:
    """Link lab-only NML rows onto farm collections.

    Order: exact sample number (manual wins) → fingerprint repair → GAD volume pair.
    Volume pairing is all-or-nothing for blank loads on the same calendar day.
    """
    return (
        _merge_by_exact_sample(session, index)
        + _merge_by_quality_fingerprint(session, index)
        + _merge_by_volume_pairing(session, index)
    )


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
        # Only link by exact sample number here. Blank tickets are filled later
        # by volume pairing so we never half-assign one sample at a time.
        row = _best_collection(
            index,
            producer_ref=producer_ref,
            sample_date=sample_date,
            sample_id=sample_id,
        )
        if row is None:
            new_row = NmlMilkResult(**record)
            session.add(new_row)
            index.setdefault((producer_ref, normalize_sample_id(sample_id)), []).append(new_row)
            inserted += 1
            continue
        if _attach_quality_to_collection(index, row, record, session=session):
            updated += 1
            if row.nml_matched:
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
