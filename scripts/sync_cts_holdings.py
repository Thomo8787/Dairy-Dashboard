#!/usr/bin/env python3
"""Sync CTS cattle-on-holding for all configured farms.

Designed for Render cron. Schedule hourly and pass ``--only-at-uk-hours 2``
so the job runs at 02:00 Europe/London year-round (GMT and BST):

    python scripts/sync_cts_holdings.py --only-at-uk-hours 2

Manual / forced run (ignores the hour gate):

    python scripts/sync_cts_holdings.py

ALH and BNK share one CTS holding; ``sync_farms`` normalises them to a single
pull so the shared CPH is not hit twice.
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
logger = logging.getLogger(__name__)

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
    parser = argparse.ArgumentParser(
        description="Sync CTS cattle-on-holding for all ready farms."
    )
    parser.add_argument(
        "--only-at-uk-hours",
        type=str,
        default="",
        help=(
            "Comma-separated UK local hours (Europe/London) when this job may run. "
            "Exit 0 without syncing outside those hours. Example: 2"
        ),
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
                f"Skipping CTS holdings sync — UK time is "
                f"{now_uk:%Y-%m-%d %H:%M %Z}, allowed hours {sorted(allowed)}.",
                flush=True,
            )
            return 0

    from services.cts_client import CtsError
    from services.cts_reconcile import sync_farms
    from services.database import get_session, init_db

    init_db()
    try:
        with get_session() as session:
            # farms=None → all ready farms; ALH/BNK shared holding synced once.
            payload = sync_farms(session, farms=None, source="cron")
    except CtsError as exc:
        logger.error("CTS sync hard failure: %s", exc)
        return 1
    except Exception:
        logger.exception("CTS sync failed unexpectedly")
        return 1

    results = payload.get("results") or []
    warnings = payload.get("warnings") or []
    for warning in warnings:
        logger.warning("%s", warning)
    for row in results:
        farm = row.get("farm") or "?"
        animals = row.get("animal_count", row.get("cts_count", "?"))
        archived = row.get("archived_confirmed", 0)
        requeued = row.get("requeued_stale_awaiting", 0)
        logger.info(
            "CTS sync farm=%s animals=%s archived_confirmed=%s requeued_stale=%s",
            farm,
            animals,
            archived,
            requeued,
        )

    if not results and warnings:
        logger.error(
            "CTS sync produced no farm results (%s warning(s))",
            len(warnings),
        )
        return 1
    if not payload.get("ok", False):
        logger.error("CTS sync completed with hard failures")
        return 1

    logger.info(
        "CTS holdings sync complete: %s farm(s), %s warning(s)",
        len(results),
        len(warnings),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
