#!/usr/bin/env python3
"""Pull Young Stock Health by Age All into the health-index history table.

Designed for hourly Render cron. Each hour overwrites the live table icon and
graph bar. Midnight, 6am, midday and 6pm UK become permanent history:

    python scripts/import_sensehub_youngstock.py

Each run also fills any missing locked 6-hour slots. Manual / forced run uses
the same command.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from services.database import SessionLocal, init_db
from services.sensehub_youngstock import backfill_youngstock_health, import_youngstock_health

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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import SenseHub young-stock health indexes."
    )
    parser.add_argument(
        "--only-at-uk-hours",
        type=str,
        default="",
        help="Comma-separated UK local hours when this job may run. Example: 0,6,12,18",
    )
    parser.add_argument(
        "--backfill-days",
        type=int,
        default=0,
        help="Re-run Young Stock Health by Age All at past 6-hour slots for this many days.",
    )
    parser.add_argument(
        "--backfill-all",
        action="store_true",
        help="Fill every missing SenseHub slot back to the oldest current calf.",
    )
    args = parser.parse_args()

    if args.backfill_all or (args.backfill_days and args.backfill_days > 0):
        args.only_at_uk_hours = ""

    if args.only_at_uk_hours.strip():
        try:
            allowed = _parse_hours(args.only_at_uk_hours)
        except ValueError as exc:
            print(f"Invalid --only-at-uk-hours: {exc}", file=sys.stderr)
            return 2
        now_uk = datetime.now(_UK)
        if now_uk.hour not in allowed:
            print(
                f"Skipping SenseHub youngstock import — UK time is "
                f"{now_uk:%Y-%m-%d %H:%M %Z}, allowed hours {sorted(allowed)}.",
                flush=True,
            )
            return 0

    init_db()
    db = SessionLocal()
    try:
        if args.backfill_all or (args.backfill_days and args.backfill_days > 0):
            result = backfill_youngstock_health(
                db,
                days=None if args.backfill_all else args.backfill_days,
                force=True,
            )
            print(
                f"Backfilled {result['slots']} slots, saved {result['saved']} rows "
                f"(span {result.get('span_days')} days)."
            )
            if result.get("errors"):
                print("Errors:", "; ".join(result["errors"][:5]), file=sys.stderr)
            return 0
        result = import_youngstock_health(db)
        print(
            f"Saved {result['saved']} young-stock health rows "
            f"({result.get('slot')} {result.get('sampled_at')})"
        )
        return 0
    except Exception as exc:
        print(f"Young-stock health import failed: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
