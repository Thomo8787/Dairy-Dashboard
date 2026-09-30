"""Cattle sales matching helpers for remittance lines."""

from __future__ import annotations

from sqlalchemy import or_

from services.database import CowEvent
from services.events_common import sales_classified_event_clause

EVENT_MATCH_WINDOW_DAYS = 14
CATTLE_SALES_JV_EXIT_EVENTS: tuple[str, ...] = ("GAME", "PATH", "PATHWAY")


def cattle_sales_exit_event_clause():
    """Sale-exit events for remittance matching and the sales-payments queue."""
    return or_(
        sales_classified_event_clause(),
        CowEvent.event.in_(list(CATTLE_SALES_JV_EXIT_EVENTS)),
    )
