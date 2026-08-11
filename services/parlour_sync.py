"""Import Milk Flow + Rotary Entry ID reports from DataFlow emails."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from services.database import (
    delete_parlour_records_in_date_range,
    get_imported_parlour_message_ids,
    init_db,
    save_milk_flow_records,
    save_rotary_entry_id_records,
)
from services.graph_client import require_azure_config
from services.milk_flow_email import MilkFlowEmailService
from services.milk_flow_parser import parse_milk_flow_csv
from services.rotary_entry_email import RotaryEntryEmailService
from services.rotary_entry_parser import parse_rotary_entry_id_csv

logger = logging.getLogger(__name__)

IMPORT_DAY_OPTIONS = (1, 3, 7, 14, 30)


def _since_for_days_back(days_back: int) -> datetime:
    days = max(1, int(days_back))
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


def sync_parlour_emails(
    *,
    farm_code: str = "ALH",
    days_back: int | None = None,
    overwrite: bool = False,
    top: int = 80,
) -> dict[str, Any]:
    """
    Fetch DataFlow Milk Flow + Rotary Entry ID CSVs and persist them.

    Incremental (hourly cron):
      days_back=None, overwrite=False
      → only messages not already imported; skip entirely if none are new.

    Manual days-back:
      days_back=N, overwrite=True
      → emails received in last N days; delete DB rows in that calendar window;
        then re-insert from those CSVs.
    """
    missing = require_azure_config()
    if missing:
        raise RuntimeError(f"Missing Microsoft Graph configuration: {', '.join(missing)}")

    init_db()
    farm = (farm_code or "ALH").upper()

    since: datetime | None = None
    skip_ids: set[str] | None = None
    start_date: date | None = None
    end_date: date | None = None
    deleted = {"milk_flow_deleted": 0, "rotary_entry_deleted": 0}

    if overwrite:
        days = max(1, int(days_back or 7))
        since = _since_for_days_back(days)
        start_date, end_date = _date_window(days)
        deleted = delete_parlour_records_in_date_range(farm, start_date, end_date)
        logger.info(
            "Overwrite window %s..%s for %s — deleted milk=%s entry=%s",
            start_date,
            end_date,
            farm,
            deleted["milk_flow_deleted"],
            deleted["rotary_entry_deleted"],
        )
    else:
        # Hourly / incremental: ignore already-imported Graph message IDs.
        skip_ids = get_imported_parlour_message_ids(farm)
        # Still bound the search window so Graph $search stays relevant.
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
            "farm_code": farm,
            "overwrite": overwrite,
            "days_back": days_back,
            "start_date": start_date,
            "end_date": end_date,
            "deleted": deleted,
            "milk_files": 0,
            "milk_rows": 0,
            "entry_files": 0,
            "entry_rows": 0,
        }

    skip_duplicates = not overwrite
    milk_files = 0
    milk_rows = 0
    for item in milk_downloads:
        records = parse_milk_flow_csv(item["file_path"], farm_code=farm)
        if start_date and end_date:
            records = _filter_records_to_window(records, start_date, end_date)
        if not records:
            continue
        _, inserted = save_milk_flow_records(
            {
                "farm_code": farm,
                "report_type": "milk_flow",
                "filename": item["filename"],
                "email_subject": item["email_subject"],
                "email_from": item["email_from"],
                "email_received_at": item["email_received_at"],
                "message_id": item["message_id"],
            },
            records,
            skip_duplicates=skip_duplicates,
        )
        milk_files += 1
        milk_rows += inserted

    entry_files = 0
    entry_rows = 0
    for item in entry_downloads:
        records = parse_rotary_entry_id_csv(item["file_path"], farm_code=farm)
        if start_date and end_date:
            records = _filter_records_to_window(records, start_date, end_date)
        if not records:
            continue
        _, inserted = save_rotary_entry_id_records(
            {
                "farm_code": farm,
                "report_type": "rotary_entry_id",
                "filename": item["filename"],
                "email_subject": item["email_subject"],
                "email_from": item["email_from"],
                "email_received_at": item["email_received_at"],
                "message_id": item["message_id"],
            },
            records,
            skip_duplicates=skip_duplicates,
        )
        entry_files += 1
        entry_rows += inserted

    return {
        "skipped": False,
        "farm_code": farm,
        "overwrite": overwrite,
        "days_back": days_back,
        "start_date": start_date,
        "end_date": end_date,
        "deleted": deleted,
        "milk_files": milk_files,
        "milk_rows": milk_rows,
        "entry_files": entry_files,
        "entry_rows": entry_rows,
    }


def format_sync_summary(result: dict[str, Any]) -> str:
    if result.get("skipped"):
        return "No new DataFlow emails to import."

    farm = result.get("farm_code", "ALH")
    parts = [
        f"{farm} import — Milk Flow: {result.get('milk_files', 0)} file(s), "
        f"{result.get('milk_rows', 0)} rows; "
        f"Rotary Entry ID: {result.get('entry_files', 0)} file(s), "
        f"{result.get('entry_rows', 0)} rows."
    ]
    if result.get("overwrite"):
        deleted = result.get("deleted") or {}
        parts.append(
            f"Overwrote {result.get('start_date')} → {result.get('end_date')} "
            f"(removed {deleted.get('milk_flow_deleted', 0)} milk + "
            f"{deleted.get('rotary_entry_deleted', 0)} entry rows first)."
        )
    return " ".join(parts)
