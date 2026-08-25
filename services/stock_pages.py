"""Stock inventory page metadata for Flask templates."""

from __future__ import annotations

from typing import Any

from services.farms import FARM_CHART_COLORS, HERD_FARM_OPTIONS
from services.events_pages import farm_titles

STOCK_PAGES: dict[str, dict[str, Any]] = {
    "stock-accruals": {
        "heading": "Stock Accruals",
        "nav": "stock_accruals",
        "template": "stock_inventory/stock_accruals.html",
    },
    "heifer-inventory": {
        "heading": "Heifer Inventory",
        "nav": "stock_heifer_inventory",
        "template": "stock_inventory/heifer_inventory.html",
    },
    "beef-inventory": {
        "heading": "Beef Inventory",
        "nav": "stock_beef_inventory",
        "template": "stock_inventory/beef_inventory.html",
    },
    "calves-due": {
        "heading": "Calves Due",
        "nav": "stock_calves_due",
        "template": "stock_inventory/calves_due.html",
    },
    "heifers-due": {
        "heading": "Heifers Due",
        "nav": "stock_heifers_due",
        "template": "stock_inventory/heifers_due.html",
    },
}


def stock_template_extras() -> dict[str, Any]:
    return {
        "farm_options": list(HERD_FARM_OPTIONS),
        "farm_colors": FARM_CHART_COLORS,
        "farm_titles": farm_titles(),
    }
