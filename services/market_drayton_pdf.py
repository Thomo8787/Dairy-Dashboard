"""Parse Market Drayton (Barbers) sellers-list remittances.

These PDFs are usually scanned. Windows OCR word boxes pair ear tags with amounts.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from services.cattle_sale_pdf import extract_pdf_images, ocr_words_from_image
from services.farms import HERD_FARM_OPTIONS
from services.neilds_pdf import repair_etag

BUYER_MARKET_DRAYTON = "Market Drayton"

_TAG_RE = re.compile(r"UK[I1L]?\s*[0-9A-Z]{8,16}", re.IGNORECASE)
_FILENAME_DATE_RE = re.compile(
    r"(?<!\d)(\d{1,2})[-./](\d{1,2})[-./](\d{2,4})(?!\d)"
)
_AMOUNT_TOKEN_RE = re.compile(r"^[\d,]+\.?\d{0,2}$")


def looks_like_market_drayton_pdf(text: str, source_file: str | None = None) -> bool:
    haystack = f"{source_file or ''}\n{text}".lower()
    if "market drayton" in haystack or "barbers-auction" in haystack:
        return True
    name = (source_file or "").lower()
    if name.startswith("t304") or "sellers list" in name:
        return True
    return False


def _date_from_filename(source_file: str | None) -> dt.date | None:
    if not source_file:
        return None
    match = _FILENAME_DATE_RE.search(source_file)
    if not match:
        return None
    day, month, year = match.group(1), match.group(2), match.group(3)
    if len(year) == 2:
        year = f"20{year}"
    try:
        return dt.date(int(year), int(month), int(day))
    except ValueError:
        return None


def _farm_from_words(words: list[dict[str, Any]], source_file: str | None) -> str | None:
    text = " ".join(word["text"] for word in words).upper()
    haystack = f"{source_file or ''}\n{text}"
    markers = (
        ("ASTON LO", "ALH"),
        ("ASTON JUXTA", "ALH"),
        ("BANK FARM", "BNK"),
        ("PARK HALL", "SFR"),
        ("THE PARKES", "PRK"),
        ("CHERRY ORCHARD", "COF"),
        ("THOMASSON LIVESTOCK", "ALH"),
    )
    for marker, farm in markers:
        if marker in haystack:
            return farm
    for farm in HERD_FARM_OPTIONS:
        if re.search(rf"\b{farm}\b", haystack):
            return farm
    return None


def _cluster_rows(words: list[dict[str, Any]], y_tol: float = 16.0) -> list[list[dict[str, Any]]]:
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


def _row_tag(row: list[dict[str, Any]]) -> str | None:
    joined = " ".join(word["text"] for word in sorted(row, key=lambda item: item["x"]))
    match = _TAG_RE.search(joined.replace(" ", ""))
    if match is None:
        match = _TAG_RE.search(joined)
    if match is None:
        return None
    etag = repair_etag(match.group(0))
    if not etag or not etag.startswith("UK"):
        return None
    digits = etag[2:]
    if len(digits) >= 12:
        digits = digits[:12]
    if len(digits) < 10 or not digits.isdigit():
        return None
    return f"UK{digits}"


def _parse_amount_token(text: str) -> float | None:
    raw = text.replace(",", "").replace(" ", "")
    if raw.isdigit() and 2 <= len(raw) <= 4:
        raw = f"{raw}.00"
    elif re.fullmatch(r"\d{2,5}00", raw) and "." not in raw and int(raw) >= 10000:
        raw = f"{raw[:-2]}.{raw[-2:]}"
    try:
        value = float(raw)
    except ValueError:
        return None
    if value < 20 or value > 2500:
        return None
    return round(value, 2)


def _row_amount(row: list[dict[str, Any]]) -> float | None:
    # Rightmost money column — avoid concatenating nearby footer totals.
    values: list[tuple[float, float]] = []
    for word in row:
        if word["x"] < 1300:
            continue
        if not _AMOUNT_TOKEN_RE.match(word["text"].replace(",", "")):
            continue
        value = _parse_amount_token(word["text"])
        if value is not None:
            values.append((word["x"], value))
    if not values:
        return None
    values.sort(key=lambda item: item[0])
    return values[-1][1]


def _row_weight(row: list[dict[str, Any]]) -> float | None:
    candidates: list[float] = []
    for word in row:
        if word["x"] < 900 or word["x"] >= 1300:
            continue
        raw = word["text"].replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        if 80 <= value <= 900:
            candidates.append(value)
    if not candidates:
        return None
    return round(candidates[0], 2)


def parse_market_drayton_pdf(
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
            "buyer": BUYER_MARKET_DRAYTON,
        }

    sale_date = _date_from_filename(source_file)
    farm = mailbox_farm
    lines: list[dict[str, Any]] = []
    seen: set[str] = set()
    ocr_pages = 0
    for image in images:
        page_words = ocr_words_from_image(image, scale=2)
        if not page_words:
            continue
        ocr_pages += 1
        if farm is None:
            farm = _farm_from_words(page_words, source_file)
        for row in _cluster_rows(page_words):
            etag = _row_tag(row)
            amount = _row_amount(row)
            if not etag or amount is None:
                continue
            if etag in seen:
                continue
            seen.add(etag)
            weight = _row_weight(row)
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

    if ocr_pages == 0:
        warnings.append("PDF contained no extractable text (scanned remittance needs OCR)")
    if farm is None:
        farm = _farm_from_words([], source_file)
    if not lines:
        warnings.append("No sale lines extracted from PDF")
    if sale_date is None:
        warnings.append("Could not parse sale date from filename")

    return {
        "farm": farm,
        "sale_date": sale_date,
        "lines": lines,
        "warnings": warnings,
        "buyer": BUYER_MARKET_DRAYTON,
    }
