"""Heifers due report from herd_inventory table."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy.orm import Session

from services.expected_due_common import build_expected_due_report


def get_heifers_due_report(
    db: Session,
    farms: list[str] | None = None,
    due_from: dt.date | None = None,
    due_to: dt.date | None = None,
) -> dict[str, Any]:
    return build_expected_due_report(
        db,
        category="Youngstock",
        farms=farms,
        due_from=due_from,
        due_to=due_to,
    )
