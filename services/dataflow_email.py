"""Shared DataFlow CSV email fetch helpers."""

from __future__ import annotations

import base64
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

import requests

from services.graph_client import GRAPH_BASE, auth_mode, graph_get, graph_get_bytes, graph_headers

logger = logging.getLogger(__name__)

# Keep concurrency modest on Render free (512MB / shared CPU).
_DOWNLOAD_WORKERS = 6


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
            if received_at is None:
                return False
            since_aware = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
            if received_at < since_aware:
                return False

        subject = (message.get("subject") or "").lower()
        sender = (
            message.get("from", {})
            .get("emailAddress", {})
            .get("address", "")
            .lower()
        )
        if self.SUBJECT_CONTAINS not in subject:
            return False
        if self.SENDER_DOMAIN not in sender:
            return False
        return True

    def _search_messages(
        self,
        top: int = 50,
        *,
        since: datetime | None = None,
        skip_message_ids: set[str] | None = None,
    ) -> list[dict]:
        """
        Prefer newest-first inbox paging filtered by receivedDateTime + subject.

        Graph $search ranks by relevance and often misses the newest reports when
        many historical Milk Flow / Rotary Entry emails exist.
        """
        skip = skip_message_ids or set()
        since_aware = since
        if since_aware is None:
            since_aware = datetime.now(timezone.utc) - timedelta(days=14)
        elif since_aware.tzinfo is None:
            since_aware = since_aware.replace(tzinfo=timezone.utc)

        matches = self._list_messages_by_received(
            top=top, since=since_aware, skip_message_ids=skip
        )
        if matches:
            return matches

        logger.warning(
            "Date-ordered Graph fetch returned no %s matches; falling back to $search",
            self.SEARCH_SUBJECT,
        )
        return self._list_messages_by_search(
            top=top, since=since_aware, skip_message_ids=skip
        )

    def _list_messages_by_received(
        self,
        *,
        top: int,
        since: datetime,
        skip_message_ids: set[str],
    ) -> list[dict]:
        root = self._mailbox_root()
        since_iso = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        subject = (self.SEARCH_SUBJECT or self.SUBJECT_CONTAINS or "").strip()

        # Prefer subject-scoped advanced query so we don't page through every
        # attachment email in the mailbox (that made 7-day imports crawl).
        if subject:
            scoped = self._list_messages_with_subject_filter(
                root=root,
                top=top,
                since_iso=since_iso,
                subject=subject,
                since=since,
                skip_message_ids=skip_message_ids,
            )
            if scoped is not None:
                return scoped

        return self._list_messages_attachment_scan(
            root=root,
            top=top,
            since_iso=since_iso,
            since=since,
            skip_message_ids=skip_message_ids,
        )

    def _list_messages_with_subject_filter(
        self,
        *,
        root: str,
        top: int,
        since_iso: str,
        subject: str,
        since: datetime,
        skip_message_ids: set[str],
    ) -> list[dict] | None:
        # contains() requires ConsistencyLevel: eventual + $count=true.
        # $orderby is often rejected with contains — sort client-side instead.
        safe_subject = subject.replace("'", "''")
        query = urlencode(
            {
                "$filter": (
                    f"receivedDateTime ge {since_iso} and hasAttachments eq true "
                    f"and contains(subject,'{safe_subject}')"
                ),
                "$top": str(min(50, max(top, 1))),
                "$select": "id,subject,receivedDateTime,from,hasAttachments",
                "$count": "true",
            }
        )
        url: str | None = f"{GRAPH_BASE}/{root}/messages?{query}"
        headers = {**graph_headers(), "ConsistencyLevel": "eventual"}
        matches: list[dict] = []
        pages = 0

        try:
            while url and len(matches) < top and pages < 20:
                pages += 1
                response = requests.get(url, headers=headers, timeout=60)
                if response.status_code >= 400:
                    logger.warning(
                        "Subject-filtered Graph list failed (%s); using attachment scan. Body: %s",
                        response.status_code,
                        (response.text or "")[:300],
                    )
                    return None
                payload = response.json()
                for message in payload.get("value", []):
                    if self._message_matches(
                        message, since=since, skip_message_ids=skip_message_ids
                    ):
                        matches.append(message)
                        if len(matches) >= top:
                            break
                url = payload.get("@odata.nextLink")
        except requests.RequestException as exc:
            logger.warning("Subject-filtered Graph list error; using attachment scan: %s", exc)
            return None

        matches.sort(
            key=lambda m: m.get("receivedDateTime") or "",
            reverse=True,
        )
        logger.info(
            "Graph subject filter %r returned %s message(s) in %s page(s)",
            subject,
            len(matches),
            pages,
        )
        return matches[:top]

    def _list_messages_attachment_scan(
        self,
        *,
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

        while url and len(matches) < top and pages < 20:
            pages += 1
            response = requests.get(url, headers=graph_headers(), timeout=60)
            response.raise_for_status()
            payload = response.json()
            for message in payload.get("value", []):
                if self._message_matches(
                    message, since=since, skip_message_ids=skip_message_ids
                ):
                    matches.append(message)
                    if len(matches) >= top:
                        break
            url = payload.get("@odata.nextLink")

        logger.info(
            "Graph attachment scan for %r returned %s message(s) in %s page(s)",
            self.SEARCH_SUBJECT or self.SUBJECT_CONTAINS,
            len(matches),
            pages,
        )
        return matches

    def _list_messages_by_search(
        self,
        *,
        top: int,
        since: datetime,
        skip_message_ids: set[str],
    ) -> list[dict]:
        root = self._mailbox_root()
        url = (
            f"{GRAPH_BASE}/{root}/messages"
            f'?$search="subject:{self.SEARCH_SUBJECT}"'
            f"&$top={max(top, 100)}"
            f"&$select=id,subject,receivedDateTime,from,hasAttachments"
        )
        response = requests.get(
            url,
            headers={**graph_headers(), "ConsistencyLevel": "eventual"},
            timeout=60,
        )
        response.raise_for_status()
        matches = []
        for message in response.json().get("value", []):
            if self._message_matches(
                message, since=since, skip_message_ids=skip_message_ids
            ):
                matches.append(message)
        return matches

    def _list_attachments(self, message_id: str) -> list[dict]:
        root = self._mailbox_root()
        url = f"{GRAPH_BASE}/{root}/messages/{message_id}/attachments"
        return graph_get(url).get("value", [])

    def _attachment_bytes(self, message_id: str, attachment: dict) -> bytes | None:
        """Prefer inline contentBytes; fall back to /$value download."""
        raw = attachment.get("contentBytes")
        if raw:
            try:
                return base64.b64decode(raw)
            except Exception:
                logger.warning(
                    "Could not decode contentBytes for %s; downloading /$value",
                    attachment.get("name"),
                )

        root = self._mailbox_root()
        return graph_get_bytes(
            f"{GRAPH_BASE}/{root}/messages/{message_id}/attachments/{attachment['id']}/$value"
        )

    def _download_csv(self, message_id: str, attachment: dict) -> Path | None:
        name = attachment.get("name", "")
        if not name.lower().endswith(".csv"):
            return None
        if self.FILENAME_CONTAINS not in name.lower():
            return None

        content = self._attachment_bytes(message_id, attachment)
        if content is None:
            return None
        # Reject OLE/Excel binaries that were mislabeled as .csv
        if content.startswith(b"\xd0\xcf\x11\xe0"):
            logger.warning("Skipping non-CSV binary attachment: %s", name)
            return None
        safe_name = Path(name).name
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = self.download_dir / f"{timestamp}_{safe_name}"
        destination.write_bytes(content)
        return destination

    def _downloads_for_message(self, message: dict) -> list[dict]:
        received_at = _parse_received(message.get("receivedDateTime"))
        sender = (
            message.get("from", {})
            .get("emailAddress", {})
            .get("address")
        )
        found: list[dict] = []
        for attachment in self._list_attachments(message["id"]):
            path = self._download_csv(message["id"], attachment)
            if not path:
                continue
            found.append(
                {
                    "file_path": path,
                    "filename": path.name,
                    "email_subject": message.get("subject"),
                    "email_from": sender,
                    "email_received_at": received_at,
                    "message_id": message["id"],
                }
            )
        return found

    def fetch_csvs(
        self,
        top: int = 100,
        *,
        since: datetime | None = None,
        skip_message_ids: set[str] | None = None,
    ) -> list[dict]:
        started = datetime.now(timezone.utc)
        messages = self._search_messages(
            top=top, since=since, skip_message_ids=skip_message_ids
        )
        if not messages:
            return []

        downloads: list[dict] = []
        workers = min(_DOWNLOAD_WORKERS, len(messages))
        logger.info(
            "Downloading %s %r attachment(s) with %s worker(s)",
            len(messages),
            self.SEARCH_SUBJECT or self.SUBJECT_CONTAINS,
            workers,
        )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._downloads_for_message, message): message
                for message in messages
            }
            done = 0
            for future in as_completed(futures):
                done += 1
                try:
                    downloads.extend(future.result())
                except Exception as exc:
                    message = futures[future]
                    logger.warning(
                        "Failed downloading attachments for %s: %s",
                        message.get("subject"),
                        exc,
                    )
                if done == 1 or done % 10 == 0 or done == len(messages):
                    logger.info(
                        "Attachment download progress %s/%s for %r",
                        done,
                        len(messages),
                        self.SEARCH_SUBJECT or self.SUBJECT_CONTAINS,
                    )

        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        logger.info(
            "Fetched %s CSV(s) for %r in %.1fs",
            len(downloads),
            self.SEARCH_SUBJECT or self.SUBJECT_CONTAINS,
            elapsed,
        )
        return downloads


def _parse_received(raw: str | None) -> datetime | None:
    if not raw:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))
