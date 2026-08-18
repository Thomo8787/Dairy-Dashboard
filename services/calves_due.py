"""Calves due report from herd_inventory table."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy.orm import Session

from services.expected_due_common import build_expected_due_report


def get_calves_due_report(
    db: Session,
    farms: list[str] | None = None,
    breeds: list[str] | None = None,
    due_from: dt.date | None = None,
    due_to: dt.date | None = None,
) -> dict[str, Any]:
    return build_expected_due_report(
        db,
        farms=farms,
        breeds=breeds,
        due_from=due_from,
        due_to=due_to,
        include_breed_options=True,
        include_farm_breed_summary=True,
        rc_values=(5, 6),
    )
