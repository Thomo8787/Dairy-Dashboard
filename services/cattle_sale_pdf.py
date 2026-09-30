"""Shared helpers for cattle remittance PDFs."""

from __future__ import annotations

import io
import logging
import os
import re

from typing import Any

import pdfplumber

for _noisy in ("pdfminer", "pdfminer.pdffont", "pdfminer.pdfinterp"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)

_MIN_CARCASS_KG = 50.0
_MAX_CARCASS_KG = 900.0


def normalize_etag(value: str | None) -> str:
    """Normalize ear tags for matching herd events and remittances.

    DairyComp often zero-pads after the country letters (e.g. BE000214283270)
    while processors drop those zeros and may insert spaces (BE21428 3270).
    """
    raw = re.sub(r"\s+", "", (value or "").strip()).upper()
    if not raw:
        return ""
    if not re.match(r"^[A-Z]{2}", raw) and raw.isdigit() and len(raw) >= 10:
        raw = f"UK{raw}"
    match = re.match(r"^([A-Z]{2})0*(\d+)$", raw)
    if match:
        return f"{match.group(1)}{match.group(2)}"
    return raw


def extract_pdf_text(content: bytes) -> str:
    lines: list[str] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines.extend(text.splitlines())
    return "\n".join(lines)


_MAX_DECODED_IMAGE_BYTES = 4 * 1024 * 1024


def extract_pdf_images(content: bytes) -> list[bytes]:
    images: list[bytes] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            for image in page.images or []:
                stream = image.get("stream")
                if stream is None:
                    continue
                try:
                    data = stream.get_data()
                except Exception:
                    continue
                # Some remittance PDFs embed a huge unused bitmap. Decoding it
                # blows the 512MB Render cron, and it is not a page scan.
                if not data or len(data) > _MAX_DECODED_IMAGE_BYTES:
                    continue
                images.append(data)
    return images


_rapid_ocr = None


def _prepare_ocr_image(image_bytes: bytes, *, scale: int = 1, max_edge: int = 1600):
    from PIL import Image, ImageOps

    image = Image.open(io.BytesIO(image_bytes))
    if image.mode not in {"RGB", "L"}:
        image = image.convert("RGB")
    target_w = image.width * scale if scale and scale > 1 else image.width
    target_h = image.height * scale if scale and scale > 1 else image.height
    longest = max(target_w, target_h, 1)
    if longest > max_edge:
        ratio = max_edge / longest
        target_w = max(1, int(target_w * ratio))
        target_h = max(1, int(target_h * ratio))
    if (target_w, target_h) != (image.width, image.height):
        image = image.resize((target_w, target_h), Image.Resampling.LANCZOS)
        image = ImageOps.autocontrast(image)
    return image


def _winocr_words(image) -> list[dict[str, Any]]:
    try:
        import asyncio

        import winocr
    except ImportError:
        return []
    try:
        result = asyncio.run(winocr.recognize_pil(image, lang="en"))
    except Exception:
        logging.getLogger(__name__).exception("Windows OCR failed")
        return []
    words: list[dict[str, Any]] = []
    for line in getattr(result, "lines", None) or []:
        for word in getattr(line, "words", None) or []:
            rec = getattr(word, "bounding_rect", None)
            text = (getattr(word, "text", None) or "").strip()
            if not text or rec is None:
                continue
            words.append(
                {
                    "text": text,
                    "x": float(getattr(rec, "x", 0.0)),
                    "y": float(getattr(rec, "y", 0.0)),
                    "width": float(getattr(rec, "width", 0.0)),
                    "height": float(getattr(rec, "height", 0.0)),
                }
            )
    return words


def _rapidocr_engine():
    global _rapid_ocr
    if _rapid_ocr is False:
        return None
    if _rapid_ocr is not None:
        return _rapid_ocr
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        _rapid_ocr = False
        return None
    try:
        _rapid_ocr = RapidOCR(use_angle_cls=False)
    except Exception:
        logging.getLogger(__name__).exception("RapidOCR failed to start")
        _rapid_ocr = False
        return None
    return _rapid_ocr


def _rapidocr_words(image) -> list[dict[str, Any]]:
    engine = _rapidocr_engine()
    if engine is None:
        return []
    try:
        import numpy as np
    except ImportError:
        return []
    try:
        result, _elapsed = engine(np.array(image))
    except Exception:
        logging.getLogger(__name__).exception("RapidOCR failed")
        return []
    words: list[dict[str, Any]] = []
    for item in result or []:
        box, text, _score = item
        label = (text or "").strip()
        if not label or not box:
            continue
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        words.append(
            {
                "text": label,
                "x": min(xs),
                "y": min(ys),
                "width": max(xs) - min(xs),
                "height": max(ys) - min(ys),
            }
        )
    return words


def ocr_image_bytes(image_bytes: bytes) -> str:
    """OCR a scanned remittance image."""
    words = ocr_words_from_image(image_bytes, scale=2)
    if not words:
        return ""
    ordered = sorted(words, key=lambda word: (word["y"], word["x"]))
    return " ".join(word["text"] for word in ordered).strip()


def ocr_words_from_image(image_bytes: bytes, *, scale: int = 2) -> list[dict[str, Any]]:
    """OCR words with bounding boxes.

    Windows OCR is used when it is installed. Render's Linux cron uses RapidOCR.
    """
    try:
        image = _prepare_ocr_image(image_bytes, scale=scale)
    except Exception:
        logging.getLogger(__name__).exception("Could not open remittance image")
        return []
    words = _winocr_words(image)
    if words:
        return words
    return _rapidocr_words(image)


def extract_pdf_text_or_ocr(content: bytes) -> str:
    text = extract_pdf_text(content)
    if text.strip():
        return text
    parts = [ocr_image_bytes(image) for image in extract_pdf_images(content)]
    return "\n".join(part for part in parts if part)


def _weights_match(a: float, b: float, tolerance: float = 0.05) -> bool:
    return abs(a - b) <= tolerance


def is_rejected_sale(
    cold_weight_kg: float | None,
    reject_kg: float | None,
    amount_gbp: float | None,
) -> bool:
    """Animal rejected at abattoir: zero payment and cold weight equals reject kgs."""
    if cold_weight_kg is None or reject_kg is None or amount_gbp is None:
        return False
    if abs(amount_gbp) > 0.005:
        return False
    if cold_weight_kg < _MIN_CARCASS_KG or cold_weight_kg > _MAX_CARCASS_KG:
        return False
    return _weights_match(cold_weight_kg, reject_kg)
