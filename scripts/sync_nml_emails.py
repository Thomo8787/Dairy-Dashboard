"""CLI entrypoint for NML and cattle remittance PDF import (Render cron / Task Scheduler)."""

from __future__ import annotations

import argparse
import datetime as dt
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


def _sync_cattle_sales() -> bool:
    from services.cattle_sales_email import outlook_cattle_sales_configured
    from services.cattle_sales_import import sync_outlook_cattle_sales
    from services.database import get_session, init_db

    if not outlook_cattle_sales_configured():
        print("Cattle sales: skipped (Outlook mailbox not configured)")
        return True
    try:
        init_db()
        with get_session() as session:
            result = sync_outlook_cattle_sales(session)
    except Exception:
        logging.exception("Cattle-sale email sync failed")
        print("Cattle sales: failed")
        return False
    if not result:
        print("Cattle sales: no new remittance PDFs")
        return True
    print(
        "Cattle sales: "
        f"{result.get('files_processed', 0)} file(s), "
        f"{result.get('rows_inserted', 0)} inserted, "
        f"{result.get('rows_updated', 0)} updated"
    )
    for warning in result.get("warnings") or []:
        print(f"warning: {warning}")
    for skipped in result.get("skipped_files") or []:
        print(f"skipped: {skipped}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import NML and cattle remittance PDFs from the DataFlow Outlook mailbox"
    )
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
    parser.add_argument(
        "--since",
        help="Only emails on/after this date (YYYY-MM-DD). Overrides --days.",
    )
    args = parser.parse_args(argv)

    from services.nml_import import format_nml_summary, import_nml_results

    since = None
    if args.since:
        try:
            since = dt.date.fromisoformat(args.since)
        except ValueError:
            parser.error("--since must be YYYY-MM-DD")

    days = None if args.full_history or since else max(1, args.days)
    result = import_nml_results(full_history=args.full_history, days=days, since=since)
    print(format_nml_summary(result))
    for warning in result.get("warnings") or []:
        print(f"warning: {warning}")

    sales_ok = _sync_cattle_sales()
    return 0 if sales_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
