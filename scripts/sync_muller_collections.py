#!/usr/bin/env python3
"""Download the current month's Müller collection CSVs.

Designed for Render cron. Schedule hourly and pass ``--only-at-uk-hours 6``
so the download stays at 06:00 UK time through GMT and BST:

    python scripts/sync_muller_collections.py --only-at-uk-hours 6

Manual run (ignores the hour gate):

    python scripts/sync_muller_collections.py
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

_UK = ZoneInfo("Europe/London")


def _parse_hours(raw: str) -> set[int]:
    hours: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        hour = int(part)
        if hour < 0 or hour > 23:
            raise ValueError(f"Invalid hour {hour!r} (expected 0-23)")
        hours.add(hour)
    return hours


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download Müller collection CSVs")
    parser.add_argument(
        "--only-at-uk-hours",
        default="",
        help="Comma-separated UK hours (Europe/London) when this job may run.",
    )
    args = parser.parse_args(argv)

    if args.only_at_uk_hours.strip():
        try:
            allowed = _parse_hours(args.only_at_uk_hours)
        except ValueError as exc:
            print(f"Invalid --only-at-uk-hours: {exc}", file=sys.stderr)
            return 2
        now_uk = datetime.now(_UK)
        if now_uk.hour not in allowed:
            print(
                f"Skipping Müller download — UK time is {now_uk:%Y-%m-%d %H:%M %Z}, "
                f"allowed hours {sorted(allowed)}.",
                flush=True,
            )
            return 0

    from services.database import init_db
    from services.muller_portal import MullerPortalError, muller_is_configured, sync_muller_collections

    if not muller_is_configured():
        print(
            "Müller login is not set. Add MULLER_USERNAME and MULLER_PASSWORD "
            "on the dairy-dashboard service in Render.",
            flush=True,
        )
        return 0

    init_db()
    try:
        result = sync_muller_collections()
    except MullerPortalError as exc:
        print(f"Müller download failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"Müller {result['month']}: {', '.join(result['farms'])} — "
        f"updated {result['rows_updated']}, "
        f"unmatched {result['rows_unmatched']}, "
        f"ambiguous {result['rows_ambiguous']}",
        flush=True,
    )
    for item in result.get("errors") or []:
        print(f"  skipped {item.get('source_file')}: {item.get('error')}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
