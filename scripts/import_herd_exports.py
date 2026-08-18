"""CLI entrypoint for daily DairyComp DCEXPORT import."""

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
    parser = argparse.ArgumentParser(description="Import DairyComp DCEXPORT files from OneDrive")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reimport even when OneDrive last-modified fingerprints are unchanged",
    )
    args = parser.parse_args(argv)

    from services.database import get_session, init_db
    from services.herd_full_import import format_herd_import_summary, import_herd_exports

    init_db()
    with get_session() as session:
        result = import_herd_exports(session, force=args.force)
    print(format_herd_import_summary(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
