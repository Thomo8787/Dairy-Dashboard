"""Fetch DataFlow Rotary Entry ID Report CSV attachments from Outlook."""

from __future__ import annotations

from datetime import datetime

from services.dataflow_email import DataFlowCsvEmailService


class RotaryEntryEmailService(DataFlowCsvEmailService):
    """Import Rotary Entry ID Report CSVs from Support@dataflow2.com."""

    SUBJECT_CONTAINS = "rotary entry id"
    FILENAME_CONTAINS = "rotary entry id"
    SEARCH_SUBJECT = "Rotary Entry ID"
    DOWNLOAD_SUBDIR = "rotary_entry"

    def fetch_rotary_entry_csvs(
        self,
        top: int = 50,
        *,
        since: datetime | None = None,
        skip_message_ids: set[str] | None = None,
    ) -> list[dict]:
        return self.fetch_csvs(top=top, since=since, skip_message_ids=skip_message_ids)
