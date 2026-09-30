"""Parse Pickstock Telford FPF remittance PDFs.

Each remittance lists sold animals with ear tag, cold weight (kg), kill date,
and amount (£). Farm is inferred from the vendor / delivery name.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from services.cattle_sale_pdf import extract_pdf_text, is_rejected_sale, normalize_etag
from services.farms import HERD_FARM_OPTIONS

_TAG_RE = re.compile(
    r"(?:UK\s*\d{10,15}|[A-Z]{2}\s*\d{6,18})",
    re.IGNORECASE,
)
_KILL_DATE_RE = re.compile(
    r"Kill\s+Date:\s*([A-Za-z]{3,9}\s+\d{1,2}\s+\d{4})",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"-?[\d,]+(?:\.\d+)?")
_FARM_MARKERS: tuple[tuple[str, str], ...] = (
    ("ASTON LOWER HALL", "ALH"),
    ("BANK FARM", "BNK"),
    ("PARK HALL", "SFR"),
    ("THE PARKES", "PRK"),
    ("CHERRY ORCHARD", "COF"),
)


def looks_like_pickstock_pdf(text: str, source_file: str | None = None) -> bool:
    low = (text or "").lower()
    name = (source_file or "").lower()
    if "pickstock" in low or "pickstock" in name:
        return True
    if name.startswith("fpf_") or " fpf" in name:
        return True
    return "vendor code:" in low and "kill date:" in low and "eartag" in low


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


def _parse_kill_date(text: str) -> dt.date | None:
    match = _KILL_DATE_RE.search(text)
    if not match:
        return None
    raw = re.sub(r"\s+", " ", match.group(1).strip())
    for fmt in ("%b %d %Y", "%B %d %Y"):
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
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


def _parse_animal_row(line: str) -> dict[str, Any] | None:
    tag_match = _TAG_RE.search(line)
    if tag_match is None:
        return None
    etag = normalize_etag(tag_match.group(0))
    if not etag:
        return None

    after = line[tag_match.end() :]
    numbers = [_to_float(token) for token in _NUMBER_RE.findall(after)]
    numbers = [n for n in numbers if n is not None]
    if len(numbers) < 2:
        return None

    amount = numbers[-1]
    # Typical Pickstock tail: Hot, Rebate, Cold, Cond, p/Kg, Value
    if len(numbers) >= 6:
        cold_weight = numbers[-4]
        reject_kg = 0.0
    elif len(numbers) >= 4:
        cold_weight = numbers[-3]
        reject_kg = 0.0
    else:
        cold_weight = numbers[-2]
        reject_kg = 0.0

    if cold_weight is None or amount is None:
        return None
    if cold_weight < 20 or cold_weight > 900:
        return None

    rejected = is_rejected_sale(cold_weight, reject_kg, amount) or (
        abs(amount) <= 0.005 and "condemn" in line.lower()
    )
    return {
        "etag": etag,
        "cold_weight_kg": round(cold_weight, 2),
        "amount_gbp": round(amount, 2),
        "reject_kg": round(reject_kg, 2) if reject_kg is not None else None,
        "is_rejected": rejected,
    }


def parse_pickstock_pdf(
    content: bytes,
    *,
    mailbox_farm: str | None = None,
    source_file: str | None = None,
) -> dict[str, Any]:
    warnings: list[str] = []
    text = extract_pdf_text(content)
    if not text.strip():
        return {
            "farm": mailbox_farm,
            "sale_date": None,
            "lines": [],
            "warnings": ["PDF contained no extractable text"],
            "buyer": "Pickstock",
        }

    if not looks_like_pickstock_pdf(text, source_file):
        warnings.append("PDF does not look like a Pickstock remittance")

    sale_date = _parse_kill_date(text)
    farm = mailbox_farm or _farm_from_text(text, source_file)

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
        warnings.append("Could not parse kill date from PDF")

    return {
        "farm": farm,
        "sale_date": sale_date,
        "lines": lines,
        "warnings": warnings,
        "buyer": "Pickstock",
    }
