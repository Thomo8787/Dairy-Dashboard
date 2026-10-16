"""Parse ABP Gamechanger (Blade) calf Payment Advice PDFs.

Digital remittances are read from the PDF text. A scan with no copyable text
is read from its page images, one image at a time. Farm is left unset so
import can resolve it from the DairyComp ear-tag event — the letterhead is
the office address.
"""

from __future__ import annotations

import datetime as dt
import gc
import re
from typing import Any

from services.cattle_sale_pdf import extract_pdf_text, normalize_etag, ocr_words_from_image

BUYER_GAMECHANGER = "Gamechanger"

_TAG_RE = re.compile(r"UK\s*\d{10,15}", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})(?:T\d{2}:\d{2}:\d{2})?\b")
_UK_DATE_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b")
_FILENAME_DATE_RE = re.compile(r"PaymentAdvice[_\-]?(\d{8})", re.IGNORECASE)
_NUMBER_RE = re.compile(r"-?[\d,]+\.\d{1,2}|-?\d+")
_PENCE_TOKEN_RE = re.compile(r"-?[\d,]+\.\d{2}")
_MIN_PRICE_GBP = 20.0
_MAX_PRICE_GBP = 2500.0


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
    if match:
        try:
            return dt.date.fromisoformat(match.group(1))
        except ValueError:
            pass
    for day, month, year in _UK_DATE_RE.findall(text):
        try:
            return dt.date(int(year), int(month), int(day))
        except ValueError:
            continue
    return None


def _cluster_rows(words: list[dict[str, Any]], y_tol: float = 14.0) -> list[list[dict[str, Any]]]:
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


_OCR_TAG_RE = re.compile(r"UK[\sI1]*\d(?:[\d\sOILSBZ]{8,24})", re.IGNORECASE)
_OCR_DIGIT = str.maketrans({"O": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2"})


def _jpeg_bytes(image: Any, *, max_edge: int = 1600) -> bytes:
    import io

    image.thumbnail((max_edge, max_edge))
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=75)
    return buffer.getvalue()


def _image_bytes_for_ocr(image: dict[str, Any]) -> bytes | None:
    """Return JPEG or PNG bytes for one embedded page image.

    Gamechanger scans store the page as a fax bitmap. Sheet pages store the
    table as a large colour picture. Both are turned into a picture file the
    text reader can open, and a blank picture is left out.
    """
    stream = image.get("stream")
    if stream is None:
        return None
    try:
        data = stream.get_data()
    except Exception:
        return None
    if not data:
        return None
    if data[:2] == b"\xff\xd8" or data[:8] == b"\x89PNG\r\n\x1a\n":
        if len(data) > 4 * 1024 * 1024:
            return None
        return data
    src = image.get("srcsize") or ()
    if len(src) != 2:
        return None
    width, height = int(src[0]), int(src[1])
    if width < 1 or height < 1:
        return None
    import io

    from PIL import Image

    if image.get("bits") == 1:
        row_bytes = (width + 7) // 8
        if len(data) != row_bytes * height or len(data) > 4 * 1024 * 1024:
            return None
        bitmap = Image.frombytes("1", (width, height), data)
        buffer = io.BytesIO()
        bitmap.save(buffer, format="PNG")
        return buffer.getvalue()
    if image.get("bits") == 8 and len(data) == width * height * 3:
        picture = Image.frombytes("RGB", (width, height), data)
        del data
        if picture.convert("L").getextrema() == (255, 255):
            return None
        return _jpeg_bytes(picture)
    return None


def _is_pence_token(token: str) -> bool:
    return _PENCE_TOKEN_RE.fullmatch(token) is not None


def _price_in_range(value: float) -> bool:
    return _MIN_PRICE_GBP <= value <= _MAX_PRICE_GBP


def _pence_prices(text: str) -> list[float]:
    prices: list[float] = []
    for token in _PENCE_TOKEN_RE.findall(text):
        value = _to_float(token)
        if value is not None and _price_in_range(value):
            prices.append(value)
    return prices


def _join_split_prices(raw_lines: list[str]) -> list[str]:
    """Attach a Price column that OCR placed on the following line."""
    joined: list[str] = []
    index = 0
    while index < len(raw_lines):
        line = raw_lines[index]
        if index + 1 < len(raw_lines):
            nxt = raw_lines[index + 1]
            if (
                _TAG_RE.search(line)
                and not _pence_prices(line)
                and _TAG_RE.search(nxt) is None
                and _pence_prices(nxt)
            ):
                joined.append(f"{line} {nxt}")
                index += 2
                continue
        joined.append(line)
        index += 1
    return joined


def _repair_ocr_line(line: str) -> str:
    """Turn an OCR ear tag into UK plus 12 digits, leaving the rest of the row."""

    def repl(match: re.Match[str]) -> str:
        raw = re.sub(r"[^A-Z0-9]", "", match.group(0).upper())
        if raw.startswith("UKI"):
            raw = "UK1" + raw[3:]
        digits = re.sub(r"\D", "", raw[2:].translate(_OCR_DIGIT))
        if len(digits) < 12:
            return match.group(0)
        return f"UK{digits[:12]}"

    return _OCR_TAG_RE.sub(repl, line)


def _lines_from_scan(content: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    """Read animal rows from page images when the PDF has no copyable text."""
    import io

    import pdfplumber

    lines: list[dict[str, Any]] = []
    seen: set[str] = set()
    raw_lines: list[str] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            bitmaps: list[bytes] = []
            pictures: list[bytes] = []
            for image in page.images or []:
                blob = _image_bytes_for_ocr(image)
                if blob is None:
                    continue
                if image.get("bits") == 1:
                    bitmaps.append(blob)
                else:
                    pictures.append(blob)
            chosen = bitmaps or pictures
            while chosen:
                image_bytes = chosen.pop()
                words = ocr_words_from_image(image_bytes, scale=2, prefer_rapid=True)
                del image_bytes
                if words:
                    page_lines: list[str] = []
                    for row in _cluster_rows(words):
                        tokens = [word["text"] for word in sorted(row, key=lambda item: item["x"])]
                        raw_line = _repair_ocr_line(" ".join(tokens))
                        page_lines.append(raw_line)
                        raw_lines.append(raw_line)
                    for raw_line in _join_split_prices(page_lines):
                        parsed = _parse_animal_row(raw_line)
                        if parsed is None or parsed["etag"] in seen:
                            continue
                        seen.add(parsed["etag"])
                        lines.append(parsed)
                del words
                gc.collect()
    return lines, raw_lines


def _parse_animal_row(line: str) -> dict[str, Any] | None:
    tag_match = _TAG_RE.search(line)
    if tag_match is None:
        return None
    etag = normalize_etag(tag_match.group(0))
    if not etag:
        return None

    numbers = [
        (value, _is_pence_token(token))
        for token in _NUMBER_RE.findall(line[tag_match.end() :])
        if (value := _to_float(token)) is not None
    ]
    if not numbers:
        return None

    amount_index: int | None = None
    for index, (value, is_pence) in enumerate(numbers):
        if is_pence and _price_in_range(value):
            amount_index = index
    if amount_index is None:
        for index, (value, _is_pence) in enumerate(numbers):
            if _price_in_range(value):
                amount_index = index
    if amount_index is None:
        return None
    amount = numbers[amount_index][0]

    weight = 0.0
    for index, (value, _is_pence) in enumerate(numbers):
        if index == amount_index:
            continue
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
        lines, raw_lines = _lines_from_scan(content)
        sale_date = _date_from_filename(source_file) or _date_from_text("\n".join(raw_lines))
        for line in lines:
            line["kill_date"] = sale_date
        if not lines:
            warnings.append(
                "No sale lines extracted from PDF"
                if raw_lines
                else "PDF contained no extractable text"
            )
        if sale_date is None:
            warnings.append("Could not parse sale date from payment advice")
        return {
            "farm": None,
            "sale_date": sale_date,
            "lines": lines,
            "warnings": warnings,
            "buyer": BUYER_GAMECHANGER,
        }

    if not looks_like_blade_pdf(text, source_file):
        warnings.append("PDF does not look like a Gamechanger payment advice")

    sale_date = _date_from_filename(source_file) or _date_from_text(text)
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
