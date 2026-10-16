"""Parse Warrendale Wagyu self-bill / purchase-order remittances.

Ear tags are listed in a comma-separated block. Farm is left unset so import
resolves it from the DairyComp ear-tag event.
"""

from __future__ import annotations

import datetime as dt
import io
import re
from typing import Any

import pdfplumber

from services.cattle_sale_pdf import extract_pdf_text, normalize_etag

BUYER_WARRENDALE = "Warrendale"

_TAG_RE = re.compile(r"UK\s*\d{10,15}", re.IGNORECASE)
_COLLECTION_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\s*:")
_SELF_BILL_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{4})\b",
    re.IGNORECASE,
)
_QTY_PRICE_RE = re.compile(
    r"(\d+(?:,\d{3})*(?:\.\d+)?)\s+(\d+(?:,\d{3})*(?:\.\d+)?)\s+Zero\s+Rated",
    re.IGNORECASE,
)
_FLEX_DATE_RE = re.compile(
    r"(\d{1,2})\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s*(\d{4})",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")
_ROW_TOP_TOLERANCE = 3.0
_DATE_BELOW_LABEL = 22.0
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
        qty = float(match.group(1).replace(",", ""))
        price = float(match.group(2).replace(",", ""))
        if price >= 20 and (tag_count == 0 or abs(qty - tag_count) < 0.5 or qty >= 1):
            return round(price, 2)
    return None


def _compact(text: str) -> str:
    return "".join((text or "").split()).lower()


def _is_self_bill(text: str) -> bool:
    lowered = text.lower()
    if "self-bill" in lowered or "self bill" in lowered or "selfbill" in _compact(text):
        return True
    return _parse_collection_date(text) is not None


def _is_purchase_order(text: str) -> bool:
    return "purchaseorder" in _compact(text) and not _is_self_bill(text)


def _pdf_words(content: bytes) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            words.extend(page.extract_words() or [])
    return words


def _word_rows(words: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    rows: list[list[dict[str, Any]]] = []
    for word in sorted(words, key=lambda item: (item["top"], item["x0"])):
        if rows and abs(word["top"] - rows[-1][0]["top"]) <= _ROW_TOP_TOLERANCE:
            rows[-1].append(word)
        else:
            rows.append([word])
    for row in rows:
        row.sort(key=lambda item: item["x0"])
    return rows


def _parse_flexible_date(text: str) -> dt.date | None:
    match = _FLEX_DATE_RE.search(text)
    if not match:
        return None
    month = _MONTHS.get(match.group(2)[:3].lower())
    if month is None:
        return None
    try:
        return dt.date(int(match.group(3)), month, int(match.group(1)))
    except ValueError:
        return None


def _label_span(row: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    target = "purchaseorderdate"
    for start in range(len(row)):
        acc = ""
        for end in range(start, min(start + 4, len(row))):
            acc += _compact(row[end]["text"])
            if acc == target:
                return row[start : end + 1]
            if not target.startswith(acc):
                break
    return None


def _purchase_order_date(words: list[dict[str, Any]]) -> dt.date | None:
    label: list[dict[str, Any]] | None = None
    for row in _word_rows(words):
        label = _label_span(row)
        if label is not None:
            break
    if not label:
        return None
    x0 = min(word["x0"] for word in label)
    x1 = max(word["x1"] for word in label)
    top = min(word["top"] for word in label)
    under = [
        word
        for word in words
        if top + 1 < word["top"] <= top + _DATE_BELOW_LABEL
        and word["x0"] <= x1 + 8
        and word["x1"] >= x0 - 4
    ]
    under.sort(key=lambda word: (word["top"], word["x0"]))
    return _parse_flexible_date("".join(word["text"] for word in under))


def _plain_number(text: str) -> float | None:
    token = (text or "").strip()
    if not _NUMBER_RE.fullmatch(token):
        return None
    return float(token.replace(",", ""))


def _purchase_order_price(words: list[dict[str, Any]]) -> float | None:
    for row in _word_rows(words):
        if any("total" in _compact(word["text"]) for word in row):
            continue
        for index, word in enumerate(row):
            token = _compact(word["text"])
            zero_rated = token == "zerorated" or (
                token == "zero"
                and index + 1 < len(row)
                and _compact(row[index + 1]["text"]) == "rated"
            )
            if not zero_rated:
                continue
            for previous in reversed(row[:index]):
                number = _plain_number(previous["text"])
                if number is None:
                    continue
                if number >= 20:
                    return round(number, 2)
                break
    return None


def _purchase_order_layout(content: bytes) -> tuple[float | None, dt.date | None]:
    words = _pdf_words(content)
    return _purchase_order_price(words), _purchase_order_date(words)


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
    if _is_purchase_order(text) and (amount is None or sale_date is None):
        po_amount, po_date = _purchase_order_layout(content)
        if amount is None:
            amount = po_amount
        if sale_date is None:
            sale_date = po_date
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
