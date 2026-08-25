"""Shared month pivot for cow event reports."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import and_, case, func, literal, or_, select
from sqlalchemy.orm import Session

from services.database import CowEvent, HerdBirth
from services.farms import HERD_FARM_OPTIONS
from services.breeding_sires import classify_semen_type, load_sire_overrides
from services.herd_import_utils import BEEF_CBREED_MIN


def _farm_zero_counts() -> dict[str, int]:
    return {code: 0 for code in HERD_FARM_OPTIONS}


def _row_from_farm_counts(counts: dict[str, int], extra: dict[str, Any]) -> dict[str, Any]:
    row = dict(extra)
    total = 0
    for code in HERD_FARM_OPTIONS:
        value = int(counts.get(code, 0) or 0)
        row[code] = value
        total += value
    row["total"] = total
    return row


EVENT_PAGE_TYPES: dict[str, tuple[str, ...]] = {
    "calvings": ("FRESH",),
    "sales": ("SOLD",),
    "deaths": ("DIED",),
    "breedings": ("BRED",),
    "disease": ("ILL", "SCOURS", "LAME", "MAST", "METR", "RP", "RESP", "INJURY", "ABORT", "DA", "MF"),
    "hooftrimming": ("FOOTRIM", "LAME"),
}

DISEASE_EVENT_LABELS: dict[str, str] = {
    "ILL": "Illness",
    "SCOURS": "Scours",
    "LAME": "Lameness",
    "MAST": "Mastitis (all)",
    "MAST_ABX": "Mastitis - Abx",
    "METR": "Metritis",
    "RP": "Retained Placenta",
    "RESP": "Respiratory",
    "INJURY": "Injury",
    "ABORT": "Abortion",
    "DA": "Displaced Abomasum",
    "MF": "Milk Fever",
    "DIED": "Died",
}

# Virtual disease filters that query a base DairyComp event plus an extra predicate.
DISEASE_VIRTUAL_EVENT_MAP: dict[str, str] = {
    "MAST_ABX": "MAST",
}

DISEASE_FILTER_OPTIONS: tuple[str, ...] = (
    "ILL",
    "SCOURS",
    "LAME",
    "MAST",
    "MAST_ABX",
    "METR",
    "RP",
    "RESP",
    "INJURY",
    "ABORT",
    "DA",
    "MF",
    "DIED",
)

DEFAULT_DISEASE_EPISODE_GAP_DAYS = 7

# Minimum days between counted episodes for the same cow and disease type.
DISEASE_EPISODE_GAP_DAYS: dict[str, int] = {
    code: DEFAULT_DISEASE_EPISODE_GAP_DAYS for code in DISEASE_FILTER_OPTIONS
}

LACTATION_GROUPS: tuple[str, ...] = ("1", "2", "3+")
PARITY_GROUPS: tuple[str, ...] = ("primiparous", "multiparous", "beef")
PAGES_WITH_PARITY_FILTER: frozenset[str] = frozenset({"sales", "deaths", "disease", "breedings"})
SALES_REASON_ORDER: tuple[str, ...] = ("OFS", "TB", "Beef", "Dairy", "CULL")
SALES_TABLE_REASON_ORDER: tuple[str, ...] = ("CULL", "TB", "OFS", "Beef", "Dairy")
SALES_DAIRY_REMARKS: tuple[str, ...] = ("CAR18", "CAR19")
SALES_TB_REMARKS: tuple[str, ...] = ("CAR11", "TB")
SALES_MAPPED_REMARKS: tuple[str, ...] = ("OFS", *SALES_TB_REMARKS, "CAR16", *SALES_DAIRY_REMARKS)
# DairyComp DIED + these remarks used to be rewritten to SOLD on import for sales
# reporting. We now keep the original DIED event for BCMS, and still treat them as
# sales in reporting via sales_classified_event_clause().
DIED_AS_SALES_REMARKS: tuple[str, ...] = ("TB", "OFS")
# Fallen-stock collectors recorded on DEST (DairyComp item codes).
OFS_DEST_PREFIXES: tuple[str, ...] = ("OFS", "CNEILD", "CSARG")
BREEDINGS_SEMEN_ORDER: tuple[str, ...] = ("beef", "dairy", "unknown")
BREEDINGS_CHART_SEMEN_ORDER: tuple[str, ...] = ("beef", "dairy", "unknown")


def _dest_upper():
    return func.upper(func.trim(CowEvent.dest))


def ofs_dest_clause():
    """DEST is OFS or a fallen-stock collector (Cneild / CSarg…)."""
    dest = _dest_upper()
    return and_(
        CowEvent.dest.isnot(None),
        or_(*[dest.like(f"{prefix}%") for prefix in OFS_DEST_PREFIXES]),
    )


def ofs_sales_clause():
    """Remark OFS, or DEST is a fallen-stock collector / OFS."""
    return or_(CowEvent.remark == "OFS", ofs_dest_clause())


def died_as_sales_clause():
    """DIED rows that Sales treats as TB/OFS (not fallen-stock Deaths)."""
    return and_(
        CowEvent.event == "DIED",
        or_(
            CowEvent.remark.in_(list(DIED_AS_SALES_REMARKS)),
            ofs_dest_clause(),
        ),
    )


def sales_classified_event_clause():
    """SOLD, plus DIED events that sales reporting treats as TB/OFS sales."""
    return or_(
        CowEvent.event == "SOLD",
        died_as_sales_clause(),
    )


def death_report_event_clause():
    """True DIED events for deaths/fallen-stock reports (exclude TB/OFS sales-deaths)."""
    return and_(
        CowEvent.event == "DIED",
        ~or_(
            and_(
                CowEvent.remark.isnot(None),
                CowEvent.remark.in_(list(DIED_AS_SALES_REMARKS)),
            ),
            ofs_dest_clause(),
        ),
    )

# Deaths report: for youngstock (lact == 0) exclude very-early deaths and
# deaths recorded with generic/unwanted reasons so they don't skew the report.
DEATHS_YOUNGSTOCK_MIN_AGE_DAYS = 4
DEATHS_YOUNGSTOCK_EXCLUDED_REMARKS: tuple[str, ...] = ("OTHER", "SMALL", "BVD")


def normalize_farms(farms: list[str] | None) -> list[str]:
    if not farms:
        return list(HERD_FARM_OPTIONS)
    return [f for f in farms if f in HERD_FARM_OPTIONS]


def normalize_lact_groups(lact_groups: list[str] | None) -> list[str] | None:
    if not lact_groups:
        return None
    selected = [group for group in lact_groups if group in LACTATION_GROUPS]
    return selected or None


def _apply_lact_groups(query, lact_groups: list[str] | None):
    if not lact_groups:
        return query
    conditions = []
    if "1" in lact_groups:
        conditions.append(CowEvent.lact == 1)
    if "2" in lact_groups:
        conditions.append(CowEvent.lact == 2)
    if "3+" in lact_groups:
        conditions.append(CowEvent.lact >= 3)
    if not conditions:
        return query
    return query.where(or_(*conditions))


def normalize_parity_groups(parity_groups: list[str] | None) -> list[str] | None:
    if not parity_groups:
        return None
    selected = [group for group in parity_groups if group in PARITY_GROUPS]
    return selected or None


def normalize_disease_type(disease: str | None) -> str | None:
    if not disease:
        return None
    disease = disease.strip().upper()
    if disease not in DISEASE_FILTER_OPTIONS:
        return None
    return disease


def disease_db_event_types(event_types: tuple[str, ...]) -> tuple[str, ...]:
    """Map disease filter codes (incl. virtual) to CowEvent.event values."""
    mapped = [DISEASE_VIRTUAL_EVENT_MAP.get(code, code) for code in event_types]
    return tuple(dict.fromkeys(mapped))


def is_loxicom_only_mastitis_remark(remark: str | None) -> bool:
    """True when the DairyComp remark marks a Loxicom-only mastitis treatment."""
    if remark is None:
        return False
    return remark.strip().upper() == "LOXICOM"


def normalize_semen_types(semen_types: list[str] | None) -> list[str] | None:
    if not semen_types:
        return None
    selected = [value.lower() for value in semen_types if value.lower() in BREEDINGS_SEMEN_ORDER]
    return selected or None


def resolve_page_event_types(page_slug: str, disease: str | None = None) -> tuple[str, ...]:
    event_types = EVENT_PAGE_TYPES.get(page_slug)
    if not event_types:
        raise ValueError(f"Unknown events page: {page_slug}")
    if page_slug == "disease":
        selected = normalize_disease_type(disease)
        if selected:
            return (selected,)
    return event_types


def _apply_parity_groups(query, parity_groups: list[str] | None, *, split_beef: bool = False):
    if not parity_groups:
        return query

    if split_beef:
        # Sales page: Cows / dairy Youngstock / Beef (aligned with Stock Accruals).
        conditions = []
        if "multiparous" in parity_groups:
            conditions.append(and_(CowEvent.lact.isnot(None), CowEvent.lact > 0))
        if "primiparous" in parity_groups:
            conditions.append(
                and_(
                    CowEvent.lact == 0,
                    func.upper(func.coalesce(CowEvent.gndr, "")) == "F",
                    CowEvent.cbrd.isnot(None),
                    CowEvent.cbrd < BEEF_CBREED_MIN,
                )
            )
        if "beef" in parity_groups:
            conditions.append(
                and_(
                    CowEvent.lact == 0,
                    or_(
                        func.upper(func.coalesce(CowEvent.gndr, "")) != "F",
                        CowEvent.cbrd.is_(None),
                        CowEvent.cbrd >= BEEF_CBREED_MIN,
                    ),
                )
            )
        if not conditions:
            return query
        return query.where(or_(*conditions))

    conditions = []
    if "primiparous" in parity_groups:
        conditions.append(CowEvent.lact == 0)
    if "multiparous" in parity_groups:
        conditions.append(CowEvent.lact > 0)
    if not conditions:
        return query
    return query.where(or_(*conditions))


def _fiscal_year_from_date(value: dt.date) -> int:
    return value.year + 1 if value.month >= 4 else value.year


def _sort_key_from_date(value: dt.date) -> int:
    month = value.month
    fiscal_year = _fiscal_year_from_date(value)
    month_adjusted = month - 3 if month >= 4 else month + 9
    return fiscal_year * 100 + month_adjusted


def _month_start(value: dt.date) -> dt.date:
    return value.replace(day=1)


def _iter_month_starts(start: dt.date, end: dt.date) -> list[dt.date]:
    current = _month_start(start)
    end_month = _month_start(end)
    months: list[dt.date] = []
    while current <= end_month:
        months.append(current)
        if current.month == 12:
            current = dt.date(current.year + 1, 1, 1)
        else:
            current = dt.date(current.year, current.month + 1, 1)
    return months


def _month_count_inclusive(event_from: dt.date, event_to: dt.date) -> int:
    return len(_iter_month_starts(event_from, event_to))


def _build_range_summary(farm_totals: dict[str, int], month_count: int) -> dict[str, Any]:
    def avg(total: int) -> float:
        return round(total / month_count, 1) if month_count else 0.0

    grand_total = sum(int(farm_totals.get(code, 0) or 0) for code in HERD_FARM_OPTIONS)
    result: dict[str, Any] = {
        "total": grand_total,
        "month_count": month_count,
        "average_per_month": avg(grand_total),
        "farms": {},
    }
    for code in HERD_FARM_OPTIONS:
        total = int(farm_totals.get(code, 0) or 0)
        block = {"total": total, "average_per_month": avg(total)}
        result["farms"][code] = block
        result[code] = block
    return result


def _empty_range_summary() -> dict[str, Any]:
    return _build_range_summary(_farm_zero_counts(), 0)



def _get_fiscal_year_options(
    db: Session,
    event_types: tuple[str, ...],
    selected_farms: list[str],
) -> list[int]:
    rows = db.execute(
        select(CowEvent.fiscal_year)
        .where(CowEvent.event.in_(list(event_types)))
        .where(CowEvent.event_date.isnot(None))
        .where(CowEvent.farm.in_(selected_farms))
        .where(CowEvent.fiscal_year.isnot(None))
        .distinct()
        .order_by(CowEvent.fiscal_year.desc())
    ).all()
    return [int(row[0]) for row in rows if row[0] is not None]


def _apply_fiscal_year(query, fiscal_year: int | None):
    if fiscal_year is None:
        return query
    return query.where(CowEvent.fiscal_year == fiscal_year)


def _sales_reason_expression():
    return case(
        (ofs_sales_clause(), literal("OFS")),
        (CowEvent.remark.in_(list(SALES_TB_REMARKS)), literal("TB")),
        (CowEvent.remark == "CAR16", literal("Beef")),
        (CowEvent.remark.in_(list(SALES_DAIRY_REMARKS)), literal("Dairy")),
        else_=literal("CULL"),
    )


def _build_sales_table_rows(
    db: Session,
    *,
    selected_farms: list[str],
    effective_from: dt.date,
    effective_to: dt.date,
    selected_parity_groups: list[str] | None,
    fiscal_year: int | None,
) -> list[dict[str, Any]]:
    reason_expr = _sales_reason_expression()
    counts_query = (
        select(
            CowEvent.month_label,
            reason_expr.label("reason"),
            CowEvent.farm,
            func.count(),
        )
        .where(sales_classified_event_clause())
        .where(CowEvent.event_date.isnot(None))
        .where(CowEvent.farm.in_(selected_farms))
        .where(CowEvent.event_date >= effective_from)
        .where(CowEvent.event_date <= effective_to)
    )
    counts_query = _apply_parity_groups(counts_query, selected_parity_groups, split_beef=True)
    counts_query = _apply_fiscal_year(counts_query, fiscal_year)
    counts = db.execute(
        counts_query.group_by(CowEvent.month_label, reason_expr, CowEvent.farm).order_by(
            func.min(CowEvent.sort_key), reason_expr
        )
    ).all()

    pivot: dict[str, dict[str, dict[str, int]]] = {}
    for month_label, reason, farm, count in counts:
        if not month_label or not reason or farm not in selected_farms:
            continue
        month_key = str(month_label)
        reason_key = str(reason)
        pivot.setdefault(month_key, {})
        pivot[month_key].setdefault(farm, {name: 0 for name in SALES_TABLE_REASON_ORDER})
        if reason_key in pivot[month_key][farm]:
            pivot[month_key][farm][reason_key] = int(count)

    table_rows: list[dict[str, Any]] = []
    for month_start in _iter_month_starts(effective_from, effective_to):
        event_month = month_start.strftime("%b-%y")
        month_counts = pivot.get(event_month, {})
        row: dict[str, Any] = {
            "event_month": event_month,
            "sort_key": _sort_key_from_date(month_start),
        }
        for farm in selected_farms:
            row[farm] = {
                reason: month_counts.get(farm, {}).get(reason, 0)
                for reason in SALES_TABLE_REASON_ORDER
            }
        table_rows.append(row)
    return table_rows


def _fiscal_year_calendar_bounds(fiscal_year: int) -> tuple[dt.date, dt.date]:
    """UK fiscal year: Apr (FY-1) through Mar (FY)."""
    return dt.date(fiscal_year - 1, 4, 1), dt.date(fiscal_year, 3, 31)


def _clamp_date(value: dt.date, min_date: dt.date, max_date: dt.date) -> dt.date:
    return max(min_date, min(value, max_date))


def _get_date_bounds(
    db: Session,
    event_types: tuple[str, ...],
    selected_farms: list[str],
    *,
    today: dt.date | None = None,
) -> tuple[dt.date | None, dt.date | None]:
    row = db.execute(
        select(
            func.min(CowEvent.event_date),
            func.max(CowEvent.event_date),
        )
        .where(CowEvent.event.in_(list(event_types)))
        .where(CowEvent.event_date.isnot(None))
        .where(CowEvent.farm.in_(selected_farms))
    ).one()
    min_date = row[0]
    max_date = row[1]
    if min_date is None or max_date is None:
        return None, None
    if hasattr(min_date, "date"):
        min_date = min_date.date()
    if hasattr(max_date, "date"):
        max_date = max_date.date()
    # Keep the axis open through the current month even when no events have
    # happened yet (e.g. no DAs in July so far).
    reference = today or dt.date.today()
    if max_date < reference:
        max_date = reference
    return min_date, max_date


def _zero_fill_rows(
    pivot: dict[str, dict[str, int]],
    event_from: dt.date,
    event_to: dt.date,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for month_start in _iter_month_starts(event_from, event_to):
        sort_key = _sort_key_from_date(month_start)
        event_month = month_start.strftime("%b-%y")
        counts = pivot.get(event_month, _farm_zero_counts())
        rows.append(
            _row_from_farm_counts(
                counts,
                {"event_month": event_month, "sort_key": sort_key},
            )
        )
    return rows


def _disease_episode_gap_days(event: str) -> int:
    return DISEASE_EPISODE_GAP_DAYS.get(event, DEFAULT_DISEASE_EPISODE_GAP_DAYS)


def _animal_identifier(cow_id: str | None, etag: str | None) -> str | None:
    if cow_id:
        return str(cow_id)
    if etag:
        return f"etag:{etag}"
    return None


def _disease_event_date(record: dict[str, Any]) -> dt.date | None:
    """Event date for disease reports (CSV Date column, not EDAT)."""
    event_date = record.get("event_date")
    if event_date is None:
        return None
    if hasattr(event_date, "date"):
        return event_date.date()
    return event_date


def filter_disease_episode_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop repeat disease rows within each disease-specific episode gap for one animal."""
    sorted_records = sorted(
        records,
        key=lambda record: (
            record.get("farm") or "",
            _animal_identifier(record.get("cow_id"), record.get("etag")) or "",
            record.get("event") or "",
            _disease_event_date(record) or dt.date.min,
        ),
    )
    last_counted: dict[tuple[str, str, str], dt.date] = {}
    kept: list[dict[str, Any]] = []
    for record in sorted_records:
        event_date = _disease_event_date(record)
        farm = record.get("farm")
        event = record.get("event")
        if event_date is None or not farm or not event:
            continue
        animal = _animal_identifier(record.get("cow_id"), record.get("etag"))
        if animal is None:
            kept.append(record)
            continue
        key = (farm, animal, event)
        gap_days = _disease_episode_gap_days(event)
        previous = last_counted.get(key)
        if previous is None or (event_date - previous).days > gap_days:
            kept.append(record)
            last_counted[key] = event_date
    return kept


def filter_disease_episodes(
    events: list[tuple[str | None, str | None, str | None, dt.date | None, str | None, str | None]],
) -> list[tuple[str | None, str | None, str | None, dt.date | None, str | None, str | None]]:
    records = [
        {
            "cow_id": cow_id,
            "etag": etag,
            "event": event,
            "event_date": event_date,
            "farm": farm,
            "month_label": month_label,
        }
        for cow_id, etag, event, event_date, farm, month_label in events
    ]
    filtered = filter_disease_episode_records(records)
    return [
        (
            record.get("cow_id"),
            record.get("etag"),
            record.get("event"),
            record.get("event_date"),
            record.get("farm"),
            record.get("month_label"),
        )
        for record in filtered
    ]


def _fetch_disease_event_records(
    db: Session,
    *,
    event_types: tuple[str, ...],
    selected_farms: list[str],
    effective_from: dt.date,
    effective_to: dt.date,
    selected_parity_groups: list[str] | None,
    fiscal_year: int | None,
) -> list[dict[str, Any]]:
    db_event_types = disease_db_event_types(event_types)
    mastitis_abx_only = event_types == ("MAST_ABX",)
    if "DIED" in db_event_types:
        # Match Deaths / fallen stock: DIED+TB/OFS (remark or DEST) are sales, not true deaths.
        non_died = [event for event in db_event_types if event != "DIED"]
        event_clause = (
            or_(death_report_event_clause(), CowEvent.event.in_(non_died))
            if non_died
            else death_report_event_clause()
        )
    else:
        event_clause = CowEvent.event.in_(list(db_event_types))
    events_query = (
        select(
            CowEvent.cow_id,
            CowEvent.etag,
            CowEvent.event,
            CowEvent.event_date,
            CowEvent.fdat,
            CowEvent.bdat,
            CowEvent.lact,
            CowEvent.farm,
            CowEvent.month_label,
            CowEvent.remark,
        )
        .where(event_clause)
        .where(CowEvent.event_date.isnot(None))
        .where(CowEvent.farm.in_(selected_farms))
        .where(CowEvent.event_date >= effective_from)
        .where(CowEvent.event_date <= effective_to)
    )
    if mastitis_abx_only:
        # Exclude Loxicom-only mastitis (Remark = LOXICOM); keep antibiotic / other cases.
        events_query = events_query.where(
            func.upper(func.trim(func.coalesce(CowEvent.remark, ""))) != "LOXICOM"
        )
    events_query = _apply_parity_groups(events_query, selected_parity_groups)
    events_query = _apply_fiscal_year(events_query, fiscal_year)
    rows = db.execute(events_query).all()
    records: list[dict[str, Any]] = []
    for row in rows:
        event = row[2]
        lact = row[6]
        event_date = row[3]
        bdat = row[5]
        remark = row[9]
        # Same youngstock death exclusions as the Deaths page when DIED is included.
        if (
            event == "DIED"
            and lact == 0
            and _exclude_youngstock_death(remark, event_date, bdat)
        ):
            continue
        records.append(
            {
                "cow_id": row[0],
                "etag": row[1],
                "event": event,
                "event_date": event_date,
                "fdat": row[4],
                "bdat": bdat,
                "lact": lact,
                "farm": row[7],
                "month_label": row[8],
                "remark": remark,
            }
        )
    return records


def _episode_pivot_from_records(
    records: list[dict[str, Any]],
    selected_farms: list[str],
) -> dict[str, dict[str, int]]:
    pivot: dict[str, dict[str, int]] = {}
    for record in records:
        event_date = _disease_event_date(record)
        farm = record.get("farm")
        if event_date is None or farm not in selected_farms:
            continue
        month_label = record.get("month_label")
        month_key = str(month_label or event_date.strftime("%b-%y"))
        pivot.setdefault(month_key, _farm_zero_counts())
        if farm in pivot[month_key]:
            pivot[month_key][farm] += 1
    return pivot


def _record_y_value(
    record: dict[str, Any],
    selected_parity_groups: list[str] | None,
) -> int | None:
    youngstock = selected_parity_groups == ["primiparous"]
    event_day = _disease_event_date(record)
    if event_day is None:
        return None
    base_date = record.get("bdat") if youngstock else record.get("fdat")
    if base_date is None:
        return None
    if hasattr(base_date, "date"):
        base_date = base_date.date()
    y_value = (event_day - base_date).days
    if y_value < 0:
        return None
    return y_value


def _disease_y_bounds(
    records: list[dict[str, Any]],
    selected_parity_groups: list[str] | None,
) -> dict[str, int]:
    y_values = [_record_y_value(record, selected_parity_groups) for record in records]
    valid = [y for y in y_values if y is not None]
    return {"min": 0, "max": max(valid, default=0)}


def _apply_y_range_filter(
    records: list[dict[str, Any]],
    selected_parity_groups: list[str] | None,
    y_min: int | None,
    y_max: int | None,
) -> list[dict[str, Any]]:
    if y_min is None and y_max is None:
        return records
    lo = y_min if y_min is not None else 0
    hi = y_max if y_max is not None else 10**9
    filtered: list[dict[str, Any]] = []
    for record in records:
        y_value = _record_y_value(record, selected_parity_groups)
        if y_value is None:
            continue
        if lo <= y_value <= hi:
            filtered.append(record)
    return filtered


def _build_disease_scatter(
    records: list[dict[str, Any]],
    selected_parity_groups: list[str] | None,
    *,
    y_bounds: dict[str, int] | None = None,
) -> dict[str, Any]:
    youngstock = selected_parity_groups == ["primiparous"]
    y_axis_label = "Age (days)" if youngstock else "DIM"
    points: list[dict[str, Any]] = []
    for record in records:
        event_day = _disease_event_date(record)
        farm = record.get("farm")
        y_value = _record_y_value(record, selected_parity_groups)
        if event_day is None or not farm or y_value is None:
            continue
        points.append(
            {
                "x": event_day.isoformat(),
                "y": y_value,
                "farm": farm,
                "event": record.get("event"),
            }
        )
    bounds = y_bounds or _disease_y_bounds(records, selected_parity_groups)
    return {
        "points": points,
        "y_axis_label": y_axis_label,
        "y_bounds": bounds,
    }


def _cohort_date_for_record(
    record: dict[str, Any],
    selected_parity_groups: list[str] | None,
) -> dt.date | None:
    youngstock = selected_parity_groups == ["primiparous"]
    cohort_date = record.get("bdat") if youngstock else record.get("fdat")
    if cohort_date is None:
        return None
    if hasattr(cohort_date, "date"):
        return cohort_date.date()
    return cohort_date


def _fetch_cow_cohort_rows(
    db: Session,
    selected_farms: list[str],
) -> list[tuple[str, str | None, str | None, dt.date]]:
    fresh_rows = db.execute(
        select(CowEvent.farm, CowEvent.cow_id, CowEvent.etag, CowEvent.fdat)
        .where(CowEvent.event == "FRESH")
        .where(CowEvent.fdat.isnot(None))
        .where(CowEvent.farm.in_(selected_farms))
    ).all()
    if fresh_rows:
        return [(str(r[0]), r[1], r[2], r[3]) for r in fresh_rows]
    fallback = db.execute(
        select(CowEvent.farm, CowEvent.cow_id, CowEvent.etag, CowEvent.fdat)
        .where(CowEvent.lact > 0)
        .where(CowEvent.fdat.isnot(None))
        .where(CowEvent.farm.in_(selected_farms))
    ).all()
    return [(str(r[0]), r[1], r[2], r[3]) for r in fallback]


def _fetch_youngstock_cohort_rows(
    db: Session,
    selected_farms: list[str],
) -> list[tuple[str, str | None, str | None, dt.date]]:
    birth_rows = db.execute(
        select(HerdBirth.farm, HerdBirth.cow_id, HerdBirth.etag, HerdBirth.bdat)
        .where(HerdBirth.bdat.isnot(None))
        .where(HerdBirth.farm.in_(selected_farms))
    ).all()
    if birth_rows:
        return [(str(r[0]), r[1], r[2], r[3]) for r in birth_rows]
    fallback = db.execute(
        select(CowEvent.farm, CowEvent.cow_id, CowEvent.etag, CowEvent.bdat)
        .where(CowEvent.lact == 0)
        .where(CowEvent.bdat.isnot(None))
        .where(CowEvent.farm.in_(selected_farms))
    ).all()
    return [(str(r[0]), r[1], r[2], r[3]) for r in fallback]


def _build_cohort_denominators(
    cohort_rows: list[tuple[str, str | None, str | None, dt.date]],
    selected_farms: list[str],
) -> dict[str, dict[str, int]]:
    seen: set[tuple[str, str, dt.date]] = set()
    denominators: dict[str, dict[str, int]] = {}
    for farm, cow_id, etag, cohort_date in cohort_rows:
        if farm not in selected_farms:
            continue
        animal = _animal_identifier(cow_id, etag)
        if animal is None:
            continue
        dedupe_key = (farm, animal, cohort_date)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        month_label = cohort_date.strftime("%b-%y")
        denominators.setdefault(month_label, _farm_zero_counts())
        denominators[month_label][farm] = denominators[month_label].get(farm, 0) + 1
    return denominators


def _build_disease_incidence(
    db: Session,
    *,
    filtered_records: list[dict[str, Any]],
    selected_farms: list[str],
    selected_parity_groups: list[str] | None,
    effective_from: dt.date,
    effective_to: dt.date,
) -> dict[str, Any]:
    youngstock = selected_parity_groups == ["primiparous"]
    x_axis_label = "Birth month" if youngstock else "Calving month"
    cohort_label = "Births" if youngstock else "Calvings"
    if youngstock:
        cohort_rows = _fetch_youngstock_cohort_rows(db, selected_farms)
    else:
        cohort_rows = _fetch_cow_cohort_rows(db, selected_farms)

    denominators = _build_cohort_denominators(cohort_rows, selected_farms)
    affected: dict[str, dict[str, set[str]]] = {}
    for record in filtered_records:
        farm = record.get("farm")
        cohort_date = _cohort_date_for_record(record, selected_parity_groups)
        animal = _animal_identifier(record.get("cow_id"), record.get("etag"))
        if not farm or cohort_date is None or animal is None:
            continue
        month_label = cohort_date.strftime("%b-%y")
        affected.setdefault(month_label, {}).setdefault(farm, set()).add(animal)

    rows: list[dict[str, Any]] = []
    for month_start in _iter_month_starts(effective_from, effective_to):
        month_label = month_start.strftime("%b-%y")
        row: dict[str, Any] = {
            "cohort_month": month_label,
            "sort_key": _sort_key_from_date(month_start),
            "cohort": {},
            "affected": {},
        }
        for farm in selected_farms:
            denom = denominators.get(month_label, {}).get(farm, 0)
            numer = len(affected.get(month_label, {}).get(farm, set()))
            row["cohort"][farm] = denom
            row["affected"][farm] = numer
            row[farm] = round(100 * numer / denom, 1) if denom else 0.0
        rows.append(row)

    month_labels = [month_start.strftime("%b-%y") for month_start in _iter_month_starts(
        effective_from, effective_to
    )]
    summary: dict[str, Any] = {
        "cohort_label": cohort_label,
        "total": {"incidence_pct": 0.0, "cohort_size": 0, "affected": 0},
    }
    total_affected: set[str] = set()
    total_denominator = 0
    for farm in selected_farms:
        farm_affected: set[str] = set()
        farm_denominator = 0
        for month_label in month_labels:
            farm_denominator += denominators.get(month_label, {}).get(farm, 0)
            farm_affected |= affected.get(month_label, {}).get(farm, set())
        farm_pct = round(100 * len(farm_affected) / farm_denominator, 1) if farm_denominator else 0.0
        summary[farm] = {
            "incidence_pct": farm_pct,
            "cohort_size": farm_denominator,
            "affected": len(farm_affected),
        }
        total_affected |= farm_affected
        total_denominator += farm_denominator
    summary["total"] = {
        "incidence_pct": (
            round(100 * len(total_affected) / total_denominator, 1) if total_denominator else 0.0
        ),
        "cohort_size": total_denominator,
        "affected": len(total_affected),
    }

    return {
        "rows": rows,
        "x_axis_label": x_axis_label,
        "cohort_label": cohort_label,
        "summary": summary,
    }


def _coerce_date(value: Any) -> dt.date | None:
    if value is None:
        return None
    if hasattr(value, "date"):
        return value.date()
    return value


def _exclude_youngstock_death(
    remark: str | None,
    event_date: Any,
    bdat: Any,
) -> bool:
    """Whether a lact==0 (youngstock) death should be excluded from the report.

    Excludes deaths recorded with remarks OTHER / SMALL / BVD, and deaths that
    occur under DEATHS_YOUNGSTOCK_MIN_AGE_DAYS days old (age = event date - birth
    date). Records with no birth date can't be aged, so they're kept unless the
    remark rule applies.
    """
    if remark and remark.strip().upper() in DEATHS_YOUNGSTOCK_EXCLUDED_REMARKS:
        return True
    event_day = _coerce_date(event_date)
    birth_day = _coerce_date(bdat)
    if event_day is not None and birth_day is not None:
        age_days = (event_day - birth_day).days
        if age_days < DEATHS_YOUNGSTOCK_MIN_AGE_DAYS:
            return True
    return False


def _build_deaths_pivot(
    db: Session,
    *,
    selected_farms: list[str],
    effective_from: dt.date,
    effective_to: dt.date,
    selected_parity_groups: list[str] | None,
    fiscal_year: int | None,
) -> dict[str, dict[str, int]]:
    """Month/farm pivot of DIED events with youngstock exclusions applied.

    For lact == 0 animals, drops deaths under DEATHS_YOUNGSTOCK_MIN_AGE_DAYS days
    old and deaths with remarks OTHER / SMALL / BVD. Counts are built in Python to
    keep age math portable across SQLite and PostgreSQL.
    """
    query = (
        select(
            CowEvent.month_label,
            CowEvent.farm,
            CowEvent.lact,
            CowEvent.remark,
            CowEvent.event_date,
            CowEvent.bdat,
        )
        .where(death_report_event_clause())
        .where(CowEvent.event_date.isnot(None))
        .where(CowEvent.farm.in_(selected_farms))
        .where(CowEvent.event_date >= effective_from)
        .where(CowEvent.event_date <= effective_to)
    )
    query = _apply_parity_groups(query, selected_parity_groups)
    query = _apply_fiscal_year(query, fiscal_year)
    rows = db.execute(query).all()

    pivot: dict[str, dict[str, int]] = {}
    for month_label, farm, lact, remark, event_date, bdat in rows:
        if not month_label or farm not in selected_farms:
            continue
        if lact == 0 and _exclude_youngstock_death(remark, event_date, bdat):
            continue
        key = str(month_label)
        pivot.setdefault(key, _farm_zero_counts())
        if farm in pivot[key]:
            pivot[key][farm] += 1
    return pivot


def _build_standard_event_pivot(
    db: Session,
    *,
    event_types: tuple[str, ...],
    selected_farms: list[str],
    effective_from: dt.date,
    effective_to: dt.date,
    selected_lact_groups: list[str] | None,
    selected_parity_groups: list[str] | None,
    fiscal_year: int | None,
    split_beef: bool = False,
    sales_classified: bool = False,
) -> dict[str, dict[str, int]]:
    counts_query = (
        select(
            CowEvent.month_label,
            CowEvent.farm,
            func.count(),
        )
        .where(CowEvent.event_date.isnot(None))
        .where(CowEvent.farm.in_(selected_farms))
        .where(CowEvent.event_date >= effective_from)
        .where(CowEvent.event_date <= effective_to)
    )
    if sales_classified:
        counts_query = counts_query.where(sales_classified_event_clause())
    else:
        counts_query = counts_query.where(CowEvent.event.in_(list(event_types)))
    counts_query = _apply_lact_groups(counts_query, selected_lact_groups)
    counts_query = _apply_parity_groups(
        counts_query, selected_parity_groups, split_beef=split_beef
    )
    counts_query = _apply_fiscal_year(counts_query, fiscal_year)
    counts = db.execute(
        counts_query.group_by(CowEvent.month_label, CowEvent.farm).order_by(
            func.min(CowEvent.sort_key)
        )
    ).all()

    pivot: dict[str, dict[str, int]] = {}
    for month_label, farm, count in counts:
        if not month_label:
            continue
        key = str(month_label)
        pivot.setdefault(key, _farm_zero_counts())
        if farm in pivot[key]:
            pivot[key][farm] = int(count)
    return pivot


def _build_disease_episode_bundle(
    db: Session,
    *,
    event_types: tuple[str, ...],
    selected_farms: list[str],
    effective_from: dt.date,
    effective_to: dt.date,
    selected_parity_groups: list[str] | None,
    fiscal_year: int | None,
    y_min: int | None = None,
    y_max: int | None = None,
) -> tuple[dict[str, dict[str, int]], dict[str, Any], dict[str, Any]]:
    raw_records = _fetch_disease_event_records(
        db,
        event_types=event_types,
        selected_farms=selected_farms,
        effective_from=effective_from,
        effective_to=effective_to,
        selected_parity_groups=selected_parity_groups,
        fiscal_year=fiscal_year,
    )
    kept_records = filter_disease_episode_records(raw_records)
    y_bounds = _disease_y_bounds(kept_records, selected_parity_groups)
    filtered_records = _apply_y_range_filter(
        kept_records, selected_parity_groups, y_min, y_max
    )
    pivot = _episode_pivot_from_records(filtered_records, selected_farms)
    scatter = _build_disease_scatter(
        filtered_records,
        selected_parity_groups,
        y_bounds=y_bounds,
    )
    incidence = _build_disease_incidence(
        db,
        filtered_records=filtered_records,
        selected_farms=selected_farms,
        selected_parity_groups=selected_parity_groups,
        effective_from=effective_from,
        effective_to=effective_to,
    )
    return pivot, scatter, incidence


def _fetch_breeding_records(
    db: Session,
    *,
    selected_farms: list[str],
    effective_from: dt.date,
    effective_to: dt.date,
    selected_parity_groups: list[str] | None,
    fiscal_year: int | None,
) -> list[tuple[str, str, str, dt.date]]:
    query = (
        select(
            CowEvent.month_label,
            CowEvent.farm,
            CowEvent.remark,
            CowEvent.event_date,
        )
        .where(CowEvent.event == "BRED")
        .where(CowEvent.event_date.isnot(None))
        .where(CowEvent.farm.in_(selected_farms))
        .where(CowEvent.event_date >= effective_from)
        .where(CowEvent.event_date <= effective_to)
    )
    query = _apply_parity_groups(query, selected_parity_groups)
    query = _apply_fiscal_year(query, fiscal_year)
    rows = db.execute(query).all()
    result: list[tuple[str, str, str, dt.date]] = []
    for month_label, farm, remark, event_date in rows:
        if not month_label or farm not in selected_farms or event_date is None:
            continue
        if hasattr(event_date, "date"):
            event_date = event_date.date()
        result.append((str(month_label), str(farm), str(remark or ""), event_date))
    return result


HOOF_DIM_BIN_SIZE = 30
HOOF_DIM_MAX_BIN_START = 570


def _normalize_lame_protocol(raw: str | None) -> str:
    value = (raw or "").strip()
    return value if value else "Unknown"


def _dim_bin_start(dim: float) -> int:
    bin_start = int(dim // HOOF_DIM_BIN_SIZE) * HOOF_DIM_BIN_SIZE
    return min(bin_start, HOOF_DIM_MAX_BIN_START)


def _dim_bin_label(bin_start: int) -> str:
    if bin_start >= HOOF_DIM_MAX_BIN_START:
        return f"{HOOF_DIM_MAX_BIN_START}+"
    return f"{bin_start} to {bin_start + HOOF_DIM_BIN_SIZE - 1}"


def normalize_lame_protocols(protocols: list[str] | None) -> list[str] | None:
    """None / empty = all protocols (no filter). Otherwise the selected list."""
    if not protocols:
        return None
    cleaned = [_normalize_lame_protocol(value) for value in protocols if value is not None]
    # Preserve order while de-duplicating.
    seen: set[str] = set()
    ordered: list[str] = []
    for value in cleaned:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered or None


def _lame_protocol_match_condition(selected_protocols: set[str]):
    named = sorted(p for p in selected_protocols if p != "Unknown")
    include_unknown = "Unknown" in selected_protocols
    conditions = []
    if named:
        conditions.append(func.trim(func.coalesce(CowEvent.protocols, "")).in_(named))
    if include_unknown:
        conditions.append(
            or_(
                CowEvent.protocols.is_(None),
                func.trim(CowEvent.protocols) == "",
            )
        )
    if not conditions:
        return literal(False)
    if len(conditions) == 1:
        return conditions[0]
    return or_(*conditions)


def _apply_lame_protocol_filter(query, selected_protocols: set[str] | None):
    if selected_protocols is None:
        return query
    return query.where(_lame_protocol_match_condition(selected_protocols))


def _list_available_lame_protocols(
    db: Session,
    *,
    selected_farms: list[str],
    effective_from: dt.date,
    effective_to: dt.date,
    fiscal_year: int | None,
) -> list[str]:
    query = (
        select(CowEvent.protocols, func.count())
        .where(CowEvent.event == "LAME")
        .where(CowEvent.event_date.isnot(None))
        .where(CowEvent.farm.in_(selected_farms))
        .where(CowEvent.event_date >= effective_from)
        .where(CowEvent.event_date <= effective_to)
    )
    query = _apply_fiscal_year(query, fiscal_year)
    rows = db.execute(query.group_by(CowEvent.protocols)).all()
    totals: dict[str, int] = {}
    for protocols, count in rows:
        protocol = _normalize_lame_protocol(protocols)
        totals[protocol] = totals.get(protocol, 0) + int(count)
    return [
        protocol
        for protocol, _ in sorted(
            totals.items(), key=lambda item: (-item[1], item[0].lower())
        )
    ]


def _build_lame_month_pivot(
    db: Session,
    *,
    selected_farms: list[str],
    effective_from: dt.date,
    effective_to: dt.date,
    fiscal_year: int | None,
    selected_protocols: set[str] | None,
) -> dict[str, dict[str, int]]:
    query = (
        select(
            CowEvent.month_label,
            CowEvent.farm,
            func.count(),
        )
        .where(CowEvent.event == "LAME")
        .where(CowEvent.event_date.isnot(None))
        .where(CowEvent.farm.in_(selected_farms))
        .where(CowEvent.event_date >= effective_from)
        .where(CowEvent.event_date <= effective_to)
    )
    query = _apply_fiscal_year(query, fiscal_year)
    query = _apply_lame_protocol_filter(query, selected_protocols)
    counts = db.execute(query.group_by(CowEvent.month_label, CowEvent.farm)).all()

    pivot: dict[str, dict[str, int]] = {}
    for month_label, farm, count in counts:
        if not month_label or farm not in selected_farms:
            continue
        key = str(month_label)
        pivot.setdefault(key, _farm_zero_counts())
        pivot[key][farm] = int(count)
    return pivot


def _build_hoof_main_pivot(
    db: Session,
    *,
    selected_farms: list[str],
    effective_from: dt.date,
    effective_to: dt.date,
    fiscal_year: int | None,
    selected_protocols: set[str] | None,
) -> dict[str, dict[str, int]]:
    """FOOTRIM (all) + LAME (optionally protocol-filtered)."""
    footrim_pivot = _build_standard_event_pivot(
        db,
        event_types=("FOOTRIM",),
        selected_farms=selected_farms,
        effective_from=effective_from,
        effective_to=effective_to,
        selected_lact_groups=None,
        selected_parity_groups=None,
        fiscal_year=fiscal_year,
    )
    lame_pivot = _build_lame_month_pivot(
        db,
        selected_farms=selected_farms,
        effective_from=effective_from,
        effective_to=effective_to,
        fiscal_year=fiscal_year,
        selected_protocols=selected_protocols,
    )
    pivot: dict[str, dict[str, int]] = {}
    for source in (footrim_pivot, lame_pivot):
        for month_label, counts in source.items():
            pivot.setdefault(month_label, _farm_zero_counts())
            for farm in selected_farms:
                pivot[month_label][farm] = pivot[month_label].get(farm, 0) + int(
                    counts.get(farm, 0)
                )
    return pivot


def _build_lame_protocol_rows(
    db: Session,
    *,
    selected_farms: list[str],
    effective_from: dt.date,
    effective_to: dt.date,
    fiscal_year: int | None,
    selected_protocols: set[str] | None,
) -> list[dict[str, Any]]:
    query = (
        select(
            CowEvent.protocols,
            CowEvent.farm,
            func.count(),
        )
        .where(CowEvent.event == "LAME")
        .where(CowEvent.event_date.isnot(None))
        .where(CowEvent.farm.in_(selected_farms))
        .where(CowEvent.event_date >= effective_from)
        .where(CowEvent.event_date <= effective_to)
    )
    query = _apply_fiscal_year(query, fiscal_year)
    query = _apply_lame_protocol_filter(query, selected_protocols)
    rows = db.execute(query.group_by(CowEvent.protocols, CowEvent.farm)).all()

    pivot: dict[str, dict[str, int]] = {}
    for protocols, farm, count in rows:
        if farm not in selected_farms:
            continue
        protocol = _normalize_lame_protocol(protocols)
        pivot.setdefault(protocol, _farm_zero_counts())
        pivot[protocol][farm] = int(count)

    result: list[dict[str, Any]] = []
    for protocol, counts in pivot.items():
        result.append(_row_from_farm_counts(counts, {"protocol": protocol}))
    result.sort(key=lambda row: (-row["total"], row["protocol"].lower()))
    return result


def _build_lame_dim_rows(
    db: Session,
    *,
    selected_farms: list[str],
    effective_from: dt.date,
    effective_to: dt.date,
    fiscal_year: int | None,
    selected_protocols: set[str] | None,
) -> list[dict[str, Any]]:
    query = (
        select(
            CowEvent.dim,
            CowEvent.farm,
        )
        .where(CowEvent.event == "LAME")
        .where(CowEvent.event_date.isnot(None))
        .where(CowEvent.dim.isnot(None))
        .where(CowEvent.farm.in_(selected_farms))
        .where(CowEvent.event_date >= effective_from)
        .where(CowEvent.event_date <= effective_to)
    )
    query = _apply_fiscal_year(query, fiscal_year)
    query = _apply_lame_protocol_filter(query, selected_protocols)
    rows = db.execute(query).all()

    pivot: dict[int, dict[str, int]] = {}
    for dim, farm in rows:
        if farm not in selected_farms or dim is None:
            continue
        bin_start = _dim_bin_start(float(dim))
        pivot.setdefault(bin_start, _farm_zero_counts())
        pivot[bin_start][farm] = pivot[bin_start].get(farm, 0) + 1

    if not pivot:
        return []

    result: list[dict[str, Any]] = []
    bin_end = min(max(pivot), HOOF_DIM_MAX_BIN_START)
    for bin_start in range(min(pivot), bin_end + 1, HOOF_DIM_BIN_SIZE):
        counts = pivot.get(bin_start, _farm_zero_counts())
        result.append(
            _row_from_farm_counts(
                counts,
                {
                    "bin_start": bin_start,
                    "bin_label": _dim_bin_label(bin_start),
                },
            )
        )
    return result


def _resolve_hoof_protocol_selection(
    available_protocols: list[str],
    requested_protocols: list[str] | None,
) -> set[str] | None:
    """Return None for all protocols, otherwise the selected set."""
    if requested_protocols is None:
        return None
    available = set(available_protocols)
    selected = {p for p in requested_protocols if p in available}
    if not available:
        return set()
    if selected and selected == available:
        return None
    return selected


def _build_hooftrimming_bundle(
    db: Session,
    *,
    selected_farms: list[str],
    effective_from: dt.date,
    effective_to: dt.date,
    fiscal_year: int | None,
    requested_protocols: list[str] | None,
) -> tuple[
    dict[str, dict[str, int]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[str],
]:
    available_protocols = _list_available_lame_protocols(
        db,
        selected_farms=selected_farms,
        effective_from=effective_from,
        effective_to=effective_to,
        fiscal_year=fiscal_year,
    )
    selected_protocols = _resolve_hoof_protocol_selection(
        available_protocols, requested_protocols
    )

    main_pivot = _build_hoof_main_pivot(
        db,
        selected_farms=selected_farms,
        effective_from=effective_from,
        effective_to=effective_to,
        fiscal_year=fiscal_year,
        selected_protocols=selected_protocols,
    )
    lame_rows = _zero_fill_rows(
        _build_lame_month_pivot(
            db,
            selected_farms=selected_farms,
            effective_from=effective_from,
            effective_to=effective_to,
            fiscal_year=fiscal_year,
            selected_protocols=selected_protocols,
        ),
        effective_from,
        effective_to,
    )
    protocol_rows = _build_lame_protocol_rows(
        db,
        selected_farms=selected_farms,
        effective_from=effective_from,
        effective_to=effective_to,
        fiscal_year=fiscal_year,
        selected_protocols=selected_protocols,
    )
    dim_rows = _build_lame_dim_rows(
        db,
        selected_farms=selected_farms,
        effective_from=effective_from,
        effective_to=effective_to,
        fiscal_year=fiscal_year,
        selected_protocols=selected_protocols,
    )
    return main_pivot, lame_rows, protocol_rows, dim_rows, available_protocols



def _build_breedings_semen_summary(
    farm_totals: dict[str, dict[str, int]],
    selected_farms: list[str],
    month_count: int,
) -> dict[str, Any]:
    by_farm: dict[str, dict[str, int]] = {}
    total = {name: 0 for name in BREEDINGS_SEMEN_ORDER}
    for farm in selected_farms:
        counts = farm_totals.get(farm, {name: 0 for name in BREEDINGS_SEMEN_ORDER})
        by_farm[farm] = {
            semen_type: int(counts.get(semen_type, 0)) for semen_type in BREEDINGS_SEMEN_ORDER
        }
        for semen_type in BREEDINGS_SEMEN_ORDER:
            total[semen_type] += by_farm[farm][semen_type]
    return {"by_farm": by_farm, "total": total, "month_count": month_count}


def _build_breedings_bundle(
    db: Session,
    *,
    selected_farms: list[str],
    effective_from: dt.date,
    effective_to: dt.date,
    selected_parity_groups: list[str] | None,
    fiscal_year: int | None,
    selected_semen_types: list[str] | None,
) -> tuple[
    dict[str, dict[str, int]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, dict[str, int]],
]:
    overrides = load_sire_overrides(db)
    records = _fetch_breeding_records(
        db,
        selected_farms=selected_farms,
        effective_from=effective_from,
        effective_to=effective_to,
        selected_parity_groups=selected_parity_groups,
        fiscal_year=fiscal_year,
    )

    allowed_semen = set(selected_semen_types or BREEDINGS_SEMEN_ORDER)

    farm_pivot: dict[str, dict[str, int]] = {}
    semen_table_pivot: dict[str, dict[str, dict[str, int]]] = {}
    semen_chart_pivot: dict[str, dict[str, int]] = {}
    semen_farm_totals = {
        farm: {name: 0 for name in BREEDINGS_SEMEN_ORDER} for farm in selected_farms
    }
    # Unfiltered counts for slicer visibility (independent of selected semen types).
    available_farm_totals = {
        farm: {name: 0 for name in BREEDINGS_SEMEN_ORDER} for farm in selected_farms
    }

    for month_label, farm, remark, event_date in records:
        semen_type = classify_semen_type(remark, overrides)
        if farm in available_farm_totals and semen_type in available_farm_totals[farm]:
            available_farm_totals[farm][semen_type] += 1
        if semen_type not in allowed_semen:
            continue

        farm_pivot.setdefault(month_label, _farm_zero_counts())
        if farm in farm_pivot[month_label]:
            farm_pivot[month_label][farm] += 1

        semen_table_pivot.setdefault(month_label, {})
        semen_table_pivot[month_label].setdefault(
            farm, {name: 0 for name in BREEDINGS_SEMEN_ORDER}
        )
        if semen_type in semen_table_pivot[month_label][farm]:
            semen_table_pivot[month_label][farm][semen_type] += 1
        if farm in semen_farm_totals and semen_type in semen_farm_totals[farm]:
            semen_farm_totals[farm][semen_type] += 1

        if semen_type in BREEDINGS_CHART_SEMEN_ORDER:
            semen_chart_pivot.setdefault(month_label, {name: 0 for name in BREEDINGS_CHART_SEMEN_ORDER})
            semen_chart_pivot[month_label][semen_type] += 1

    table_rows: list[dict[str, Any]] = []
    for month_start in _iter_month_starts(effective_from, effective_to):
        event_month = month_start.strftime("%b-%y")
        month_counts = semen_table_pivot.get(event_month, {})
        row: dict[str, Any] = {
            "event_month": event_month,
            "sort_key": _sort_key_from_date(month_start),
        }
        for farm in selected_farms:
            row[farm] = {
                semen_type: month_counts.get(farm, {}).get(semen_type, 0)
                for semen_type in BREEDINGS_SEMEN_ORDER
            }
        table_rows.append(row)

    chart_rows: list[dict[str, Any]] = []
    for month_start in _iter_month_starts(effective_from, effective_to):
        event_month = month_start.strftime("%b-%y")
        counts = semen_chart_pivot.get(event_month, {name: 0 for name in BREEDINGS_CHART_SEMEN_ORDER})
        beef = counts.get("beef", 0)
        dairy = counts.get("dairy", 0)
        unknown = counts.get("unknown", 0)
        chart_rows.append(
            {
                "event_month": event_month,
                "sort_key": _sort_key_from_date(month_start),
                "beef": beef,
                "dairy": dairy,
                "unknown": unknown,
                "total": beef + dairy + unknown,
            }
        )

    month_count = _month_count_inclusive(effective_from, effective_to)
    semen_summary = _build_breedings_semen_summary(semen_farm_totals, selected_farms, month_count)
    available_summary = _build_breedings_semen_summary(
        available_farm_totals, selected_farms, month_count
    )
    semen_summary["available"] = available_summary["total"]
    return farm_pivot, table_rows, chart_rows, semen_summary


def build_events_report(
    db: Session,
    *,
    event_types: tuple[str, ...],
    farms: list[str] | None = None,
    event_from: dt.date | None = None,
    event_to: dt.date | None = None,
    lact_groups: list[str] | None = None,
    parity_groups: list[str] | None = None,
    fiscal_year: int | None = None,
    include_sales_reason_breakdown: bool = False,
    include_breedings_semen_breakdown: bool = False,
    include_hooftrimming_breakdown: bool = False,
    semen_types: list[str] | None = None,
    lame_protocols: list[str] | None = None,
    use_disease_episode_counting: bool = False,
    apply_death_exclusions: bool = False,
    y_min: int | None = None,
    y_max: int | None = None,
    bounds_event_types: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)
    selected_lact_groups = normalize_lact_groups(lact_groups)
    selected_parity_groups = normalize_parity_groups(parity_groups)
    selected_semen_types = normalize_semen_types(semen_types) if include_breedings_semen_breakdown else None
    requested_lame_protocols = (
        normalize_lame_protocols(lame_protocols) if include_hooftrimming_breakdown else None
    )
    latest_import = db.scalar(select(func.max(CowEvent.import_timestamp)))
    # Date slider / axis use the page's full event set (not a single disease
    # filter), so switching to DA does not shrink the range to the last DA.
    bound_types = bounds_event_types or event_types

    empty_result: dict[str, Any] = {
        "rows": [],
        "grand_total": {**_farm_zero_counts(), "total": 0},
        "range_summary": _empty_range_summary(),
        "fiscal_year_options": [],
        "latest_import": latest_import.isoformat() if latest_import else None,
    }
    if include_sales_reason_breakdown:
        empty_result["sales_table_rows"] = []
    if include_breedings_semen_breakdown:
        empty_result["breedings_semen_rows"] = []
        empty_result["breedings_semen_chart_rows"] = []
        empty_result["breedings_semen_summary"] = _build_breedings_semen_summary(
            {farm: {name: 0 for name in BREEDINGS_SEMEN_ORDER} for farm in selected_farms},
            selected_farms,
            0,
        )
        empty_result["breedings_semen_summary"]["available"] = {
            name: 0 for name in BREEDINGS_SEMEN_ORDER
        }
    if include_hooftrimming_breakdown:
        empty_result["lame_rows"] = []
        empty_result["lame_protocol_rows"] = []
        empty_result["lame_dim_rows"] = []
        empty_result["available_protocols"] = []
    if use_disease_episode_counting:
        empty_result["disease_scatter"] = {
            "points": [],
            "y_axis_label": "DIM",
            "y_bounds": {"min": 0, "max": 0},
        }
        empty_result["disease_incidence"] = {
            "rows": [],
            "x_axis_label": "Calving month",
            "cohort_label": "Calvings",
            "summary": {
                "cohort_label": "Calvings",
                "total": {"incidence_pct": 0.0, "cohort_size": 0, "affected": 0},
            },
        }

    if not selected_farms:
        return empty_result

    fiscal_year_options = _get_fiscal_year_options(db, bound_types, selected_farms)
    empty_result["fiscal_year_options"] = fiscal_year_options

    bounds_min, bounds_max = _get_date_bounds(db, bound_types, selected_farms)
    if bounds_min is None or bounds_max is None:
        empty_result["date_bounds"] = None
        return empty_result

    if fiscal_year is not None:
        slider_min, slider_max = _fiscal_year_calendar_bounds(fiscal_year)
    else:
        slider_min, slider_max = bounds_min, bounds_max

    date_bounds = {
        "min": slider_min.isoformat(),
        "max": slider_max.isoformat(),
    }

    effective_from = event_from if event_from is not None else slider_min
    effective_to = event_to if event_to is not None else slider_max
    effective_from = _clamp_date(effective_from, slider_min, slider_max)
    effective_to = _clamp_date(effective_to, slider_min, slider_max)
    if effective_from > effective_to:
        effective_from, effective_to = effective_to, effective_from

    disease_scatter: dict[str, Any] | None = None
    disease_incidence: dict[str, Any] | None = None
    breedings_semen_summary: dict[str, Any] | None = None
    breedings_semen_rows: list[dict[str, Any]] | None = None
    breedings_semen_chart_rows: list[dict[str, Any]] | None = None
    lame_rows: list[dict[str, Any]] | None = None
    lame_protocol_rows: list[dict[str, Any]] | None = None
    lame_dim_rows: list[dict[str, Any]] | None = None
    available_protocols: list[str] | None = None
    if use_disease_episode_counting:
        pivot, disease_scatter, disease_incidence = _build_disease_episode_bundle(
            db,
            event_types=event_types,
            selected_farms=selected_farms,
            effective_from=effective_from,
            effective_to=effective_to,
            selected_parity_groups=selected_parity_groups,
            fiscal_year=fiscal_year,
            y_min=y_min,
            y_max=y_max,
        )
    elif include_breedings_semen_breakdown:
        pivot, breedings_semen_rows, breedings_semen_chart_rows, breedings_semen_summary = (
            _build_breedings_bundle(
                db,
                selected_farms=selected_farms,
                effective_from=effective_from,
                effective_to=effective_to,
                selected_parity_groups=selected_parity_groups,
                fiscal_year=fiscal_year,
                selected_semen_types=selected_semen_types,
            )
        )
    elif include_hooftrimming_breakdown:
        (
            pivot,
            lame_rows,
            lame_protocol_rows,
            lame_dim_rows,
            available_protocols,
        ) = _build_hooftrimming_bundle(
            db,
            selected_farms=selected_farms,
            effective_from=effective_from,
            effective_to=effective_to,
            fiscal_year=fiscal_year,
            requested_protocols=requested_lame_protocols,
        )
    elif apply_death_exclusions:
        pivot = _build_deaths_pivot(
            db,
            selected_farms=selected_farms,
            effective_from=effective_from,
            effective_to=effective_to,
            selected_parity_groups=selected_parity_groups,
            fiscal_year=fiscal_year,
        )
    else:
        pivot = _build_standard_event_pivot(
            db,
            event_types=event_types,
            selected_farms=selected_farms,
            effective_from=effective_from,
            effective_to=effective_to,
            selected_lact_groups=selected_lact_groups,
            selected_parity_groups=selected_parity_groups,
            fiscal_year=fiscal_year,
            split_beef=include_sales_reason_breakdown,
            sales_classified=include_sales_reason_breakdown,
        )

    rows = _zero_fill_rows(pivot, effective_from, effective_to)

    farm_totals = {
        code: sum(int(row.get(code, 0) or 0) for row in rows) for code in HERD_FARM_OPTIONS
    }
    grand_total = sum(farm_totals.values())
    month_count = _month_count_inclusive(effective_from, effective_to)

    result: dict[str, Any] = {
        "rows": rows,
        "grand_total": {**farm_totals, "total": grand_total},
        "date_bounds": date_bounds,
        "range_summary": _build_range_summary(farm_totals, month_count),
        "fiscal_year_options": fiscal_year_options,
        "latest_import": latest_import.isoformat() if latest_import else None,
    }
    if use_disease_episode_counting:
        result["counting_mode"] = "disease_episodes"
        result["disease_episode_gaps"] = {
            event: _disease_episode_gap_days(event) for event in event_types
        }
        result["disease_scatter"] = disease_scatter or {
            "points": [],
            "y_axis_label": "DIM",
            "y_bounds": {"min": 0, "max": 0},
        }
        result["disease_incidence"] = disease_incidence or {
            "rows": [],
            "x_axis_label": "Calving month",
            "cohort_label": "Calvings",
            "summary": {
                "cohort_label": "Calvings",
                "total": {"incidence_pct": 0.0, "cohort_size": 0, "affected": 0},
            },
        }
    if include_sales_reason_breakdown:
        result["sales_table_rows"] = _build_sales_table_rows(
            db,
            selected_farms=selected_farms,
            effective_from=effective_from,
            effective_to=effective_to,
            selected_parity_groups=selected_parity_groups,
            fiscal_year=fiscal_year,
        )
        result["sales_table_reasons"] = list(SALES_TABLE_REASON_ORDER)
    if include_breedings_semen_breakdown:
        result["breedings_semen_rows"] = breedings_semen_rows or []
        result["breedings_semen_chart_rows"] = breedings_semen_chart_rows or []
        result["breedings_semen_summary"] = breedings_semen_summary or _build_breedings_semen_summary(
            {farm: {name: 0 for name in BREEDINGS_SEMEN_ORDER} for farm in selected_farms},
            selected_farms,
            month_count,
        )
        if "available" not in result["breedings_semen_summary"]:
            result["breedings_semen_summary"]["available"] = {
                name: 0 for name in BREEDINGS_SEMEN_ORDER
            }
        result["breedings_semen_types"] = list(BREEDINGS_SEMEN_ORDER)
    if include_hooftrimming_breakdown:
        result["lame_rows"] = lame_rows or []
        result["lame_protocol_rows"] = lame_protocol_rows or []
        result["lame_dim_rows"] = lame_dim_rows or []
        result["available_protocols"] = available_protocols or []
    return result


def build_events_page_report(
    db: Session,
    *,
    page_slug: str,
    farms: list[str] | None = None,
    event_from: dt.date | None = None,
    event_to: dt.date | None = None,
    lact_groups: list[str] | None = None,
    parity_groups: list[str] | None = None,
    fiscal_year: int | None = None,
    disease: str | None = None,
    semen_types: list[str] | None = None,
    lame_protocols: list[str] | None = None,
    y_min: int | None = None,
    y_max: int | None = None,
) -> dict[str, Any]:
    event_types = resolve_page_event_types(page_slug, disease)
    page_event_types = EVENT_PAGE_TYPES.get(page_slug, event_types)
    # Sales slider/bounds also need DIED+TB/OFS (remark or DEST) rows kept as DIED.
    if page_slug == "sales":
        page_event_types = ("SOLD", "DIED")
    return build_events_report(
        db,
        event_types=event_types,
        farms=farms,
        event_from=event_from,
        event_to=event_to,
        lact_groups=lact_groups if page_slug == "calvings" else None,
        parity_groups=parity_groups if page_slug in PAGES_WITH_PARITY_FILTER else None,
        fiscal_year=fiscal_year,
        include_sales_reason_breakdown=page_slug == "sales",
        include_breedings_semen_breakdown=page_slug == "breedings",
        include_hooftrimming_breakdown=page_slug == "hooftrimming",
        semen_types=semen_types if page_slug == "breedings" else None,
        lame_protocols=lame_protocols if page_slug == "hooftrimming" else None,
        use_disease_episode_counting=page_slug == "disease",
        apply_death_exclusions=page_slug == "deaths",
        y_min=y_min if page_slug == "disease" else None,
        y_max=y_max if page_slug == "disease" else None,
        bounds_event_types=page_event_types,
    )


def _semen_status_band(value: float, average: float) -> str:
    if average <= 0:
        return "unknown" if value <= 0 else "green"
    ratio = value / average
    if ratio >= 1.0:
        return "green"
    if ratio >= 0.9:
        return "yellow"
    return "red"


def build_dairy_semen_30d(db: Session) -> dict[str, Any]:
    """Home widget: dairy BRED events in the last 30 days vs 120-day average."""
    today = dt.date.today()
    start_30 = today - dt.timedelta(days=30)
    start_120 = today - dt.timedelta(days=120)
    overrides = load_sire_overrides(db)
    rows = db.execute(
        select(CowEvent.remark, CowEvent.event_date)
        .where(CowEvent.event == "BRED")
        .where(CowEvent.event_date.isnot(None))
        .where(CowEvent.event_date >= start_120)
        .where(CowEvent.event_date <= today)
    ).all()

    count_30 = 0
    count_120 = 0
    for remark, event_date in rows:
        if classify_semen_type(remark, overrides) != "dairy":
            continue
        if hasattr(event_date, "date"):
            event_date = event_date.date()
        count_120 += 1
        if event_date >= start_30:
            count_30 += 1

    avg_30d_120 = count_120 / 4.0
    status = _semen_status_band(count_30, avg_30d_120)
    return {
        "count": count_30,
        "avg_30d_120": round(avg_30d_120, 1),
        "status": status,
        "avg_status": status,
        "href": "/events/breedings?semen=dairy",
    }
