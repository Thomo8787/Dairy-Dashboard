"""Fetch Blade Payment Advice PDFs from the DataFlow Outlook mailbox."""

from __future__ import annotations

import base64
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from services.blade_pdf import looks_like_blade_pdf, looks_like_blade_pdf_bytes
from services.warrendale_pdf import looks_like_warrendale_pdf, looks_like_warrendale_pdf_bytes
from services.graph_client import (
    GRAPH_BASE,
    _request_with_retries,
    auth_mode,
    graph_get,
    graph_get_bytes,
    graph_headers,
    require_azure_config,
)

logger = logging.getLogger(__name__)

CATTLE_SALES_LOOKBACK_DAYS = 45
_MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
_SUBJECT_FRAGMENTS = (
    "Payment Advice",
    "PaymentAdvice",
    "Gamechanger",
    "GameChanger",
    "Purchase Order",
    "PurchaseOrder",
    "Warrendale",
    "Wagyu",
)
_SEARCH_TERMS = ("PaymentAdvice", "PurchaseOrder")


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


class CattleSalePdfEmailService:
    """Pull Blade Payment Advice PDFs from the same mailbox as other farm mail."""

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

    def _list_filtered_messages(self, *, top: int, since: datetime) -> list[dict]:
        root = self._mailbox_root()
        since_iso = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        subject_clause = " or ".join(
            f"contains(subject,'{fragment.replace(chr(39), chr(39)*2)}')"
            for fragment in _SUBJECT_FRAGMENTS
        )
        scoped = (
            f"receivedDateTime ge {since_iso} and hasAttachments eq true "
            f"and ({subject_clause})"
        )
        query = urlencode(
            {
                "$filter": scoped,
                "$orderby": "receivedDateTime desc",
                "$top": str(min(50, max(top, 1))),
                "$select": "id,subject,receivedDateTime,from,hasAttachments",
            }
        )
        url: str | None = f"{GRAPH_BASE}/{root}/messages?{query}"
        by_id: dict[str, dict] = {}
        pages = 0
        max_pages = max(4, min(20, (top + 49) // 50 + 2))
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
                    message_id = message.get("id") or ""
                    if message_id:
                        by_id[message_id] = message
                    if len(by_id) >= top:
                        break
                url = payload.get("@odata.nextLink")
        except Exception:
            logger.exception("Cattle-sale subject-filtered Graph list failed")
        return list(by_id.values())

    def _search_named_messages(self, term: str, *, top: int, since: datetime) -> list[dict]:
        root = self._mailbox_root()
        query = urlencode(
            {
                "$search": f'"{term}"',
                "$top": str(min(25, max(top, 1))),
                "$select": "id,subject,receivedDateTime,from,hasAttachments",
            }
        )
        url = f"{GRAPH_BASE}/{root}/messages?{query}"
        headers = {**graph_headers(), "ConsistencyLevel": "eventual"}
        matches: list[dict] = []
        try:
            response = _request_with_retries("GET", url, timeout=60, headers=headers)
            for message in response.json().get("value", []):
                if not message.get("hasAttachments"):
                    continue
                if not self._received_ok(message, since):
                    continue
                matches.append(message)
        except Exception:
            logger.exception("Cattle-sale %s Graph search failed", term)
        return matches

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
        skip_filenames: set[str] | None = None,
        top: int = 40,
    ) -> list[dict[str, Any]]:
        since_aware = since or (
            datetime.now(timezone.utc) - timedelta(days=CATTLE_SALES_LOOKBACK_DAYS)
        )
        if since_aware.tzinfo is None:
            since_aware = since_aware.replace(tzinfo=timezone.utc)
        skip_ids = skip_message_ids or set()
        skip_names = {name.lower() for name in (skip_filenames or set())}

        by_id: dict[str, dict] = {}
        for message in self._list_filtered_messages(top=top, since=since_aware):
            message_id = message.get("id") or ""
            if message_id and message_id not in skip_ids:
                by_id[message_id] = message
        for term in _SEARCH_TERMS:
            for message in self._search_named_messages(term, top=top, since=since_aware):
                message_id = message.get("id") or ""
                if message_id and message_id not in skip_ids and message_id not in by_id:
                    by_id[message_id] = message

        found: list[dict[str, Any]] = []
        for message in by_id.values():
            message_id = message.get("id") or ""
            for attachment in self._list_attachments(message_id):
                name = Path(attachment.get("name") or "payment-advice.pdf").name
                if not name.lower().endswith(".pdf"):
                    continue
                if name.lower() in skip_names:
                    continue
                size = int(attachment.get("size") or 0)
                if size > _MAX_ATTACHMENT_BYTES:
                    logger.warning("Skipping oversized cattle-sale PDF %s (%s bytes)", name, size)
                    continue
                content = self._attachment_bytes(message_id, attachment)
                if not content or not content.startswith(b"%PDF"):
                    continue
                name_low = name.lower()
                name_ok = name_low.startswith("paymentadvice") or name_low.startswith(
                    "purchaseorder"
                )
                if not name_ok:
                    if not looks_like_blade_pdf("", name) and not looks_like_warrendale_pdf("", name):
                        if not looks_like_blade_pdf_bytes(
                            content, name
                        ) and not looks_like_warrendale_pdf_bytes(content, name):
                            continue
                found.append(
                    {
                        "content": content,
                        "source_file": name,
                        "message_id": message_id,
                        "subject": message.get("subject"),
                        "received_at": _parse_received(message.get("receivedDateTime")),
                    }
                )
        logger.info("Fetched %s cattle-sale PDF(s) from %s message(s)", len(found), len(by_id))
        return found
