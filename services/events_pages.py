"""Events page metadata and Flask query helpers."""

from __future__ import annotations

from datetime import date
from typing import Any

from flask import request

from services.events_common import DISEASE_EVENT_LABELS, DISEASE_FILTER_OPTIONS
from services.farms import FARM_CHART_COLORS, FARMS, HERD_FARM_OPTIONS

EVENT_PAGES: dict[str, dict[str, Any]] = {
    "calvings": {
        "heading": "Calvings",
        "chart_title": "Calvings by Month",
        "nav": "events_calvings",
        "template": "events/report.html",
        "show_lact_filter": True,
    },
    "sales": {
        "heading": "Sales",
        "chart_title": "Sales by Month",
        "nav": "events_sales",
        "template": "events/report.html",
        "show_parity_filter": True,
        "show_parity_beef": True,
        "show_reason_table": True,
    },
    "deaths": {
        "heading": "Deaths",
        "chart_title": "Deaths by Month",
        "nav": "events_deaths",
        "template": "events/report.html",
        "show_parity_filter": True,
    },
    "disease": {
        "heading": "Disease",
        "chart_title": "Disease Episodes by Month",
        "nav": "events_disease",
        "template": "events/report.html",
        "show_parity_filter": True,
        "show_disease_filter": True,
        "show_disease_scatter": True,
    },
    "hooftrimming": {
        "heading": "Hoof Trimming",
        "chart_title": "Foot Trims by Month",
        "nav": "events_hooftrimming",
        "template": "events/report.html",
        "show_hooftrimming_charts": True,
    },
    "breedings": {
        "heading": "Breedings",
        "chart_title": "Breedings by Month",
        "nav": "events_breedings",
        "template": "events/report.html",
        "show_parity_filter": True,
        "show_breedings_semen_chart": True,
        "show_breedings_semen_table": True,
        "show_breedings_sire_settings": True,
    },
    "births": {
        "heading": "Births",
        "chart_title": "Births by Month",
        "nav": "events_births",
        "template": "events/births.html",
    },
    "total-protein": {
        "heading": "Total Protein",
        "chart_title": "Calf Serum Total Protein",
        "nav": "events_total_protein",
        "template": "events/total_protein.html",
    },
}


def farm_titles() -> dict[str, str]:
    titles = {farm.code: f"{farm.name} ({farm.code})" for farm in FARMS}
    titles["total"] = "Combined (All Farms)"
    return titles


def events_template_extras(slug: str, *, is_admin: bool) -> dict[str, Any]:
    page = EVENT_PAGES[slug]
    extras = {
        "page_heading": page["heading"],
        "chart_title": page["chart_title"],
        "api_slug": slug,
        "farm_options": list(HERD_FARM_OPTIONS),
        "farm_colors": FARM_CHART_COLORS,
        "farm_titles": farm_titles(),
        "show_lact_filter": bool(page.get("show_lact_filter")),
        "show_parity_filter": bool(page.get("show_parity_filter")),
        "show_parity_beef": bool(page.get("show_parity_beef")),
        "parity_exclusive": False,
        "parity_default_both": False,
        "show_disease_filter": bool(page.get("show_disease_filter")),
        "show_disease_scatter": bool(page.get("show_disease_scatter")),
        "show_reason_table": bool(page.get("show_reason_table")),
        "show_breedings_semen_chart": bool(page.get("show_breedings_semen_chart")),
        "show_breedings_semen_table": bool(page.get("show_breedings_semen_table")),
        "show_breedings_sire_settings": bool(page.get("show_breedings_sire_settings")),
        "show_hooftrimming_charts": bool(page.get("show_hooftrimming_charts")),
        "can_edit_sires": is_admin,
        "disease_options": [
            {"value": code, "label": DISEASE_EVENT_LABELS.get(code, code)}
            for code in DISEASE_FILTER_OPTIONS
        ],
    }
    return extras


def _parse_date_arg(name: str) -> date | None:
    raw = (request.args.get(name) or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _parse_int_arg(name: str) -> int | None:
    raw = (request.args.get(name) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None
