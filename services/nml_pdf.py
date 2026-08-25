"""Parse NML (National Milk Laboratories) milk-quality report PDFs.

NML emails the same data in farm-specific templates. The five Thomasson farms
currently use three layouts:

* Ongoing Test Report — ALH, COF, SFR
  Sample ID, Date, optional Vat Literage flag, Butterfat, Protein, SCC,
  BactoScan, Urea, Water/FPD, Antibiotic.
* Additional Test Results Notification — BNK
  Sample No, Date (dd/mm/yy), Vat order (1st/2nd/3rd), B/Fat, Protein, FPD,
  Cell Count, Urea, B'Scan, Antibiotic. Producer is labelled "Producer Number".
* Additional Test Notification — PRK (Heler)
  Sample ID, Date, B/Fat, Protein, SCC, BactoScan, FPD, A/B, Urea.

Reports often continue onto a second page without repeating the header.
Antibiotic is assumed Pass unless the row says Fail.
"""

from __future__ import annotations

import datetime as dt
import io
import re
from typing import Any

import pdfplumber

_MONTHS = (
    "January February March April May June July August September "
    "October November December"
).split()

_MONTH_RE = re.compile(r"^(?:%s)\s+\d{4}$" % "|".join(_MONTHS), re.I)
_PRODUCER_RE = re.compile(
    r"Producer\s+(?:Reference|Number)\s*:?\s*([0-9A-Za-z]+)",
    re.I,
)
_REPORT_DATE_RE = re.compile(
    r"(?:Report\s+Date|Produced\s+on)\s*:?\s*(\d{2}/\d{2}/\d{2,4})",
    re.I,
)
_PRODUCTION_UNIT_RE = re.compile(r"Production Unit\s+(.+?)\s*$", re.I)
_SAMPLE_ID_RE = re.compile(r"^\d{1,6}$")
_DATE_TOKEN_RE = re.compile(r"^\d{2}/\d{2}/\d{2,4}$")
_VAT_ORDER_RE = re.compile(r"^(?:1st|2nd|3rd|4th|5th)$", re.I)
_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)?$")
_SKIP_ROW_RE = re.compile(
    r"page\s+\d+|registered office|contact nml|if your sample|"
    r"buyer messages|indicates the tests|ongoing test|additional test|"
    r"producer (?:reference|number)|sample\s+sample|butterfat|b/fat",
    re.I,
)

LAYOUT_ONGOING = "ongoing"
LAYOUT_BNK = "bnk"
LAYOUT_PRK = "prk"

# x0 bands copied from the live NML templates (same on continuation pages).
_BANDS: dict[str, tuple[tuple[str, float, float], ...]] = {
    LAYOUT_ONGOING: (
        ("sample_id", 0, 55),
        ("sample_date", 55, 118),
        ("vat", 118, 165),
        ("butterfat_pct", 165, 218),
        ("protein_pct", 218, 272),
        ("scc", 272, 328),
        ("bactoscan", 328, 378),
        ("urea_pct", 378, 422),
        ("fpd", 422, 472),
        ("antibiotic", 472, 600),
    ),
    LAYOUT_BNK: (
        ("sample_id", 0, 52),
        ("sample_date", 52, 112),
        ("vat", 112, 168),
        ("butterfat_pct", 168, 220),
        ("protein_pct", 220, 280),
        ("fpd", 280, 332),
        ("scc", 332, 392),
        ("urea_pct", 392, 438),
        ("bactoscan", 438, 478),
        ("antibiotic", 478, 600),
    ),
    LAYOUT_PRK: (
        ("sample_id", 0, 50),
        ("sample_date", 50, 115),
        ("butterfat_pct", 115, 158),
        ("protein_pct", 158, 212),
        ("scc", 212, 258),
        ("bactoscan", 258, 322),
        ("fpd", 322, 372),
        ("antibiotic", 372, 422),
        ("urea_pct", 422, 520),
    ),
}

# Producer reference -> internal farm code.
PRODUCER_REF_FARM: dict[str, str] = {
    "641565": "ALH",
    "618538": "BNK",
    "527634": "SFR",
    "231000002": "PRK",
    "930221": "COF",
}
FARM_PRODUCER_REF: dict[str, str] = {farm: ref for ref, farm in PRODUCER_REF_FARM.items()}


def farm_for_producer_ref(producer_ref: str | None) -> str | None:
    if not producer_ref:
        return None
    return PRODUCER_REF_FARM.get(producer_ref.strip())


def normalize_sample_id(sample_id: str | None) -> str:
    """Strip leading zeros so PDF '001' matches EOM sample '1'."""
    text = str(sample_id or "").strip()
    if not text:
        return ""
    if text.isdigit():
        return str(int(text))
    return text


def _to_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    text = value.strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _clean_number(token: str) -> str | None:
    cleaned = re.sub(r"[^0-9.]", "", token)
    if cleaned and _NUMBER_RE.match(cleaned):
        return cleaned
    return None


def _detect_layout(full_text: str) -> str:
    lowered = full_text.lower()
    if "additional test results notification" in lowered:
        return LAYOUT_BNK
    if "additional test notification" in lowered:
        return LAYOUT_PRK
    return LAYOUT_ONGOING


def _parse_metadata(lines: list[str], full_text: str) -> dict[str, Any]:
    producer_ref = None
    if match := _PRODUCER_RE.search(full_text):
        producer_ref = match.group(1).strip()

    report_date = None
    if match := _REPORT_DATE_RE.search(full_text):
        report_date = _to_date(match.group(1))

    production_unit = None
    for line in lines:
        if match := _PRODUCTION_UNIT_RE.search(line):
            production_unit = match.group(1).strip()
            break

    notification_type = None
    notif_idx = None
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.lower().endswith("notification") or stripped.lower() == "ongoing test report":
            notification_type = stripped
            notif_idx = idx
            break

    report_month = None
    month_idx = None
    for idx, line in enumerate(lines):
        if _MONTH_RE.match(line.strip()):
            report_month = line.strip()
            month_idx = idx
            break

    milk_buyer = None
    if notif_idx is not None and month_idx is not None:
        start, end = sorted((notif_idx, month_idx))
        for line in lines[start + 1 : end]:
            if line.strip():
                milk_buyer = line.strip()
                break

    return {
        "producer_ref": producer_ref,
        "farm": farm_for_producer_ref(producer_ref),
        "milk_buyer": milk_buyer,
        "notification_type": notification_type,
        "report_month": report_month,
        "report_date": report_date,
        "production_unit": production_unit,
        "layout": _detect_layout(full_text),
    }


def _parse_antibiotic(tokens: list[str]) -> bool:
    """Assume Pass unless the row explicitly says Fail."""
    for token in tokens:
        low = re.sub(r"[^a-z]", "", token.lower())
        if low == "fail":
            return False
        if low == "pass":
            return True
    return True


def _to_float(token: str | None) -> float | None:
    if not token:
        return None
    number = _clean_number(token)
    if number is None:
        return None
    try:
        return float(number)
    except ValueError:
        return None


def _to_int(token: str | None) -> int | None:
    value = _to_float(token)
    if value is None:
        return None
    return int(round(value))


def _fix_urea_fpd(row: dict[str, Any]) -> None:
    """Urea is a small decimal (e.g. 0.021); FPD is typically 400–600."""
    urea = row.get("urea_pct")
    fpd = row.get("fpd")
    urea_looks_fpd = urea is not None and abs(urea) >= 1
    fpd_looks_urea = fpd is not None and 0 < abs(fpd) < 1
    if urea_looks_fpd and (fpd is None or fpd_looks_urea):
        row["fpd"] = int(round(urea)) if urea is not None else fpd
        row["urea_pct"] = fpd if fpd_looks_urea else None


def _row_from_fields(fields: dict[str, str]) -> dict[str, Any] | None:
    sample_id = normalize_sample_id(fields.get("sample_id"))
    sample_date = _to_date(fields.get("sample_date"))
    if not sample_id or sample_date is None:
        return None
    fat = _to_float(fields.get("butterfat_pct"))
    protein = _to_float(fields.get("protein_pct"))
    if fat is None or protein is None:
        return None
    if fat <= 0 or fat > 15 or protein <= 0 or protein > 10:
        return None
    row = {
        "sample_id": sample_id,
        "sample_date": sample_date,
        "butterfat_pct": fat,
        "protein_pct": protein,
        "scc": _to_int(fields.get("scc")),
        "bactoscan": _to_int(fields.get("bactoscan")),
        "fpd": _to_int(fields.get("fpd")),
        "urea_pct": _to_float(fields.get("urea_pct")),
        "antibiotic_pass": _parse_antibiotic(
            [fields.get("antibiotic") or "", fields.get("vat") or ""]
        ),
    }
    _fix_urea_fpd(row)
    return row


def _cluster_word_rows(
    words: list[dict[str, Any]], *, y_tol: float = 7.0
) -> list[list[dict[str, Any]]]:
    if not words:
        return []
    ordered = sorted(words, key=lambda item: (item["top"], item["x0"]))
    rows: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = [ordered[0]]
    current_y = ordered[0]["top"]
    for word in ordered[1:]:
        if abs(word["top"] - current_y) <= y_tol:
            current.append(word)
            continue
        rows.append(sorted(current, key=lambda item: item["x0"]))
        current = [word]
        current_y = word["top"]
    rows.append(sorted(current, key=lambda item: item["x0"]))
    return rows


def _assign_band(x0: float, bands: tuple[tuple[str, float, float], ...]) -> str | None:
    for name, start, end in bands:
        if start <= x0 < end:
            return name
    return None


def _parse_word_rows(
    pages_words: list[list[dict[str, Any]]], layout: str
) -> list[dict[str, Any]]:
    bands = _BANDS[layout]
    samples: list[dict[str, Any]] = []
    seen: set[tuple[str, dt.date]] = set()
    for words in pages_words:
        for row_words in _cluster_word_rows(words):
            texts = [w["text"] for w in row_words]
            joined = " ".join(texts)
            if _SKIP_ROW_RE.search(joined):
                continue
            if not texts or not _SAMPLE_ID_RE.match(texts[0]):
                continue
            fields: dict[str, str] = {}
            for word in row_words:
                band = _assign_band(word["x0"], bands)
                if not band or band == "vat":
                    continue
                token = word["text"].strip()
                if band in ("sample_id", "sample_date"):
                    fields[band] = token
                    continue
                if band == "antibiotic":
                    fields["antibiotic"] = (fields.get("antibiotic") or "") + " " + token
                    continue
                if _clean_number(token) is None:
                    continue
                fields[band] = token
            parsed = _row_from_fields(fields)
            if parsed is None:
                continue
            key = (parsed["sample_id"], parsed["sample_date"])
            if key in seen:
                continue
            seen.add(key)
            samples.append(parsed)
    return samples


def _parse_line_row(line: str, layout: str) -> dict[str, Any] | None:
    """Fallback when a continuation page has no usable word coordinates."""
    stripped = line.strip()
    if not stripped or _SKIP_ROW_RE.search(stripped):
        return None
    parts = stripped.split()
    if len(parts) < 4 or not _SAMPLE_ID_RE.match(parts[0]) or not _DATE_TOKEN_RE.match(parts[1]):
        return None
    rest = parts[2:]
    if rest and (_VAT_ORDER_RE.match(rest[0]) or rest[0] == "1"):
        rest = rest[1:]
    antibiotic = _parse_antibiotic(rest)
    numbers: list[str] = []
    for token in rest:
        if token.lower() in ("pass", "fail", "‡", "n", "y"):
            continue
        number = _clean_number(token)
        if number is not None:
            numbers.append(number)
    if layout == LAYOUT_BNK:
        names = ("butterfat_pct", "protein_pct", "fpd", "scc", "urea_pct", "bactoscan")
    elif layout == LAYOUT_PRK:
        names = ("butterfat_pct", "protein_pct", "scc", "bactoscan", "fpd", "urea_pct")
    else:
        names = ("butterfat_pct", "protein_pct", "scc", "bactoscan", "urea_pct", "fpd")
    fields = {
        "sample_id": parts[0],
        "sample_date": parts[1],
        "antibiotic": "Fail" if not antibiotic else "Pass",
    }
    for name, number in zip(names, numbers):
        fields[name] = number
    return _row_from_fields(fields)


def parse_nml_pdf(content: bytes) -> dict[str, Any]:
    """Parse a single NML report PDF into metadata + per-sample result rows."""
    all_lines: list[str] = []
    pages_words: list[list[dict[str, Any]]] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            all_lines.extend(text.splitlines())
            pages_words.append(
                page.extract_words(x_tolerance=2, y_tolerance=3, keep_blank_chars=False)
                or []
            )

    full_text = "\n".join(all_lines)
    metadata = _parse_metadata(all_lines, full_text)
    layout = metadata["layout"]

    samples = _parse_word_rows(pages_words, layout)
    if not samples:
        seen: set[tuple[str, dt.date]] = set()
        for line in all_lines:
            row = _parse_line_row(line, layout)
            if row is None:
                continue
            key = (row["sample_id"], row["sample_date"])
            if key in seen:
                continue
            seen.add(key)
            samples.append(row)

    return {"metadata": metadata, "samples": samples}


def looks_like_nml_pdf(content: bytes) -> bool:
    try:
        result = parse_nml_pdf(content)
    except Exception:
        return False
    metadata = result.get("metadata") or {}
    return bool(metadata.get("producer_ref") and result.get("samples"))
