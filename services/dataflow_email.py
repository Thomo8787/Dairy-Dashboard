"""Shared DataFlow CSV email fetch helpers."""

from __future__ import annotations

import base64
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

import requests

from services.graph_client import (
    GRAPH_BASE,
    _request_with_retries,
    auth_mode,
    graph_get,
    graph_get_bytes,
    graph_headers,
)

logger = logging.getLogger(__name__)

# Keep concurrency low — Graph throttles mailbox attachment reads hard (429).
# Single worker also caps peak RAM when a large Excel lands (Render free ~512MB).
_DOWNLOAD_WORKERS = 1
_OLE_MAGIC = b"\xd0\xcf\x11\xe0"
_ZIP_MAGIC = b"PK"
_CSV_EXTENSIONS = (".csv",)
_EXCEL_EXTENSIONS = (".xls", ".xlsx")
# Normal Milk Flow / Rotary CSVs are <300KB; PRK .xls ~100KB.
# One mislabeled ALH Excel was ~18MB / 65k rows and OOM'd the web dyno.
_MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024


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
            # Non-empty hit — done. Empty list can be a false miss from
            # ConsistencyLevel eventual, so fall through to attachment scan.
            if scoped:
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
                response = _request_with_retries(
                    "GET", url, timeout=60, headers=headers
                )
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
        response = _request_with_retries(
            "GET",
            url,
            timeout=60,
            headers={**graph_headers(), "ConsistencyLevel": "eventual"},
        )
        matches = []
        for message in response.json().get("value", []):
            if self._message_matches(
                message, since=since, skip_message_ids=skip_message_ids
            ):
                matches.append(message)
        logger.info(
            "Graph $search for %r returned %s message(s) after date filter",
            self.SEARCH_SUBJECT,
            len(matches),
        )
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

    def _looks_like_excel(self, name: str, content: bytes) -> bool:
        lower = name.lower()
        if lower.endswith(_EXCEL_EXTENSIONS):
            return True
        # DataFlow sometimes labels Excel as .csv
        return content.startswith(_OLE_MAGIC) or (
            lower.endswith(".csv") and content.startswith(_ZIP_MAGIC)
        )

    def _excel_to_csv_file(self, content: bytes, destination: Path, source_name: str) -> Path | None:
        """Convert .xls/.xlsx (or mislabeled Excel) into a CSV the parsers expect."""
        import gc
        import io

        import pandas as pd

        try:
            frame = pd.read_excel(io.BytesIO(content), engine="calamine")
        except Exception as exc:
            logger.warning("Could not read Excel attachment %s: %s", source_name, exc)
            return None
        if frame.empty:
            logger.warning("Excel attachment %s has no rows", source_name)
            return None
        keep = [col for col in frame.columns if not str(col).startswith("Unnamed")]
        if keep:
            frame = frame.loc[:, keep]
        destination.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(destination, index=False)
        del frame
        gc.collect()
        return destination

    def _download_csv(self, message_id: str, attachment: dict) -> Path | None:
        name = attachment.get("name", "") or ""
        lower = name.lower()
        if not (
            lower.endswith(_CSV_EXTENSIONS)
            or lower.endswith(_EXCEL_EXTENSIONS)
        ):
            return None
        if self.FILENAME_CONTAINS not in lower:
            return None

        declared = attachment.get("size")
        try:
            declared_size = int(declared) if declared is not None else None
        except (TypeError, ValueError):
            declared_size = None
        if declared_size is not None and declared_size > _MAX_ATTACHMENT_BYTES:
            logger.warning(
                "Skipping oversized attachment %s (declared %s bytes > %s limit)",
                name,
                declared_size,
                _MAX_ATTACHMENT_BYTES,
            )
            return None

        content = self._attachment_bytes(message_id, attachment)
        if content is None:
            return None

        if len(content) > _MAX_ATTACHMENT_BYTES:
            logger.warning(
                "Skipping oversized attachment %s (%s bytes > %s limit)",
                name,
                len(content),
                _MAX_ATTACHMENT_BYTES,
            )
            return None

        safe_stem = Path(name).stem
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = self.download_dir / f"{timestamp}_{safe_stem}.csv"

        if self._looks_like_excel(name, content):
            converted = self._excel_to_csv_file(content, destination, name)
            if converted is None:
                logger.warning("Skipping unreadable Excel attachment: %s", name)
                return None
            logger.info("Converted Excel attachment %s → %s", name, converted.name)
            return converted

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
        failures = 0
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
                    failures += 1
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
                # Small pause between completions to stay under Graph throttle.
                if done < len(messages):
                    time.sleep(0.2)

        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        logger.info(
            "Fetched %s CSV(s) for %r in %.1fs (%s download failure(s))",
            len(downloads),
            self.SEARCH_SUBJECT or self.SUBJECT_CONTAINS,
            elapsed,
            failures,
        )
        if messages and not downloads:
            if failures:
                raise RuntimeError(
                    f"Found {len(messages)} '{self.SEARCH_SUBJECT or self.SUBJECT_CONTAINS}' "
                    f"email(s) but downloaded 0 usable CSV/Excel attachments "
                    f"({failures} attachment request(s) failed). "
                    "Check attachment names/formats, or retry if Graph was throttling."
                )
            # Intentional skips (oversized / wrong type) — not a hard failure.
            logger.info(
                "Found %s %r email(s) but no usable attachments after filters "
                "(oversized or unmatched); treating as nothing to import.",
                len(messages),
                self.SEARCH_SUBJECT or self.SUBJECT_CONTAINS,
            )
            return []
        return downloads


def _parse_received(raw: str | None) -> datetime | None:
    if not raw:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))
