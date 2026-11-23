"""Save every Home tile. Render cron at 08:00 UTC, after the morning imports."""

from __future__ import annotations

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


def main() -> int:
    from services.database import get_session, init_db
    from services.home_tiles import refresh_home_tiles

    init_db()
    with get_session() as session:
        refresh_home_tiles(session, parts="all")
    print("Home tiles saved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
