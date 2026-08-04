"""Import Outlook email attachments via Microsoft Graph."""

import os
from datetime import datetime, timezone
from pathlib import Path

import msal
import requests

GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
EXCEL_EXTENSIONS = {".xlsx", ".xls", ".xlsm"}


class GraphEmailService:
    def __init__(self, download_dir: str | Path | None = None):
        self.client_id = os.environ["AZURE_CLIENT_ID"]
        self.client_secret = os.environ["AZURE_CLIENT_SECRET"]
        self.tenant_id = os.environ["AZURE_TENANT_ID"]
        self.mailbox = os.environ.get("OUTLOOK_MAILBOX", "")
        self.sender_filter = os.environ.get("OUTLOOK_SENDER_FILTER", "").lower()
        self.subject_filter = os.environ.get("OUTLOOK_SUBJECT_FILTER", "").lower()
        self.download_dir = Path(download_dir or "data")
        self.download_dir.mkdir(parents=True, exist_ok=True)

    def _get_access_token(self) -> str:
        app = msal.ConfidentialClientApplication(
            self.client_id,
            authority=f"https://login.microsoftonline.com/{self.tenant_id}",
            client_credential=self.client_secret,
        )
        result = app.acquire_token_for_client(scopes=GRAPH_SCOPE)
        if "access_token" not in result:
            error = result.get("error_description") or result.get("error") or "Unknown auth error"
            raise RuntimeError(f"Microsoft Graph authentication failed: {error}")
        return result["access_token"]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_access_token()}"}

    def _graph_get(self, url: str) -> dict:
        response = requests.get(url, headers=self._headers(), timeout=60)
        response.raise_for_status()
        return response.json()

    def _list_messages(self, top: int = 25) -> list[dict]:
        mailbox = self.mailbox or "me"
        url = (
            f"{GRAPH_BASE}/users/{mailbox}/mailFolders/Inbox/messages"
            f"?$top={top}&$orderby=receivedDateTime desc"
            f"&$select=id,subject,receivedDateTime,from,hasAttachments"
        )
        return self._graph_get(url).get("value", [])

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
        mailbox = self.mailbox or "me"
        url = f"{GRAPH_BASE}/users/{mailbox}/messages/{message_id}/attachments"
        return self._graph_get(url).get("value", [])

    def _download_attachment(self, message_id: str, attachment: dict) -> Path | None:
        name = attachment.get("name", "")
        extension = Path(name).suffix.lower()
        if extension not in EXCEL_EXTENSIONS:
            return None

        mailbox = self.mailbox or "me"
        attachment_id = attachment["id"]
        url = f"{GRAPH_BASE}/users/{mailbox}/messages/{message_id}/attachments/{attachment_id}/$value"
        response = requests.get(url, headers=self._headers(), timeout=120)
        response.raise_for_status()

        safe_name = Path(name).name
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = self.download_dir / f"{timestamp}_{safe_name}"
        destination.write_bytes(response.content)
        return destination

    def fetch_excel_attachments(self, top: int = 25) -> list[dict]:
        """
        Download Excel attachments from recent inbox messages.

        Returns a list of dicts:
        {
            "file_path": Path,
            "filename": str,
            "email_subject": str,
            "email_received_at": datetime,
            "message_id": str,
        }
        """
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
