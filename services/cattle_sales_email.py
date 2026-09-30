"""Fetch cattle remittance PDFs from Outlook by sender domain.

Neilds: auctionmarts.com
Pickstock: pickstocktelford.co.uk
Gamechanger: gamechangerfarming.com
Market Drayton: barbers-auctions.co.uk
Warrendale: not fetched from email.
"""

from __future__ import annotations

import base64
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote, urlencode

from services.blade_pdf import looks_like_blade_pdf
from services.cattle_sale_pdf import extract_pdf_text
from services.graph_client import (
    GRAPH_BASE,
    _request_with_retries,
    auth_mode,
    graph_get,
    graph_get_bytes,
    graph_headers,
    require_azure_config,
)
from services.market_drayton_pdf import looks_like_market_drayton_pdf
from services.neilds_pdf import looks_like_neilds_pdf
from services.pickstock_pdf import looks_like_pickstock_pdf

logger = logging.getLogger(__name__)

CATTLE_SALES_LOOKBACK_DAYS = 45
_MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024

# Sender domain -> parser hint used on import. Warrendale is local-file only.
_SENDER_DOMAINS: tuple[tuple[str, str], ...] = (
    ("auctionmarts.com", "neilds"),
    ("pickstocktelford.co.uk", "pickstock"),
    ("gamechangerfarming.com", "gamechanger"),
    ("barbers-auctions.co.uk", "market_drayton"),
)


def _parse_received(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def outlook_cattle_sales_configured() -> bool:
    if require_azure_config():
        return False
    if auth_mode() == "application":
        return bool(os.environ.get("OUTLOOK_MAILBOX", "").strip())
    return True


def _message_sender(message: dict) -> str:
    for key in ("from", "sender"):
        address = (
            (message.get(key) or {})
            .get("emailAddress", {})
            .get("address", "")
        )
        if address:
            return str(address).lower()
    return ""


def _parser_hint_for_sender(address: str) -> str | None:
    haystack = (address or "").lower()
    for domain, hint in _SENDER_DOMAINS:
        if domain in haystack:
            return hint
    return None


def _pdf_matches_hint(content: bytes, name: str, hint: str) -> bool:
    if hint == "neilds":
        looks = looks_like_neilds_pdf
    elif hint == "pickstock":
        looks = looks_like_pickstock_pdf
    elif hint == "gamechanger":
        looks = looks_like_blade_pdf
    elif hint == "market_drayton":
        looks = looks_like_market_drayton_pdf
    else:
        return False
    if looks("", name):
        return True
    try:
        text = extract_pdf_text(content)
    except Exception:
        text = ""
    if looks(text, name):
        return True
    # Scanned remittances often have no text; still take PDFs from the known sender.
    return not bool((text or "").strip())


class CattleSalePdfEmailService:
    """Pull remittance PDFs from known livestock-buyer sender domains."""

    def __init__(self) -> None:
        self.mailbox = os.environ.get("OUTLOOK_MAILBOX", "").strip()
        if auth_mode() == "application" and not self.mailbox:
            raise RuntimeError("OUTLOOK_MAILBOX is not set")

    def _mailbox_root(self) -> str:
        if auth_mode() == "delegated" and not self.mailbox:
            return "me"
        return f"users/{quote(self.mailbox)}"

    def _received_ok(self, message: dict, since: datetime | None) -> bool:
        if since is None:
            return True
        received_at = _parse_received(message.get("receivedDateTime"))
        since_aware = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
        return received_at is not None and received_at >= since_aware

    def _from_filter(self, since: datetime) -> str:
        since_iso = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        domain_clause = " or ".join(
            f"contains(from/emailAddress/address,'{domain}')"
            for domain, _hint in _SENDER_DOMAINS
        )
        return (
            f"receivedDateTime ge {since_iso} and hasAttachments eq true "
            f"and ({domain_clause})"
        )

    def _folder_targets(self) -> list[tuple[str, str]]:
        """Inbox plus Inbox child folders, or CATTLE_SALES_MAIL_FOLDERS names."""
        root = self._mailbox_root()
        configured = [
            part.strip()
            for part in (os.environ.get("CATTLE_SALES_MAIL_FOLDERS") or "").split(",")
            if part.strip()
        ]
        if configured:
            return self._folders_by_display_name(configured)

        inbox = graph_get(f"{GRAPH_BASE}/{root}/mailFolders/inbox?$select=id,displayName")
        inbox_id = inbox.get("id")
        if not inbox_id:
            return []
        targets = [(inbox_id, inbox.get("displayName") or "Inbox")]
        children = graph_get(
            f"{GRAPH_BASE}/{root}/mailFolders/inbox/childFolders"
            f"?$select=id,displayName&$top=50"
        )
        for child in children.get("value", []) or []:
            child_id = child.get("id")
            if child_id:
                targets.append((child_id, child.get("displayName") or "folder"))
        logger.info(
            "Cattle-sale mail folders: %s",
            ", ".join(name for _folder_id, name in targets),
        )
        return targets

    def _folders_by_display_name(self, names: list[str]) -> list[tuple[str, str]]:
        root = self._mailbox_root()
        wanted = {name.lower() for name in names}
        found: list[tuple[str, str]] = []
        url: str | None = (
            f"{GRAPH_BASE}/{root}/mailFolders?$select=id,displayName&$top=50"
        )
        pages = 0
        while url and pages < 10:
            pages += 1
            payload = graph_get(url)
            for folder in payload.get("value", []) or []:
                display = (folder.get("displayName") or "").strip()
                folder_id = folder.get("id")
                if folder_id and display.lower() in wanted:
                    found.append((folder_id, display))
            url = payload.get("@odata.nextLink")
        missing = wanted - {name.lower() for _folder_id, name in found}
        if missing:
            logger.warning("Cattle-sale mail folders not found: %s", ", ".join(sorted(missing)))
        return found

    def _page_messages(self, url: str | None, *, top: int, since: datetime) -> list[dict]:
        by_id: dict[str, dict] = {}
        pages = 0
        max_pages = max(2, min(12, (top + 49) // 50 + 2))
        try:
            while url and len(by_id) < top and pages < max_pages:
                pages += 1
                response = _request_with_retries("GET", url, timeout=60, headers=graph_headers())
                payload = response.json()
                for message in payload.get("value", []):
                    if not message.get("hasAttachments"):
                        continue
                    if not self._received_ok(message, since):
                        continue
                    if not _parser_hint_for_sender(_message_sender(message)):
                        continue
                    message_id = message.get("id") or ""
                    if message_id:
                        by_id[message_id] = message
                    if len(by_id) >= top:
                        break
                url = payload.get("@odata.nextLink")
        except Exception:
            logger.exception("Cattle-sale sender message list failed")
        return list(by_id.values())

    def _list_folder_messages(self, folder_id: str, *, top: int, since: datetime) -> list[dict]:
        root = self._mailbox_root()
        query = urlencode(
            {
                "$filter": self._from_filter(since),
                "$orderby": "receivedDateTime desc",
                "$top": str(min(50, max(top, 1))),
                "$select": "id,subject,receivedDateTime,from,sender,hasAttachments",
            }
        )
        url = f"{GRAPH_BASE}/{root}/mailFolders/{folder_id}/messages?{query}"
        return self._page_messages(url, top=top, since=since)

    def _list_mailbox_messages(self, *, top: int, since: datetime) -> list[dict]:
        root = self._mailbox_root()
        query = urlencode(
            {
                "$filter": self._from_filter(since),
                "$orderby": "receivedDateTime desc",
                "$top": str(min(50, max(top, 1))),
                "$select": "id,subject,receivedDateTime,from,sender,hasAttachments",
            }
        )
        url = f"{GRAPH_BASE}/{root}/messages?{query}"
        return self._page_messages(url, top=top, since=since)

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

    def iter_pdfs(
        self,
        *,
        since: datetime | None = None,
        skip_message_ids: set[str] | None = None,
        skip_filenames: set[str] | None = None,
        top: int = 40,
    ) -> Iterator[dict[str, Any]]:
        """Yield one remittance at a time so the cron never holds the whole batch."""
        since_aware = since or (
            datetime.now(timezone.utc) - timedelta(days=CATTLE_SALES_LOOKBACK_DAYS)
        )
        if since_aware.tzinfo is None:
            since_aware = since_aware.replace(tzinfo=timezone.utc)
        skip_ids = skip_message_ids or set()
        skip_names = {name.lower() for name in (skip_filenames or set())}

        by_id: dict[str, dict] = {}
        try:
            for folder_id, _name in self._folder_targets():
                for message in self._list_folder_messages(
                    folder_id, top=top, since=since_aware
                ):
                    message_id = message.get("id") or ""
                    if message_id and message_id not in skip_ids:
                        by_id[message_id] = message
        except Exception:
            logger.exception("Cattle-sale Inbox folder scan failed")

        for message in self._list_mailbox_messages(top=top, since=since_aware):
            message_id = message.get("id") or ""
            if message_id and message_id not in skip_ids and message_id not in by_id:
                by_id[message_id] = message

        logger.info("Cattle-sale mailbox scan: %s message(s)", len(by_id))
        for message in by_id.values():
            message_id = message.get("id") or ""
            hint = _parser_hint_for_sender(_message_sender(message))
            if not hint:
                continue
            try:
                attachments = self._list_attachments(message_id)
            except Exception:
                logger.exception("Could not list attachments for cattle-sale message")
                continue
            for attachment in attachments:
                name = Path(attachment.get("name") or "remittance.pdf").name
                if not name.lower().endswith(".pdf"):
                    continue
                if name.lower() in skip_names:
                    continue
                size = int(attachment.get("size") or 0)
                if size > _MAX_ATTACHMENT_BYTES:
                    logger.warning("Skipping oversized cattle-sale PDF %s (%s bytes)", name, size)
                    continue
                try:
                    content = self._attachment_bytes(message_id, attachment)
                except Exception:
                    logger.exception("Could not download cattle-sale PDF %s", name)
                    continue
                if not content or not content.startswith(b"%PDF"):
                    continue
                if not _pdf_matches_hint(content, name, hint):
                    continue
                logger.info("Cattle-sale PDF %s (%s bytes)", name, len(content))
                yield {
                    "content": content,
                    "source_file": name,
                    "message_id": message_id,
                    "subject": message.get("subject"),
                    "received_at": _parse_received(message.get("receivedDateTime")),
                    "parser_hint": hint,
                }

    def fetch_pdfs(
        self,
        *,
        since: datetime | None = None,
        skip_message_ids: set[str] | None = None,
        skip_filenames: set[str] | None = None,
        top: int = 40,
    ) -> list[dict[str, Any]]:
        return list(
            self.iter_pdfs(
                since=since,
                skip_message_ids=skip_message_ids,
                skip_filenames=skip_filenames,
                top=top,
            )
        )
