"""Parse Warrendale Wagyu self-bill / purchase-order remittances.

Ear tags are listed in a comma-separated block. Farm is left unset so import
resolves it from the DairyComp ear-tag event.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from services.cattle_sale_pdf import extract_pdf_text, normalize_etag

BUYER_WARRENDALE = "Warrendale"

_TAG_RE = re.compile(r"UK\s*\d{10,15}", re.IGNORECASE)
_COLLECTION_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\s*:")
_SELF_BILL_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{4})\b",
    re.IGNORECASE,
)
_QTY_PRICE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+Zero\s+Rated",
    re.IGNORECASE,
)
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def looks_like_warrendale_pdf(text: str, source_file: str | None = None) -> bool:
    name = (source_file or "").lower()
    if name.startswith("purchaseorder") or name.startswith("po26-"):
        return True
    haystack = f"{source_file or ''}\n{text}".lower()
    if "warrendale" in haystack or "wagyu" in haystack:
        return True
    return "self-bill" in haystack and "calves" in haystack and "po26" in haystack


def looks_like_warrendale_pdf_bytes(content: bytes, source_file: str | None = None) -> bool:
    if source_file and looks_like_warrendale_pdf("", source_file):
        return True
    try:
        text = extract_pdf_text(content)
    except Exception:
        return False
    return looks_like_warrendale_pdf(text, source_file)


def _parse_collection_date(text: str) -> dt.date | None:
    match = _COLLECTION_DATE_RE.search(text)
    if not match:
        return None
    day, month, year = match.group(1), match.group(2), match.group(3)
    if len(year) == 2:
        year = f"20{year}"
    try:
        return dt.date(int(year), int(month), int(day))
    except ValueError:
        return None


def _parse_self_bill_date(text: str) -> dt.date | None:
    match = _SELF_BILL_DATE_RE.search(text)
    if not match:
        return None
    month = _MONTHS.get(match.group(2)[:3].lower())
    if month is None:
        return None
    try:
        return dt.date(int(match.group(3)), month, int(match.group(1)))
    except ValueError:
        return None


def _unit_price(text: str, tag_count: int) -> float | None:
    match = _QTY_PRICE_RE.search(text)
    if match:
        qty = float(match.group(1))
        price = float(match.group(2))
        if price >= 20 and (tag_count == 0 or abs(qty - tag_count) < 0.5 or qty >= 1):
            return round(price, 2)
    return None


def parse_warrendale_pdf(
    content: bytes,
    *,
    mailbox_farm: str | None = None,
    source_file: str | None = None,
) -> dict[str, Any]:
    del mailbox_farm
    warnings: list[str] = []
    text = extract_pdf_text(content)
    if not text.strip():
        return {
            "farm": None,
            "sale_date": None,
            "lines": [],
            "warnings": ["PDF contained no extractable text"],
            "buyer": BUYER_WARRENDALE,
        }

    if not looks_like_warrendale_pdf(text, source_file):
        warnings.append("PDF does not look like a Warrendale purchase order")

    tags: list[str] = []
    seen: set[str] = set()
    for match in _TAG_RE.finditer(text):
        etag = normalize_etag(match.group(0))
        if not etag or etag in seen:
            continue
        seen.add(etag)
        tags.append(etag)

    amount = _unit_price(text, len(tags))
    sale_date = _parse_collection_date(text) or _parse_self_bill_date(text)
    lines: list[dict[str, Any]] = []
    if amount is None:
        warnings.append("Could not parse unit price from purchase order")
    else:
        for etag in tags:
            lines.append(
                {
                    "etag": etag,
                    "cold_weight_kg": 0.0,
                    "amount_gbp": amount,
                    "reject_kg": 0.0,
                    "kill_date": sale_date,
                    "is_rejected": False,
                }
            )

    if not lines:
        warnings.append("No sale lines extracted from PDF")
    if sale_date is None:
        warnings.append("Could not parse collection date from purchase order")

    return {
        "farm": None,
        "sale_date": sale_date,
        "lines": lines,
        "warnings": warnings,
        "buyer": BUYER_WARRENDALE,
    }
