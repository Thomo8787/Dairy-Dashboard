"""Load Müller collection CSV litres onto Collections weighbridge values."""

from __future__ import annotations

import argparse
import logging
import sys
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


def _print_examples(label: str, rows: list[dict], limit: int = 8) -> None:
    if not rows:
        return
    print(f"  {label}:")
    for row in rows[:limit]:
        dates = row.get("row_dates")
        extra = f" existing={','.join(dates)}" if dates else ""
        if row.get("row_date"):
            extra = f" row={row['row_date']}{extra}"
        print(
            f"    {row['farm']} sample {row['sample_id']} "
            f"{row['actual_date'] or row['payment_date']} "
            f"{row['litres']:.0f} L{extra}"
        )
    remaining = len(rows) - limit
    if remaining > 0:
        print(f"    … {remaining} more")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import Müller weighbridge litres")
    parser.add_argument(
        "--dir",
        default=None,
        help="Folder of Müller CSVs (default: data/Muller_Milk_Collections_Monthly)",
    )
    parser.add_argument("--file", default=None, help="Import one CSV instead of the folder")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Match and report without writing weighbridge values",
    )
    args = parser.parse_args(argv)

    from services.database import init_db
    from services.muller_collections_import import (
        import_muller_collections,
        import_muller_folder,
    )

    init_db()
    apply = not args.dry_run
    if args.file:
        results = [import_muller_collections(Path(args.file), apply=apply)]
    else:
        results = import_muller_folder(Path(args.dir) if args.dir else None, apply=apply)

    mode = "applied" if apply else "dry-run"
    for result in results:
        if result.get("error"):
            print(f"{result.get('farm') or '?'}: {result['source_file']} — {result['error']}")
            continue
        print(
            f"{result['farm']}: {result['source_file']} ({mode}) "
            f"parsed {result['rows_parsed']}, updated {result['rows_updated']}, "
            f"temps filled {result['temps_filled']}, "
            f"unmatched {result['rows_unmatched']}, ambiguous {result['rows_ambiguous']}, "
            f"date gap {result.get('rows_date_gap', 0)}"
        )
        _print_examples("unmatched", result.get("unmatched") or [])
        _print_examples("ambiguous", result.get("ambiguous") or [])
        _print_examples("date gap", result.get("date_gaps") or [])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
