"""OCR one scanned cattle remittance, then resume the cron queue.

This process imports only the PDF parser. It does not load the database
stack, so RapidOCR can fit in the 512MB Render cron.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("ocr_cattle_sale_pdf")


def _json_ready(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    return value


def _parse(pdf_path: Path, hint: str, source_file: str) -> dict[str, Any]:
    content = pdf_path.read_bytes()
    if hint == "market_drayton":
        from services.market_drayton_pdf import parse_market_drayton_pdf

        return parse_market_drayton_pdf(content, source_file=source_file)
    from services.neilds_pdf import parse_neilds_pdf

    return parse_neilds_pdf(content, source_file=source_file)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OCR one scanned cattle remittance PDF")
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--hint", default="neilds")
    parser.add_argument("--source-file", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--resume", required=True)
    args = parser.parse_args(argv)

    pdf_path = Path(args.pdf)
    out_path = Path(args.out)
    hint = (args.hint or "neilds").strip().lower()
    logger.info("OCR cattle-sale %s (%s bytes)", args.source_file, pdf_path.stat().st_size if pdf_path.is_file() else 0)
    try:
        parsed = _parse(pdf_path, hint, args.source_file)
    except Exception as exc:  # noqa: BLE001
        logger.exception("OCR failed for %s", args.source_file)
        parsed = {
            "farm": None,
            "sale_date": None,
            "lines": [],
            "warnings": [f"OCR failed: {exc}"],
            "buyer": hint,
        }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps({"parsed": _json_ready(parsed)}),
        encoding="utf-8",
    )
    logger.info(
        "OCR finished %s: %s line(s)",
        args.source_file,
        len(parsed.get("lines") or []),
    )
    os.execv(sys.executable, [sys.executable, args.resume, "--cattle-sales-only"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
