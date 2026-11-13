"""Load the EMBRYOS sheet into the dashboard database."""

from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.database import get_session, init_db
from services.embryo_spreadsheet import parse_embryos_sheet, read_embryos_sheet, replace_spreadsheet_import

DEFAULT_PATH = (
    r"C:\Users\ALH\OneDrive\OneDrive - ALH Farm\Documents\Dashboard\Genetics"
    r"\ALH Home Embryos MAIN Copy 13th November.xlsb"
)


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH
    database_url = os.environ.get("DATABASE_URL", "")
    if database_url:
        parsed = urlparse(database_url)
        print(f"Database: {parsed.scheme} {parsed.hostname or 'local'}{parsed.path}")
    else:
        print("Database: local sqlite")

    rows = read_embryos_sheet(path)
    implants, skipped = parse_embryos_sheet(rows)
    kinds = Counter("fresh" if item.is_fresh else "frozen" for item in implants)
    outcomes = Counter(item.outcome or "none" for item in implants)
    print(f"Rows read: {len(implants)}")
    print(f"Fresh or unmarked: {kinds['fresh']}")
    print(f"Marked frozen: {kinds['frozen']}")
    print("Outcomes:", dict(outcomes))
    for message in skipped:
        print(message)

    init_db()
    with get_session() as session:
        summary = replace_spreadsheet_import(session, implants)
    print(
        "Saved {implants} recipients in {flushes} flushes and {stock_batches} frozen batches. "
        "Replaced {removed_batches} earlier import batches.".format(**summary)
    )


if __name__ == "__main__":
    main()
