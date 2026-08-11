"""Shared DataFlow CSV email fetch helpers."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

from services.graph_client import GRAPH_BASE, auth_mode, graph_get, graph_get_bytes, graph_headers


class DataFlowCsvEmailService:
    """Fetch CSV attachments for a DataFlow report type from Outlook."""

    SENDER_DOMAIN = "dataflow2.com"
    SUBJECT_CONTAINS = ""
    FILENAME_CONTAINS = ""
    SEARCH_SUBJECT = ""
    DOWNLOAD_SUBDIR = "dataflow"

    def __init__(self, download_dir: str | Path | None = None):
        self.mailbox = os.environ.get("OUTLOOK_MAILBOX", "").strip()
        self.download_dir = (
            Path(download_dir) if download_dir else Path("data") / self.DOWNLOAD_SUBDIR
        )
        self.download_dir.mkdir(parents=True, exist_ok=True)

        if auth_mode() == "application" and not self.mailbox:
            raise RuntimeError("OUTLOOK_MAILBOX is not set")

    def _mailbox_root(self) -> str:
        if auth_mode() == "delegated" and not self.mailbox:
            return "me"
        return f"users/{quote(self.mailbox)}"

    def _search_messages(
        self,
        top: int = 50,
        *,
        since: datetime | None = None,
        skip_message_ids: set[str] | None = None,
    ) -> list[dict]:
        root = self._mailbox_root()
        url = (
            f"{GRAPH_BASE}/{root}/messages"
            f'?$search="subject:{self.SEARCH_SUBJECT}"'
            f"&$top={top}"
            f"&$select=id,subject,receivedDateTime,from,hasAttachments"
        )
        response = requests.get(
            url,
            headers={**graph_headers(), "ConsistencyLevel": "eventual"},
            timeout=60,
        )
        response.raise_for_status()
        messages = response.json().get("value", [])
        skip = skip_message_ids or set()
        matches = []
        for message in messages:
            message_id = message.get("id") or ""
            if message_id in skip:
                continue
            if not message.get("hasAttachments"):
                continue

            received_at = _parse_received(message.get("receivedDateTime"))
            if since is not None and received_at is not None:
                since_aware = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
                if received_at < since_aware:
                    continue
            elif since is not None and received_at is None:
                continue

            subject = (message.get("subject") or "").lower()
            sender = (
                message.get("from", {})
                .get("emailAddress", {})
                .get("address", "")
                .lower()
            )
            if self.SUBJECT_CONTAINS not in subject:
                continue
            if self.SENDER_DOMAIN not in sender:
                continue
            matches.append(message)
        return matches

    def _list_attachments(self, message_id: str) -> list[dict]:
        root = self._mailbox_root()
        url = f"{GRAPH_BASE}/{root}/messages/{message_id}/attachments"
        return graph_get(url).get("value", [])

    def _download_csv(self, message_id: str, attachment: dict) -> Path | None:
        name = attachment.get("name", "")
        if not name.lower().endswith(".csv"):
            return None
        if self.FILENAME_CONTAINS not in name.lower():
            return None

        root = self._mailbox_root()
        content = graph_get_bytes(
            f"{GRAPH_BASE}/{root}/messages/{message_id}/attachments/{attachment['id']}/$value"
        )
        safe_name = Path(name).name
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = self.download_dir / f"{timestamp}_{safe_name}"
        destination.write_bytes(content)
        return destination

    def fetch_csvs(
        self,
        top: int = 50,
        *,
        since: datetime | None = None,
        skip_message_ids: set[str] | None = None,
    ) -> list[dict]:
        downloads: list[dict] = []
        for message in self._search_messages(
            top=top, since=since, skip_message_ids=skip_message_ids
        ):
            received_at = _parse_received(message.get("receivedDateTime"))
            sender = (
                message.get("from", {})
                .get("emailAddress", {})
                .get("address")
            )

            for attachment in self._list_attachments(message["id"]):
                path = self._download_csv(message["id"], attachment)
                if not path:
                    continue
                downloads.append(
                    {
                        "file_path": path,
                        "filename": path.name,
                        "email_subject": message.get("subject"),
                        "email_from": sender,
                        "email_received_at": received_at,
                        "message_id": message["id"],
                    }
                )
        return downloads


def _parse_received(raw: str | None) -> datetime | None:
    if not raw:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))
