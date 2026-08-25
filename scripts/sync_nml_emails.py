"""CLI entrypoint for NML milk-quality PDF import (Task Scheduler / cron friendly)."""

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import NML PDFs from the DataFlow Outlook mailbox")
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="Look back this many days in the mailbox (default 14)",
    )
    parser.add_argument(
        "--full-history",
        action="store_true",
        help="Scan from 2000-01-01 instead of --days",
    )
    args = parser.parse_args(argv)

    from services.nml_import import format_nml_summary, import_nml_results

    days = None if args.full_history else max(1, args.days)
    result = import_nml_results(full_history=args.full_history, days=days)
    print(format_nml_summary(result))
    for warning in result.get("warnings") or []:
        print(f"warning: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
