"""CLI entrypoint for cattle remittance import (Render cron sales_data_cron)."""

from __future__ import annotations

import logging
import os
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


def main() -> int:
    os.environ["CATTLE_SALES_CRON"] = "1"
    from services.cattle_sales_email import outlook_cattle_sales_configured
    from services.cattle_sales_import import sync_outlook_cattle_sales
    from services.database import get_session, init_db

    if not outlook_cattle_sales_configured():
        print("Cattle sales: skipped (Outlook mailbox not configured)")
        return 0
    try:
        init_db()
        with get_session() as session:
            result = sync_outlook_cattle_sales(session)
    except Exception:
        logging.exception("Cattle-sale email sync failed")
        print("Cattle sales: failed")
        return 1
    if not result:
        print("Cattle sales: no new remittance PDFs")
        return 0
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
