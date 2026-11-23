"""Read an Advanced Proteins Brassington invoice.

The quantity column is one per collection ticket. The animal count is in the
description, for example "3 X BOVINE 0-1 MONTH" or "BOVINE OVER 48 MONTHS".
The date on the line is the collection date, so one invoice can fill two months.
"""

from __future__ import annotations

import datetime as dt
import io
import re
from typing import Any

import pdfplumber

_FARM_NAMES: tuple[tuple[str, str], ...] = (
    ("CHERRY ORCHARD", "COF"),
    ("PARK HALL", "SFR"),
    ("ASTON LOWER", "ALH"),
    ("BANK FARM", "BNK"),
    ("THE PARKES", "PRK"),
    ("PARKES", "PRK"),
)
_BANDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"OVER\s+48"), "over-48"),
    (re.compile(r"24\s*[-–]\s*48"), "24-48"),
    (re.compile(r"6\s*[-–]\s*24"), "6-24"),
    (re.compile(r"4\s*[-–]\s*6"), "4-6"),
    (re.compile(r"1\s*[-–]\s*4"), "1-4"),
    (re.compile(r"0\s*[-–]\s*1"), "0-1"),
)
_FARM_RE = re.compile(r"Thomasson\s*\(([^)]+)\)", re.IGNORECASE)
_INVOICE_NO_RE = re.compile(r"Invoice\s+No\s+(\d+)", re.IGNORECASE)
_DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{2})\s*-")
_ANIMAL_RE = re.compile(
    r"(?:(\d+)\s*X\s+)?BOVINE\s+((?:OVER\s+\d+|\d+\s*[-–]\s*\d+)\s+MONTHS?)",
    re.IGNORECASE,
)


def _pdf_text(content: bytes) -> str:
    pages: list[str] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    return " ".join(" ".join(pages).split())


def _farm_code(name: str) -> str | None:
    folded = re.sub(r"\s+", " ", name).strip().upper()
    for label, code in _FARM_NAMES:
        if label in folded:
            return code
    return None


def _band_id(phrase: str) -> str | None:
    for pattern, band in _BANDS:
        if pattern.search(phrase):
            return band
    return None


def _collection_date(day: str, month: str, year: str) -> dt.date:
    return dt.date(2000 + int(year), int(month), int(day))


def parse_brassington_invoice(content: bytes) -> dict[str, Any]:
    """Return the farm and the animal counts by collection month and age band."""
    text = _pdf_text(content)
    farm_match = _FARM_RE.search(text)
    if farm_match is None:
        raise ValueError("This PDF is not a Brassington invoice for one of the farms.")
    farm = _farm_code(farm_match.group(1))
    if farm is None:
        raise ValueError(f"Unrecognised farm on the invoice: {farm_match.group(1).strip()}.")
    invoice_no = ""
    number_match = _INVOICE_NO_RE.search(text)
    if number_match:
        invoice_no = number_match.group(1)

    dates = list(_DATE_RE.finditer(text))
    if not dates:
        raise ValueError("No collection dates were found on the invoice.")

    totals: dict[tuple[dt.date, str], int] = {}
    unknown: list[str] = []
    for index, found in enumerate(dates):
        end = dates[index + 1].start() if index + 1 < len(dates) else len(text)
        chunk = text[found.end() : end]
        if "TSE" in chunk.upper() and "BOVINE" not in chunk.upper():
            continue
        collected = _collection_date(*found.groups())
        month = collected.replace(day=1)
        animals = list(_ANIMAL_RE.finditer(chunk))
        if not animals:
            continue
        for animal in animals:
            count = int(animal.group(1) or "1")
            band = _band_id(animal.group(2))
            if band is None:
                unknown.append(animal.group(2).strip())
                continue
            totals[(month, band)] = totals.get((month, band), 0) + count

    if not totals:
        raise ValueError("No bovine collections were found on the invoice.")
    return {
        "farm": farm,
        "farm_name": farm_match.group(1).strip(),
        "invoice_no": invoice_no,
        "totals": [
            {"month": month.isoformat()[:7], "band": band, "quantity": quantity}
            for (month, band), quantity in sorted(totals.items())
        ],
        "unknown_bands": unknown,
    }
