#!/usr/bin/env python3
"""Import all SenseHub heat / health / custom reports.

Designed for Render cron. Schedule hourly and pass ``--only-at-uk-hours 6``
so BST/GMT stay correct and every report (including No Data) is refreshed once
a day:

    python scripts/import_sensehub.py --only-at-uk-hours 6

Manual / forced run (ignores the hour gate):

    python scripts/import_sensehub.py
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
from services.sensehub_import import import_sensehub

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
        description="Import all SenseHub reports into the local database."
    )
    parser.add_argument(
        "--only-at-uk-hours",
        type=str,
        default="",
        help=(
            "Comma-separated UK local hours (Europe/London) when this job may run. "
            "Exit 0 without importing outside those hours. Example: 6"
        ),
    )
    args = parser.parse_args()

    if args.only_at_uk_hours.strip():
        try:
            allowed = _parse_hours(args.only_at_uk_hours)
        except ValueError as exc:
            print(f"Invalid --only-at-uk-hours: {exc}", file=sys.stderr)
            return 2
        now_uk = datetime.now(_UK)
        if now_uk.hour not in allowed:
            print(
                f"Skipping SenseHub report import — UK time is "
                f"{now_uk:%Y-%m-%d %H:%M %Z}, allowed hours {sorted(allowed)}.",
                flush=True,
            )
            return 0

    init_db()
    db = SessionLocal()
    try:
        result = import_sensehub(db)
        print(
            f"Imported {result['reports_imported']} SenseHub reports "
            f"({result['rows_imported']} rows)"
        )
        if result.get("farm_name"):
            print(f"Farm: {result['farm_name']} ({result.get('farm_id')})")
        if result.get("latest_import"):
            print(f"Latest import: {result['latest_import']}")
        return 0
    except Exception as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
