"""CLI entrypoint for parlour email sync (Task Scheduler / cron friendly)."""

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
    parser = argparse.ArgumentParser(description="Sync DataFlow parlour emails")
    parser.add_argument("--farm", default="ALH")
    parser.add_argument(
        "--days-back",
        type=int,
        default=None,
        help="Look back this many days (required with --overwrite)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Delete and replace parlour rows in the selected date window",
    )
    parser.add_argument("--top", type=int, default=80)
    args = parser.parse_args(argv)

    from services.parlour_sync import format_sync_summary, sync_parlour_emails

    if args.overwrite and not args.days_back:
        parser.error("--overwrite requires --days-back")

    result = sync_parlour_emails(
        farm_code=args.farm,
        days_back=args.days_back,
        overwrite=args.overwrite,
        top=args.top,
    )
    print(format_sync_summary(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
