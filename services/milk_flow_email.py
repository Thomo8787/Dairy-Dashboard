"""Fetch DataFlow Milk Flow Report CSV attachments from Outlook."""

from __future__ import annotations

from datetime import datetime

from services.dataflow_email import DataFlowCsvEmailService


class MilkFlowEmailService(DataFlowCsvEmailService):
    """Import Milk Flow Report CSVs from Support@dataflow2.com."""

    SUBJECT_CONTAINS = "milk flow report"
    FILENAME_CONTAINS = "milk flow report"
    SEARCH_SUBJECT = "Milk Flow Report"
    DOWNLOAD_SUBDIR = "milk_flow"

    def fetch_milk_flow_csvs(
        self,
        top: int = 50,
        *,
        since: datetime | None = None,
        skip_message_ids: set[str] | None = None,
    ) -> list[dict]:
        return self.fetch_csvs(top=top, since=since, skip_message_ids=skip_message_ids)
