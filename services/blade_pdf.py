"""Parse ABP Gamechanger (Blade) calf Payment Advice PDFs.

These are digital remittances. Farm is left unset so import can resolve it
from the DairyComp ear-tag event — the letterhead is the office address.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from services.cattle_sale_pdf import extract_pdf_text, normalize_etag

BUYER_GAMECHANGER = "Gamechanger"

_TAG_RE = re.compile(r"UK\s*\d{10,15}", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})(?:T\d{2}:\d{2}:\d{2})?\b")
_FILENAME_DATE_RE = re.compile(r"PaymentAdvice[_\-]?(\d{8})", re.IGNORECASE)
_NUMBER_RE = re.compile(r"-?[\d,]+\.\d{1,2}|-?\d+")


def looks_like_blade_pdf(text: str, source_file: str | None = None) -> bool:
    name = (source_file or "").lower()
    if name.startswith("paymentadvice"):
        return True
    haystack = f"{source_file or ''}\n{text}".lower()
    if "payment advice" not in haystack and "paymentadvice" not in haystack:
        return False
    return any(
        marker in haystack
        for marker in ("gamechanger", "game changer", "abp2757", "abp uk")
    )


def looks_like_blade_pdf_bytes(content: bytes, source_file: str | None = None) -> bool:
    if source_file and looks_like_blade_pdf("", source_file):
        return True
    try:
        text = extract_pdf_text(content)
    except Exception:
        return False
    return looks_like_blade_pdf(text, source_file)


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


def _date_from_filename(source_file: str | None) -> dt.date | None:
    if not source_file:
        return None
    match = _FILENAME_DATE_RE.search(source_file)
    if not match:
        return None
    raw = match.group(1)
    try:
        return dt.date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    except ValueError:
        return None


def _date_from_text(text: str) -> dt.date | None:
    match = _ISO_DATE_RE.search(text)
    if not match:
        return None
    try:
        return dt.date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _parse_animal_row(line: str) -> dict[str, Any] | None:
    tag_match = _TAG_RE.search(line)
    if tag_match is None:
        return None
    etag = normalize_etag(tag_match.group(0))
    if not etag:
        return None

    numbers = [_to_float(token) for token in _NUMBER_RE.findall(line[tag_match.end() :])]
    numbers = [n for n in numbers if n is not None]
    if not numbers:
        return None

    amount = numbers[-1]
    if amount < 20 or amount > 2500:
        return None

    weight = 0.0
    for value in numbers[:-1]:
        if 20 <= value <= 150:
            weight = value
            break

    return {
        "etag": etag,
        "cold_weight_kg": round(weight, 2),
        "amount_gbp": round(amount, 2),
        "reject_kg": 0.0,
        "is_rejected": False,
    }


def parse_blade_pdf(
    content: bytes,
    *,
    mailbox_farm: str | None = None,
    source_file: str | None = None,
) -> dict[str, Any]:
    del mailbox_farm  # Letterhead is the office address, not the herd farm.
    warnings: list[str] = []
    text = extract_pdf_text(content)
    if not text.strip():
        return {
            "farm": None,
            "sale_date": _date_from_filename(source_file),
            "lines": [],
            "warnings": ["PDF contained no extractable text"],
            "buyer": BUYER_GAMECHANGER,
        }

    if not looks_like_blade_pdf(text, source_file):
        warnings.append("PDF does not look like a Gamechanger payment advice")

    sale_date = _date_from_text(text) or _date_from_filename(source_file)
    lines: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        parsed = _parse_animal_row(raw_line)
        if parsed is None:
            continue
        etag = parsed["etag"]
        if etag in seen:
            continue
        seen.add(etag)
        parsed["kill_date"] = sale_date
        lines.append(parsed)

    if not lines:
        warnings.append("No sale lines extracted from PDF")
    if sale_date is None:
        warnings.append("Could not parse sale date from payment advice")

    return {
        "farm": None,
        "sale_date": sale_date,
        "lines": lines,
        "warnings": warnings,
        "buyer": BUYER_GAMECHANGER,
    }
