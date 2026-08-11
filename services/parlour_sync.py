"""Import Milk Flow + Rotary Entry ID reports from DataFlow emails."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from services.database import (
    delete_parlour_records_in_date_range,
    get_imported_parlour_message_ids,
    init_db,
    save_milk_flow_records,
    save_rotary_entry_id_records,
)
from services.farms import FARMS_BY_CODE
from services.graph_client import require_azure_config
from services.milk_flow_email import MilkFlowEmailService
from services.milk_flow_parser import parse_milk_flow_csv
from services.rotary_entry_email import RotaryEntryEmailService
from services.rotary_entry_parser import parse_rotary_entry_id_csv

logger = logging.getLogger(__name__)

IMPORT_DAY_OPTIONS = (1, 3, 7, 14, 30)
_FARM_CODES = tuple(sorted(FARMS_BY_CODE.keys(), key=len, reverse=True))


def detect_farm_code(*texts: str) -> str | None:
    """Pick a farm code from filename / subject text (e.g. 'Milk Flow Report ALH.csv')."""
    blob = " ".join(text or "" for text in texts).upper()
    if not blob.strip():
        return None
    for code in _FARM_CODES:
        if re.search(rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])", blob):
            return code
    return None


def _since_for_days_back(days_back: int) -> datetime:
    # Emails often arrive after midnight carrying the previous milking date
    # (e.g. Evening 10 Aug lands mid-morning 11 Aug). Look back one extra day
    # so those delayed reports are still collected.
    days = max(1, int(days_back)) + 1
    return datetime.now(timezone.utc) - timedelta(days=days)


def _date_window(days_back: int) -> tuple[date, date]:
    end = date.today()
    start = end - timedelta(days=max(1, int(days_back)) - 1)
    return start, end


def _filter_records_to_window(records: list[dict], start: date, end: date) -> list[dict]:
    return [
        row
        for row in records
        if row.get("milking_date") is not None and start <= row["milking_date"] <= end
    ]


def _batch_meta(item: dict, farm_code: str, report_type: str) -> dict:
    return {
        "farm_code": farm_code,
        "report_type": report_type,
        "filename": item["filename"],
        "email_subject": item["email_subject"],
        "email_from": item["email_from"],
        "email_received_at": item["email_received_at"],
        "message_id": item["message_id"],
    }


def sync_parlour_emails(
    *,
    farm_code: str | None = None,
    days_back: int | None = None,
    overwrite: bool = False,
    top: int = 80,
) -> dict[str, Any]:
    """
    Fetch DataFlow Milk Flow + Rotary Entry ID CSVs and persist them.

    Farm codes are read from each attachment filename/subject (ALH, COF, …).
    All shifts present in the CSVs are imported — UI shift chips are view-only.

    Incremental (hourly cron):
      overwrite=False
      → only messages not already imported; skip entirely if none are new.

    Manual days-back:
      days_back=N, overwrite=True
      → emails received in last N days; delete DB rows in that calendar window
        for all farms (or one farm if farm_code is set); then re-insert.
    """
    missing = require_azure_config()
    if missing:
        raise RuntimeError(f"Missing Microsoft Graph configuration: {', '.join(missing)}")

    init_db()
    scoped_farm = (farm_code or "").strip().upper() or None
    if scoped_farm in {"ALL", "*"} or (
        scoped_farm and scoped_farm not in FARMS_BY_CODE
    ):
        scoped_farm = None

    since: datetime | None = None
    skip_ids: set[str] | None = None
    start_date: date | None = None
    end_date: date | None = None
    deleted = {"milk_flow_deleted": 0, "rotary_entry_deleted": 0}

    if overwrite:
        days = max(1, int(days_back or 7))
        since = _since_for_days_back(days)
        start_date, end_date = _date_window(days)
        deleted = delete_parlour_records_in_date_range(scoped_farm, start_date, end_date)
        logger.info(
            "Overwrite window %s..%s for %s — deleted milk=%s entry=%s",
            start_date,
            end_date,
            scoped_farm or "ALL FARMS",
            deleted["milk_flow_deleted"],
            deleted["rotary_entry_deleted"],
        )
    else:
        # Hourly / incremental: ignore already-imported Graph message IDs (any farm).
        skip_ids = get_imported_parlour_message_ids(None)
        since = _since_for_days_back(int(days_back or 2))

    milk_service = MilkFlowEmailService()
    entry_service = RotaryEntryEmailService()
    milk_downloads = milk_service.fetch_milk_flow_csvs(
        top=top, since=since, skip_message_ids=skip_ids
    )
    entry_downloads = entry_service.fetch_rotary_entry_csvs(
        top=top, since=since, skip_message_ids=skip_ids
    )

    if not milk_downloads and not entry_downloads:
        return {
            "skipped": True,
            "reason": "no_new_emails",
            "farm_code": scoped_farm or "ALL",
            "farms": [],
            "overwrite": overwrite,
            "days_back": days_back,
            "start_date": start_date,
            "end_date": end_date,
            "deleted": deleted,
            "milk_files": 0,
            "milk_rows": 0,
            "entry_files": 0,
            "entry_rows": 0,
            "skipped_files": 0,
        }

    skip_duplicates = True
    milk_files = 0
    milk_rows = 0
    entry_files = 0
    entry_rows = 0
    skipped_files = 0
    farms_seen: set[str] = set()
    milking_dates_seen: set[date] = set()

    for item in milk_downloads:
        farm = detect_farm_code(item.get("filename", ""), item.get("email_subject", ""))
        if not farm:
            skipped_files += 1
            logger.warning("Skipping Milk Flow file with unknown farm: %s", item.get("filename"))
            continue
        if scoped_farm and farm != scoped_farm:
            continue
        try:
            records = parse_milk_flow_csv(item["file_path"], farm_code=farm)
        except Exception as exc:
            skipped_files += 1
            logger.warning(
                "Skipping unreadable Milk Flow file %s: %s",
                item.get("filename"),
                exc,
            )
            continue
        if start_date and end_date:
            records = _filter_records_to_window(records, start_date, end_date)
        if not records:
            continue
        for row in records:
            if row.get("milking_date") is not None:
                milking_dates_seen.add(row["milking_date"])
        _, inserted = save_milk_flow_records(
            _batch_meta(item, farm, "milk_flow"),
            records,
            skip_duplicates=skip_duplicates,
        )
        milk_files += 1
        milk_rows += inserted
        farms_seen.add(farm)

    for item in entry_downloads:
        farm = detect_farm_code(item.get("filename", ""), item.get("email_subject", ""))
        if not farm:
            skipped_files += 1
            logger.warning("Skipping Rotary Entry file with unknown farm: %s", item.get("filename"))
            continue
        if scoped_farm and farm != scoped_farm:
            continue
        try:
            records = parse_rotary_entry_id_csv(item["file_path"], farm_code=farm)
        except Exception as exc:
            skipped_files += 1
            logger.warning(
                "Skipping unreadable Rotary Entry file %s: %s",
                item.get("filename"),
                exc,
            )
            continue
        if start_date and end_date:
            records = _filter_records_to_window(records, start_date, end_date)
        if not records:
            continue
        for row in records:
            if row.get("milking_date") is not None:
                milking_dates_seen.add(row["milking_date"])
        _, inserted = save_rotary_entry_id_records(
            _batch_meta(item, farm, "rotary_entry_id"),
            records,
            skip_duplicates=skip_duplicates,
        )
        entry_files += 1
        entry_rows += inserted
        farms_seen.add(farm)

    return {
        "skipped": False,
        "farm_code": scoped_farm or "ALL",
        "farms": sorted(farms_seen),
        "overwrite": overwrite,
        "days_back": days_back,
        "start_date": start_date,
        "end_date": end_date,
        "deleted": deleted,
        "milk_files": milk_files,
        "milk_rows": milk_rows,
        "entry_files": entry_files,
        "entry_rows": entry_rows,
        "skipped_files": skipped_files,
        "latest_milking_date": max(milking_dates_seen) if milking_dates_seen else None,
        "milking_dates": sorted(milking_dates_seen),
    }


def format_sync_summary(result: dict[str, Any]) -> str:
    if result.get("skipped"):
        return "No new DataFlow emails to import."

    farms = result.get("farms") or []
    farm_label = ", ".join(farms) if farms else result.get("farm_code", "ALL")
    parts = [
        f"{farm_label} import — Milk Flow: {result.get('milk_files', 0)} file(s), "
        f"{result.get('milk_rows', 0)} rows; "
        f"Rotary Entry ID: {result.get('entry_files', 0)} file(s), "
        f"{result.get('entry_rows', 0)} rows "
        f"(all shifts)."
    ]
    latest = result.get("latest_milking_date")
    if latest:
        parts.append(f"Latest milking date in files: {latest}.")
    if result.get("overwrite"):
        deleted = result.get("deleted") or {}
        parts.append(
            f"Overwrote {result.get('start_date')} → {result.get('end_date')} "
            f"(removed {deleted.get('milk_flow_deleted', 0)} milk + "
            f"{deleted.get('rotary_entry_deleted', 0)} entry rows first)."
        )
    skipped_files = result.get("skipped_files") or 0
    if skipped_files:
        parts.append(f"Skipped {skipped_files} file(s) with unrecognised farm codes.")
    return " ".join(parts)
