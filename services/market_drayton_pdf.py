"""Parse Market Drayton (Barbers / MDM) sellers lists and BACS statements.

Text PDFs are read from word positions. Scanned pages fall back to OCR.
The animal price is the rightmost money on the ear-tag row. Commission, VAT,
tolls, and the statement gross are not animal lines.
"""

from __future__ import annotations

import datetime as dt
import io
import re
from typing import Any

import pdfplumber

from services.cattle_sale_pdf import extract_pdf_images, ocr_words_from_image
from services.neilds_pdf import repair_etag

BUYER_MARKET_DRAYTON = "Market Drayton"

# Market Drayton account number -> herd. Bank Farm has no account yet.
_ACCOUNT_FARMS: tuple[tuple[str, str], ...] = (
    ("T1588", "SFR"),
    ("T1386", "PRK"),
    ("T934", "COF"),
    ("T304", "ALH"),
)
_FARM_MARKERS: tuple[tuple[str, str], ...] = (
    ("ASTON LOWER", "ALH"),
    ("ASTON JUXTA", "ALH"),
    ("ASTON LO", "ALH"),
    ("BANK FARM", "BNK"),
    ("PARK HALL", "SFR"),
    ("PARR HALL", "SFR"),
    ("THE PARKES", "PRK"),
    ("CHERRY ORCHARD", "COF"),
    ("THOMASSON LIVESTOCK", "ALH"),
)
_TAG_RE = re.compile(r"UK\s*[0-9A-Z]{10,16}", re.IGNORECASE)
_SEP_DATE_RE = re.compile(
    r"(?<!\d)(\d{1,2})[-./](\d{1,2})[-./](\d{2,4})(?!\d)"
)
_COMPACT_DATE_RE = re.compile(r"(?<!\d)(\d{2})(\d{2})(\d{4})(?!\d)")
_MONEY_RE = re.compile(r"^-?\d{1,5}\.\d{2}$")
# OCR sometimes reads the decimal point as a colon, then a stray mark: 240:007.
_OCR_MONEY_RE = re.compile(r"(\d{1,5})[.:](\d{2})")
_WEIGHT_RE = re.compile(r"^\d{2,3}\.\d$")
_SKIP_ROW_RE = re.compile(
    r"gross|commission|tolls|\bvat\b|\bnet\b|\bpph\b|credited|sort code|retention",
    re.IGNORECASE,
)


def looks_like_market_drayton_pdf(text: str, source_file: str | None = None) -> bool:
    haystack = f"{source_file or ''}\n{text}".lower()
    if any(
        phrase in haystack
        for phrase in (
            "market drayton",
            "barbers",
            "mdm-auction",
            "orange market",
        )
    ):
        return True
    name = (source_file or "").lower()
    if "sellers list" in name:
        return True
    compact = re.sub(r"[^a-z0-9]", "", haystack)
    return any(account.lower() in compact for account, _farm in _ACCOUNT_FARMS)


def _date_from_parts(day: int, month: int, year: int) -> dt.date | None:
    if year < 100:
        year += 2000
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def _date_from_match(match: re.Match[str]) -> dt.date | None:
    return _date_from_parts(int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _date_from_text(text: str | None) -> dt.date | None:
    if not text:
        return None
    separated = _SEP_DATE_RE.search(text)
    if separated:
        return _date_from_match(separated)
    compact = _COMPACT_DATE_RE.search(text)
    if compact:
        return _date_from_match(compact)
    return None


def _account_farm(text: str | None) -> str | None:
    compact = re.sub(r"[^A-Za-z0-9]", "", text or "").upper()
    for account, farm in _ACCOUNT_FARMS:
        if account in compact:
            return farm
    return None


def _farm_from_address(text: str | None) -> str | None:
    haystack = (text or "").upper()
    for marker, farm in _FARM_MARKERS:
        if marker in haystack:
            return farm
    return None


def _etag_from_text(text: str) -> str | None:
    match = _TAG_RE.search(text.replace(" ", ""))
    if match is None:
        match = _TAG_RE.search(text)
    if match is None:
        return None
    etag = repair_etag(match.group(0))
    if not etag or not etag.startswith("UK"):
        return None
    digits = re.sub(r"\D", "", etag[2:])
    if len(digits) > 12:
        digits = digits[:12]
    if len(digits) < 10:
        return None
    return f"UK{digits}"


def _cluster_rows(words: list[dict[str, Any]], y_tol: float = 8.0) -> list[list[dict[str, Any]]]:
    ordered = sorted(words, key=lambda word: (word["y"], word["x"]))
    rows: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    anchor_y: float | None = None
    for word in ordered:
        if anchor_y is None or abs(word["y"] - anchor_y) <= y_tol:
            current.append(word)
            if anchor_y is None:
                anchor_y = word["y"]
        else:
            rows.append(current)
            current = [word]
            anchor_y = word["y"]
    if current:
        rows.append(current)
    return rows


def _row_text(row: list[dict[str, Any]]) -> str:
    return " ".join(word["text"] for word in sorted(row, key=lambda item: item["x"]))


def _row_tag(row: list[dict[str, Any]]) -> tuple[str | None, float | None]:
    for word in sorted(row, key=lambda item: item["x"]):
        etag = _etag_from_text(word["text"])
        if etag:
            return etag, float(word["x"])
    return _etag_from_text(_row_text(row)), None


def _money_value(text: str) -> float | None:
    raw = text.replace(",", "").replace("£", "").replace(" ", "")
    if raw.startswith("-"):
        return None
    if _MONEY_RE.match(raw):
        value = float(raw)
    else:
        match = _OCR_MONEY_RE.search(raw)
        if match is None:
            return None
        value = float(f"{match.group(1)}.{match.group(2)}")
    if value < 20 or value > 8000:
        return None
    return round(value, 2)


def _row_amount(row: list[dict[str, Any]], tag_x: float | None) -> tuple[float | None, float | None]:
    """Rightmost animal price, and the x of that token."""
    found: list[tuple[float, float]] = []
    for word in row:
        if tag_x is not None and word["x"] <= tag_x:
            continue
        value = _money_value(word["text"])
        if value is None:
            continue
        found.append((float(word["x"]), value))
    if not found:
        return None, None
    found.sort(key=lambda item: item[0])
    return found[-1][1], found[-1][0]


def _row_weight(
    row: list[dict[str, Any]],
    tag_x: float | None,
    amount_x: float | None,
) -> float | None:
    if tag_x is None or amount_x is None:
        return None
    for word in sorted(row, key=lambda item: item["x"]):
        if word["x"] <= tag_x or word["x"] >= amount_x:
            continue
        raw = word["text"].replace(",", "").replace(" ", "")
        if not _WEIGHT_RE.match(raw):
            continue
        value = float(raw)
        if 80 <= value <= 900:
            return round(value, 1)
    return None


def _text_pages(content: bytes) -> list[list[dict[str, Any]]]:
    pages: list[list[dict[str, Any]]] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            words: list[dict[str, Any]] = []
            for word in page.extract_words() or []:
                text = (word.get("text") or "").strip()
                if not text:
                    continue
                words.append(
                    {
                        "text": text,
                        "x": float(word["x0"]),
                        "y": float(word["top"]),
                    }
                )
            if words:
                pages.append(words)
    return pages


def _pages_have_tag(pages: list[list[dict[str, Any]]]) -> bool:
    for page in pages:
        if _etag_from_text(" ".join(word["text"] for word in page)):
            return True
    return False


def _ocr_pages(content: bytes) -> list[list[dict[str, Any]]]:
    pages: list[list[dict[str, Any]]] = []
    for image in extract_pdf_images(content):
        words = ocr_words_from_image(image, scale=2)
        if words:
            pages.append(words)
    return pages


def _lines_from_pages(
    pages: list[list[dict[str, Any]]],
    sale_date: dt.date | None,
) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in pages:
        for row in _cluster_rows(page):
            if _SKIP_ROW_RE.search(_row_text(row)):
                continue
            etag, tag_x = _row_tag(row)
            amount, amount_x = _row_amount(row, tag_x)
            if not etag or amount is None or etag in seen:
                continue
            seen.add(etag)
            weight = _row_weight(row, tag_x, amount_x)
            lines.append(
                {
                    "etag": etag,
                    "cold_weight_kg": round(float(weight or 0.0), 2),
                    "amount_gbp": amount,
                    "reject_kg": 0.0,
                    "kill_date": sale_date,
                    "is_rejected": False,
                }
            )
    return lines


def parse_market_drayton_pdf(
    content: bytes,
    *,
    mailbox_farm: str | None = None,
    source_file: str | None = None,
) -> dict[str, Any]:
    warnings: list[str] = []
    pages = _text_pages(content)
    if not _pages_have_tag(pages):
        pages = _ocr_pages(content)
        if not pages:
            warnings.append("PDF contained no page text to read")

    page_text = "\n".join(
        word["text"] for page in pages for word in page
    )
    sale_date = _date_from_text(source_file) or _date_from_text(page_text)
    farm = (
        _account_farm(source_file)
        or _account_farm(page_text)
        or _farm_from_address(page_text)
        or mailbox_farm
    )
    lines = _lines_from_pages(pages, sale_date)
    if not lines:
        warnings.append("No sale lines extracted from PDF")
    if sale_date is None:
        warnings.append("Could not parse sale date")
    if farm is None:
        warnings.append("Could not determine farm from the account number or address")

    return {
        "farm": farm,
        "sale_date": sale_date,
        "lines": lines,
        "warnings": warnings,
        "buyer": BUYER_MARKET_DRAYTON,
    }
