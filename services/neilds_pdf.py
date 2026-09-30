"""Parse E. Nield & Partners seller remittance PDFs (T13 statements).

Digital PDFs are read as text. Scanned statements use Windows OCR when available.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from services.cattle_sale_pdf import extract_pdf_text_or_ocr, normalize_etag
from services.farms import HERD_FARM_OPTIONS

BUYER_NEILDS = "Neilds"

_TAG_RE = re.compile(
    r"(?:UK\s*[0-9A-Z]{10,15}|[A-Z]{2}\s*[0-9A-Z]{6,18})",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b")
_FILENAME_DATE_RE = re.compile(r"(?<!\d)(\d{8})(?!\d)")
_NUMBER_RE = re.compile(r"-?[\d,]+\.\d{1,2}|-?\d{2,4}")
_OCR_DIGIT = str.maketrans({
    "O": "0",
    "Q": "0",
    "D": "0",
    "I": "1",
    "L": "1",
    "Z": "2",
    "S": "5",
    "G": "6",
    "B": "8",
})
_FARM_MARKERS: tuple[tuple[str, str], ...] = (
    ("ASTON LOWER", "ALH"),
    ("ASTON JUXTA", "ALH"),
    ("BANK FARM", "BNK"),
    ("PARK HALL", "SFR"),
    ("THE PARKES", "PRK"),
    ("CHERRY ORCHARD", "COF"),
)


def looks_like_neilds_pdf(text: str, source_file: str | None = None) -> bool:
    haystack = f"{source_file or ''}\n{text}".lower()
    if "nield" in haystack or "enield" in haystack:
        return True
    name = (source_file or "").lower()
    if name.startswith("t13") or "t13-statement" in name:
        return True
    return "seller remittance" in haystack and "thomasson" in haystack


def repair_etag(value: str | None) -> str:
    """Fix OCR lookalikes in ear tags (S/5, O/0) then normalize."""
    raw = re.sub(r"\s+", "", (value or "").strip()).upper()
    if not raw:
        return ""
    if not re.match(r"^[A-Z]{2}", raw):
        return normalize_etag(raw)
    country, rest = raw[:2], raw[2:]
    if rest.isdigit():
        return normalize_etag(raw)
    repaired_rest = rest.translate(_OCR_DIGIT)
    repaired_rest = re.sub(r"\D", "", repaired_rest)
    return normalize_etag(f"{country}{repaired_rest}")


def _to_float(value: str | None) -> float | None:
    if not value:
        return None
    cleaned = re.sub(r"[^\d.\-]+", "", str(value).replace(",", ""))
    if not cleaned or cleaned in {".", "-", "-."}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_short_date(value: str) -> dt.date | None:
    raw = value.strip().replace("-", "/")
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _date_from_filename(source_file: str | None) -> dt.date | None:
    if not source_file:
        return None
    match = _FILENAME_DATE_RE.search(source_file.replace(" ", ""))
    if not match:
        return None
    raw = match.group(1)
    try:
        return dt.datetime.strptime(raw, "%d%m%Y").date()
    except ValueError:
        return None


def _farm_from_text(text: str, source_file: str | None) -> str | None:
    haystack = f"{source_file or ''}\n{text}".upper()
    for marker, farm in _FARM_MARKERS:
        if marker in haystack:
            return farm
    for farm in HERD_FARM_OPTIONS:
        if re.search(rf"\b{farm}\b", haystack):
            return farm
    return None


def _collection_date(text: str, source_file: str | None) -> dt.date | None:
    named = _date_from_filename(source_file)
    if named:
        return named
    dates = [_parse_short_date(match.group(1)) for match in _DATE_RE.finditer(text)]
    dates = [d for d in dates if d is not None]
    if not dates:
        return None
    # Prefer a date on/after 2020; DOB on calf lines is usually the earlier one.
    recent = [d for d in dates if d.year >= 2020]
    if not recent:
        return max(dates)
    return max(recent)


def _parse_weight_and_amount(text: str) -> tuple[float | None, float | None]:
    low = text.lower()
    weight = None
    amount = None

    wt_match = re.search(r"([\d,]+\.\d+)\s*(?:kg|wt)\b", low)
    if wt_match:
        weight = _to_float(wt_match.group(1))
    pph_match = re.search(r"([\d,]+\.\d+)\s*pph\b", low)
    if pph_match:
        amount = _to_float(pph_match.group(1))
    if amount is None:
        goods = re.search(r"\bgoods\b[^0-9]{0,20}([\d,]+\.\d{2})", low)
        if goods:
            amount = _to_float(goods.group(1))
    if amount is None:
        values = [_to_float(m.group(0)) for m in re.finditer(r"\b[\d,]+\.\d{2}\b", text)]
        values = [v for v in values if v is not None and 50 <= v <= 20000]
        # Skip obvious p/kg-in-pence figures like 350.00 when a larger GR value exists.
        large = [v for v in values if v >= 400]
        if large:
            amount = large[0]
        elif values:
            amount = values[-1]
    if weight is None:
        weights = [_to_float(m.group(0)) for m in re.finditer(r"\b\d{2,3}\.\d\b", text)]
        weights = [v for v in weights if v is not None and 40 <= v <= 900]
        if weights:
            weight = weights[0]
    return weight, amount


def parse_neilds_pdf(
    content: bytes,
    *,
    mailbox_farm: str | None = None,
    source_file: str | None = None,
) -> dict[str, Any]:
    warnings: list[str] = []
    text = extract_pdf_text_or_ocr(content)
    if not text.strip():
        return {
            "farm": mailbox_farm,
            "sale_date": _date_from_filename(source_file),
            "lines": [],
            "warnings": ["PDF contained no extractable text (scanned remittance needs OCR)"],
            "buyer": BUYER_NEILDS,
        }

    if not looks_like_neilds_pdf(text, source_file):
        warnings.append("PDF does not look like a Neilds remittance")

    sale_date = _collection_date(text, source_file)
    farm = mailbox_farm or _farm_from_text(text, source_file)
    weight, amount = _parse_weight_and_amount(text)

    lines: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in _TAG_RE.finditer(text):
        etag = repair_etag(match.group(0))
        if not etag or etag in seen:
            continue
        if not re.fullmatch(r"[A-Z]{2}\d{8,}", etag):
            continue
        seen.add(etag)
        line_weight = weight
        line_amount = amount
        after = text[match.end() : match.end() + 80]
        local_nums = [_to_float(n) for n in _NUMBER_RE.findall(after)]
        local_nums = [n for n in local_nums if n is not None]
        for num in local_nums:
            if line_weight is None and 40 <= num <= 900:
                line_weight = num
            elif line_amount is None and num >= 80:
                line_amount = num
        if line_weight is None:
            line_weight = 0.0
        if line_amount is None:
            warnings.append(f"No amount found for {etag}")
            continue
        lines.append(
            {
                "etag": etag,
                "cold_weight_kg": round(float(line_weight), 2),
                "amount_gbp": round(float(line_amount), 2),
                "reject_kg": 0.0,
                "kill_date": sale_date,
                "is_rejected": False,
            }
        )

    if not lines:
        warnings.append("No sale lines extracted from PDF")
    if sale_date is None:
        warnings.append("Could not parse collection date from PDF")

    return {
        "farm": farm,
        "sale_date": sale_date,
        "lines": lines,
        "warnings": warnings,
        "buyer": BUYER_NEILDS,
    }
