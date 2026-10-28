"""Parse Mold stock-sale BACS statements from J Bradburne Price.

The pages are scanned. Each animal row has an ear tag and a sale price.
There is no cold weight: these are live calves sold per head. The lot price
is the animal amount. Commission and VAT are statement charges, not part of
that price. Farm comes from the vendor address, not the account number and
not the auction town Mold.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from services.cattle_sale_pdf import extract_pdf_images, ocr_words_from_image

BUYER_MOLD = "Mold"

_SALE_PHRASE = "mold stock sale"
_TAG_BODY_RE = re.compile(r"^[0-9A-Z]{10,16}$")
_SHORT_DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{2})")
_FILENAME_DATE_RE = re.compile(r"(?<!\d)(\d{2})(\d{2})(\d{4})(?!\d)")
_AMOUNT_RE = re.compile(r"-?\d+\.\d{2}")
_FARM_CODE_RE = re.compile(r"(?<![A-Z])(ALH|BNK|SFR|PRK|COF)(?![A-Z])")
_SKIP_ROW_RE = re.compile(
    r"gross|commission|charges|\bvat\b|\bnet\b|\bpph\b|credited",
    re.IGNORECASE,
)
# Windows OCR reads a 9 on these statements as g. G is 6 on other buyers.
_OCR_DIGIT = str.maketrans(
    {
        "O": "0",
        "Q": "0",
        "D": "0",
        "I": "1",
        "L": "1",
        "Z": "2",
        "S": "5",
        "G": "9",
        "B": "8",
    }
)
_FARM_MARKERS: tuple[tuple[str, str], ...] = (
    ("PARK HALL", "SFR"),
    ("PARR HALL", "SFR"),
    ("ASTON LOWER", "ALH"),
    ("ASTON JUXTA", "ALH"),
    ("BANK FARM", "BNK"),
    ("THE PARKES", "PRK"),
    ("CHERRY ORCHARD", "COF"),
)


def looks_like_mold_pdf(text: str, source_file: str | None = None) -> bool:
    haystack = f"{source_file or ''}\n{text}".lower()
    return _SALE_PHRASE in haystack


def _date_from_parts(day: int, month: int, year: int) -> dt.date | None:
    if year < 100:
        year += 2000
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def _date_from_filename(source_file: str | None) -> dt.date | None:
    if not source_file:
        return None
    match = _FILENAME_DATE_RE.search(source_file)
    if not match:
        return None
    return _date_from_parts(int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _cluster_rows(words: list[dict[str, Any]], y_tol: float = 12.0) -> list[list[dict[str, Any]]]:
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


def _sale_date_from_words(words: list[dict[str, Any]]) -> dt.date | None:
    for row in _cluster_rows(words):
        text = _row_text(row).lower()
        if "tax" not in text and "point" not in text:
            continue
        match = _SHORT_DATE_RE.search(_row_text(row))
        if match is None:
            continue
        return _date_from_parts(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return None


def _farm_from_words(words: list[dict[str, Any]]) -> str | None:
    """Vendor address on the left. The letterhead town Mold is not a farm."""
    left = [word for word in words if word["x"] < 620 and word["y"] < 340]
    text = " ".join(word["text"] for word in sorted(left, key=lambda item: (item["y"], item["x"])))
    text = text.upper()
    for marker, farm in _FARM_MARKERS:
        if marker in text:
            return farm
    match = _FARM_CODE_RE.search(text)
    if match:
        return match.group(1)
    return None


def _etag_from_token(token: str) -> str | None:
    raw = re.sub(r"[^A-Za-z0-9]", "", token).upper()
    if raw.startswith("UK"):
        body = raw[2:]
    elif raw.startswith("K") and len(raw) > 1:
        body = raw[1:]
    else:
        return None
    if not _TAG_BODY_RE.match(body):
        return None
    body = body.translate(_OCR_DIGIT)
    body = re.sub(r"\D", "", body)
    if len(body) != 12:
        return None
    return f"UK{body}"


def _row_etag(row: list[dict[str, Any]]) -> str | None:
    for word in sorted(row, key=lambda item: item["x"]):
        etag = _etag_from_token(word["text"])
        if etag:
            return etag
    return None


def _money_amount(text: str) -> float | None:
    cleaned = text.replace(",", "").replace(" ", "")
    match = None
    for match in _AMOUNT_RE.finditer(cleaned):
        pass
    if match is None:
        return None
    try:
        value = float(match.group(0))
    except ValueError:
        return None
    if value < 1 or value > 8000:
        return None
    return round(value, 2)


def _row_amount(row: list[dict[str, Any]]) -> float | None:
    """Rightmost money on the row. That is the AMOUNT column when PRICE is also money."""
    best: float | None = None
    best_x: float | None = None
    for word in row:
        value = _money_amount(word["text"])
        if value is None:
            continue
        if best_x is None or word["x"] >= best_x:
            best = value
            best_x = word["x"]
    return best


def _gross_amount(rows: list[list[dict[str, Any]]]) -> float | None:
    for row in rows:
        if "gross" not in _row_text(row).lower():
            continue
        amount = _row_amount(row)
        if amount is not None:
            return amount
    return None


def parse_mold_pdf(
    content: bytes,
    *,
    mailbox_farm: str | None = None,
    source_file: str | None = None,
) -> dict[str, Any]:
    warnings: list[str] = []
    images = extract_pdf_images(content)
    if not images:
        return {
            "farm": mailbox_farm,
            "sale_date": _date_from_filename(source_file),
            "lines": [],
            "warnings": ["PDF contained no page images to OCR"],
            "buyer": BUYER_MOLD,
        }

    sale_date: dt.date | None = None
    farm = mailbox_farm
    lines: list[dict[str, Any]] = []
    seen: set[str] = set()
    gross: float | None = None
    ocr_pages = 0
    for image in images:
        page_words = ocr_words_from_image(image, scale=2)
        if not page_words:
            continue
        ocr_pages += 1
        if farm is None:
            farm = _farm_from_words(page_words)
        if sale_date is None:
            sale_date = _sale_date_from_words(page_words)
        page_rows = _cluster_rows(page_words)
        if gross is None:
            gross = _gross_amount(page_rows)
        for row in page_rows:
            if _SKIP_ROW_RE.search(_row_text(row)):
                continue
            etag = _row_etag(row)
            amount = _row_amount(row)
            if not etag or amount is None or etag in seen:
                continue
            seen.add(etag)
            lines.append(
                {
                    "etag": etag,
                    "cold_weight_kg": 0.0,
                    "amount_gbp": amount,
                    "reject_kg": 0.0,
                    "kill_date": None,
                    "is_rejected": False,
                }
            )

    if sale_date is None:
        sale_date = _date_from_filename(source_file)
    for line in lines:
        line["kill_date"] = sale_date
    if ocr_pages == 0:
        warnings.append("PDF contained no extractable text (scanned remittance needs OCR)")
    if not lines:
        warnings.append("No sale lines extracted from PDF")
    if sale_date is None:
        warnings.append("Could not parse sale date")
    if farm is None:
        warnings.append("Could not determine farm from the vendor address")
    if gross is not None and lines:
        total = round(sum(line["amount_gbp"] for line in lines), 2)
        if abs(total - gross) > 0.05:
            warnings.append(
                f"Animal prices total {total:.2f} and the statement gross is {gross:.2f}"
            )

    return {
        "farm": farm,
        "sale_date": sale_date,
        "lines": lines,
        "warnings": warnings,
        "buyer": BUYER_MOLD,
    }
