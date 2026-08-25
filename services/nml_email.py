"""Fetch NML milk-quality PDF attachments from the DataFlow Outlook mailbox."""

from __future__ import annotations

import base64
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from services.graph_client import GRAPH_BASE, _request_with_retries, auth_mode, graph_get, graph_get_bytes, graph_headers
from services.nml_pdf import looks_like_nml_pdf

logger = logging.getLogger(__name__)

NML_LOOKBACK_DAYS = 14
_MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024


def _parse_received(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


class NmlPdfEmailService:
    """Pull NML report PDFs from the same mailbox as DataFlow CSVs."""

    def __init__(self) -> None:
        self.mailbox = os.environ.get("OUTLOOK_MAILBOX", "").strip()
        self.sender_filter = (
            os.environ.get("NML_SENDER") or "nationalmilklabs.com"
        ).strip().lower()
        self.subject_filter = (os.environ.get("NML_SUBJECT") or "").strip()
        if auth_mode() == "application" and not self.mailbox:
            raise RuntimeError("OUTLOOK_MAILBOX is not set")

    def _mailbox_root(self) -> str:
        if auth_mode() == "delegated" and not self.mailbox:
            return "me"
        return f"users/{quote(self.mailbox)}"

    def _message_matches(
        self,
        message: dict,
        *,
        since: datetime | None,
        skip_message_ids: set[str],
    ) -> bool:
        message_id = message.get("id") or ""
        if message_id in skip_message_ids:
            return False
        if not message.get("hasAttachments"):
            return False
        received_at = _parse_received(message.get("receivedDateTime"))
        if since is not None:
            since_aware = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
            if received_at is None or received_at < since_aware:
                return False
        subject = (message.get("subject") or "").lower()
        sender = (
            message.get("from", {})
            .get("emailAddress", {})
            .get("address", "")
            .lower()
        )
        sender_ok = bool(self.sender_filter) and self.sender_filter in sender
        if sender_ok:
            return True
        if self.subject_filter:
            return self.subject_filter.lower() in subject
        return False

    def _list_messages(self, *, top: int, since: datetime, skip_message_ids: set[str]) -> list[dict]:
        root = self._mailbox_root()
        since_iso = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        sender = self.sender_filter.replace("'", "''")
        subject = self.subject_filter.replace("'", "''")
        if sender:
            scoped = (
                f"receivedDateTime ge {since_iso} and hasAttachments eq true "
                f"and contains(from/emailAddress/address,'{sender}')"
            )
        elif subject:
            scoped = (
                f"receivedDateTime ge {since_iso} and hasAttachments eq true "
                f"and contains(subject,'{subject}')"
            )
        else:
            scoped = f"receivedDateTime ge {since_iso} and hasAttachments eq true"
        query = urlencode(
            {
                "$filter": scoped,
                "$top": str(min(50, max(top, 1))),
                "$select": "id,subject,receivedDateTime,from,hasAttachments",
                "$count": "true",
            }
        )
        url: str | None = f"{GRAPH_BASE}/{root}/messages?{query}"
        headers = {**graph_headers(), "ConsistencyLevel": "eventual"}
        matches: list[dict] = []
        pages = 0
        max_pages = max(12, min(60, (top + 49) // 50 + 4))
        try:
            while url and len(matches) < top and pages < max_pages:
                pages += 1
                response = _request_with_retries("GET", url, timeout=60, headers=headers)
                payload = response.json()
                for message in payload.get("value", []):
                    if self._message_matches(
                        message, since=since, skip_message_ids=skip_message_ids
                    ):
                        matches.append(message)
                        if len(matches) >= top:
                            break
                url = payload.get("@odata.nextLink")
        except Exception:
            logger.exception("NML subject-filtered Graph list failed; trying attachment scan")
            return self._attachment_scan(root, top, since_iso, since, skip_message_ids)

        if matches:
            matches.sort(key=lambda m: m.get("receivedDateTime") or "", reverse=True)
            return matches[:top]
        return self._attachment_scan(root, top, since_iso, since, skip_message_ids)

    def _attachment_scan(
        self,
        root: str,
        top: int,
        since_iso: str,
        since: datetime,
        skip_message_ids: set[str],
    ) -> list[dict]:
        query = urlencode(
            {
                "$filter": f"receivedDateTime ge {since_iso} and hasAttachments eq true",
                "$orderby": "receivedDateTime desc",
                "$top": "50",
                "$select": "id,subject,receivedDateTime,from,hasAttachments",
            }
        )
        url: str | None = f"{GRAPH_BASE}/{root}/messages?{query}"
        matches: list[dict] = []
        pages = 0
        max_pages = max(12, min(60, (top + 49) // 50 + 4))
        while url and len(matches) < top and pages < max_pages:
            pages += 1
            response = _request_with_retries("GET", url, timeout=60)
            payload = response.json()
            for message in payload.get("value", []):
                if self._message_matches(
                    message, since=since, skip_message_ids=skip_message_ids
                ):
                    matches.append(message)
                    if len(matches) >= top:
                        break
            url = payload.get("@odata.nextLink")
        return matches[:top]

    def _list_attachments(self, message_id: str) -> list[dict]:
        root = self._mailbox_root()
        return graph_get(f"{GRAPH_BASE}/{root}/messages/{message_id}/attachments").get("value", [])

    def _attachment_bytes(self, message_id: str, attachment: dict) -> bytes | None:
        raw = attachment.get("contentBytes")
        if raw:
            try:
                return base64.b64decode(raw)
            except Exception:
                logger.warning("Could not decode contentBytes for %s", attachment.get("name"))
        root = self._mailbox_root()
        return graph_get_bytes(
            f"{GRAPH_BASE}/{root}/messages/{message_id}/attachments/{attachment['id']}/$value"
        )

    def fetch_pdfs(
        self,
        *,
        since: datetime | None = None,
        skip_message_ids: set[str] | None = None,
        top: int = 80,
    ) -> list[dict[str, Any]]:
        since_aware = since or (datetime.now(timezone.utc) - timedelta(days=NML_LOOKBACK_DAYS))
        if since_aware.tzinfo is None:
            since_aware = since_aware.replace(tzinfo=timezone.utc)
        skip = skip_message_ids if skip_message_ids is not None else set()
        messages = self._list_messages(top=top, since=since_aware, skip_message_ids=skip)
        found: list[dict[str, Any]] = []
        for message in messages:
            message_id = message.get("id") or ""
            for attachment in self._list_attachments(message_id):
                name = attachment.get("name") or "nml.pdf"
                if not str(name).lower().endswith(".pdf"):
                    continue
                size = int(attachment.get("size") or 0)
                if size > _MAX_ATTACHMENT_BYTES:
                    logger.warning("Skipping oversized NML PDF %s (%s bytes)", name, size)
                    continue
                content = self._attachment_bytes(message_id, attachment)
                if not content or not content.startswith(b"%PDF"):
                    continue
                if not looks_like_nml_pdf(content):
                    continue
                found.append(
                    {
                        "content": content,
                        "source_file": Path(name).name,
                        "message_id": message_id,
                        "subject": message.get("subject"),
                        "received_at": _parse_received(message.get("receivedDateTime")),
                    }
                )
        logger.info("Fetched %s NML PDF(s) from %s message(s)", len(found), len(messages))
        return found
