"""Monthly NML quality averages presented like Cwrt milk statements."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

from sqlalchemy import select

from services.database import NmlMilkResult, get_session
from services.nml_results import _load_weight, _weighted_avg
from services.events_common import (
    _fiscal_year_calendar_bounds,
    _fiscal_year_from_date,
    _iter_month_starts,
)
from services.farms import FARMS, FARMS_BY_CODE

_FARM_ORDER = tuple(farm.code for farm in FARMS)
_QUALITY_FIELDS = (
    "butterfat_pct",
    "protein_pct",
    "scc",
    "bactoscan",
    "fpd",
    "urea_pct",
)
_INT_FIELDS = ("scc", "bactoscan", "fpd")


def _normalise_farms(farms: list[str] | None) -> list[str]:
    if not farms:
        return ["ALH"]
    selected = {part.strip().upper() for part in farms if part and part.strip()}
    ordered = [code for code in _FARM_ORDER if code in selected]
    return ordered or ["ALH"]


def _combine(records: list[NmlMilkResult]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "sample_count": len(records) or None,
        "litres_sold": None,
        "milk_price_ppl": None,
        "haulage_ppl": None,
        "antibiotic_fails": sum(1 for r in records if r.antibiotic_pass is False),
    }
    for field in _QUALITY_FIELDS:
        digits = 3 if field == "urea_pct" else 0 if field in _INT_FIELDS else 2
        mean = _weighted_avg(records, field, digits)
        if mean is None:
            out[field] = None
        elif field in _INT_FIELDS:
            out[field] = int(round(mean))
        else:
            out[field] = mean
    litres = sum(_load_weight(r) or 0.0 for r in records)
    out["litres_sold"] = round(litres, 0) if litres else None
    return out


def list_nml_statements(
    *,
    fiscal_year: int | None = None,
    farms: list[str] | None = None,
) -> dict[str, Any]:
    with get_session() as session:
        all_rows = session.scalars(select(NmlMilkResult)).all()

    fiscal_year_options = sorted(
        {
            _fiscal_year_from_date(r.sample_date)
            for r in all_rows
            if r.sample_date
        },
        reverse=True,
    )
    if fiscal_year is None:
        fiscal_year = (
            fiscal_year_options[0]
            if fiscal_year_options
            else _fiscal_year_from_date(dt.date.today())
        )
    if fiscal_year not in fiscal_year_options:
        fiscal_year_options = sorted(set(fiscal_year_options) | {fiscal_year}, reverse=True)

    selected_farms = _normalise_farms(farms)
    selected_rows = [
        r for r in all_rows if r.farm in selected_farms and r.sample_date
    ]
    by_month: dict[dt.date, list[NmlMilkResult]] = defaultdict(list)
    for row in selected_rows:
        by_month[row.sample_date.replace(day=1)].append(row)

    fy_start, fy_end = _fiscal_year_calendar_bounds(fiscal_year)
    months = _iter_month_starts(fy_start, fy_end)
    rows = []
    for month_start in months:
        records = by_month.get(month_start, [])
        item = {
            "month_label": month_start.strftime("%b-%y"),
            "statement_month": month_start.isoformat(),
            "has_data": bool(records),
        }
        item.update(_combine(records))
        rows.append(item)

    year_records = [r for m in months for r in by_month.get(m, [])]
    total = {
        "month_label": "Total / Avg",
        "statement_month": None,
        "is_total": True,
        **_combine(year_records),
    }

    if len(selected_farms) > 1:
        farm_label = " + ".join(selected_farms) + " — litre-weighted"
    else:
        farm = FARMS_BY_CODE.get(selected_farms[0])
        farm_label = farm.name if farm else selected_farms[0]

    return {
        "fiscal_year": fiscal_year,
        "fiscal_year_label": f"Apr {fiscal_year - 1} – Mar {fiscal_year}",
        "fiscal_year_options": fiscal_year_options,
        "farms": selected_farms,
        "farm_label": farm_label,
        "is_weighted": True,
        "source": "nml",
        "rows": rows,
        "total": total,
        "months_with_data": sum(1 for r in rows if r.get("has_data")),
    }
