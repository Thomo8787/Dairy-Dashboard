"""Import Outlook email attachments via Microsoft Graph."""

import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from services.graph_client import EXCEL_EXTENSIONS, GRAPH_BASE, auth_mode, graph_get, graph_get_bytes


class GraphEmailService:
    def __init__(self, download_dir: str | Path | None = None):
        self.mailbox = os.environ.get("OUTLOOK_MAILBOX", "").strip()
        self.sender_filter = os.environ.get("OUTLOOK_SENDER_FILTER", "").lower()
        self.subject_filter = os.environ.get("OUTLOOK_SUBJECT_FILTER", "").lower()
        self.download_dir = Path(download_dir or "data")
        self.download_dir.mkdir(parents=True, exist_ok=True)

        if auth_mode() == "application" and not self.mailbox:
            raise RuntimeError(
                "OUTLOOK_MAILBOX is not set. Use the inbox that receives dairy emails "
                "(e.g. parlours@alhfarm.com or mark@alhfarm.co.uk)."
            )

    def _mailbox_root(self) -> str:
        if auth_mode() == "delegated" and not self.mailbox:
            return "me"
        return f"users/{quote(self.mailbox)}"

    def _list_messages(self, top: int = 25) -> list[dict]:
        root = self._mailbox_root()
        url = (
            f"{GRAPH_BASE}/{root}/mailFolders/Inbox/messages"
            f"?$top={top}&$orderby=receivedDateTime desc"
            f"&$select=id,subject,receivedDateTime,from,hasAttachments"
        )
        return graph_get(url).get("value", [])

    def _message_matches_filters(self, message: dict) -> bool:
        if not message.get("hasAttachments"):
            return False

        if self.subject_filter:
            subject = (message.get("subject") or "").lower()
            if self.subject_filter not in subject:
                return False

        if self.sender_filter:
            sender = (
                message.get("from", {})
                .get("emailAddress", {})
                .get("address", "")
                .lower()
            )
            if self.sender_filter not in sender:
                return False

        return True

    def _list_attachments(self, message_id: str) -> list[dict]:
        root = self._mailbox_root()
        url = f"{GRAPH_BASE}/{root}/messages/{message_id}/attachments"
        return graph_get(url).get("value", [])

    def _download_attachment(self, message_id: str, attachment: dict) -> Path | None:
        name = attachment.get("name", "")
        extension = Path(name).suffix.lower()
        if extension not in EXCEL_EXTENSIONS:
            return None

        root = self._mailbox_root()
        attachment_id = attachment["id"]
        url = f"{GRAPH_BASE}/{root}/messages/{message_id}/attachments/{attachment_id}/$value"
        content = graph_get_bytes(url)

        safe_name = Path(name).name
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = self.download_dir / f"{timestamp}_{safe_name}"
        destination.write_bytes(content)
        return destination

    def fetch_excel_attachments(self, top: int = 25) -> list[dict]:
        downloads: list[dict] = []
        for message in self._list_messages(top=top):
            if not self._message_matches_filters(message):
                continue

            received_raw = message.get("receivedDateTime")
            received_at = None
            if received_raw:
                received_at = datetime.fromisoformat(received_raw.replace("Z", "+00:00"))

            for attachment in self._list_attachments(message["id"]):
                file_path = self._download_attachment(message["id"], attachment)
                if not file_path:
                    continue

                downloads.append(
                    {
                        "file_path": file_path,
                        "filename": file_path.name,
                        "email_subject": message.get("subject"),
                        "email_received_at": received_at,
                        "message_id": message["id"],
                    }
                )

        return downloads
