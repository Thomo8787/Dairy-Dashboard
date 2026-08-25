"""Backfill milk collections from farm EOM workbooks."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import EOM milk collections")
    parser.add_argument(
        "--file",
        default=None,
        help="Import a single workbook instead of every mapped EOM file",
    )
    parser.add_argument("--farm", default=None, help="Farm code when using --file")
    parser.add_argument("--producer-ref", default=None, help="Producer ref when using --file")
    parser.add_argument(
        "--from",
        dest="date_from",
        default="2026-04-01",
        help="Earliest collection date to import (YYYY-MM-DD)",
    )
    args = parser.parse_args(argv)

    from services.database import init_db
    from services.eom_import import import_all_eom_workbooks, import_eom_collections

    init_db()
    date_from = date.fromisoformat(args.date_from)
    if args.file:
        result = import_eom_collections(
            Path(args.file),
            producer_ref=args.producer_ref or "641565",
            farm=args.farm,
            date_from=date_from,
        )
        results = [result]
    else:
        results = import_all_eom_workbooks(date_from=date_from)

    for result in results:
        if result.get("error"):
            print(f"{result['farm']}: {result['source_file']} — {result['error']}")
            continue
        print(
            f"{result['farm']}: parsed {result['rows_parsed']} loads, "
            f"inserted {result['rows_inserted']}, updated {result['rows_updated']}, "
            f"missing sample {result['rows_missing_sample']} "
            f"({result['date_from']} to {result['date_to']})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
