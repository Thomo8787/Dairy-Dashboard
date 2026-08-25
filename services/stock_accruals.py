"""Monthly stock movement report for Stock Inventory accruals."""

from __future__ import annotations

import datetime as dt
import gc
from typing import Any

from sqlalchemy import and_, delete, extract, func, or_, select
from sqlalchemy.orm import Session

from services.database import (
    STOCK_GROUP_BEEF,
    STOCK_GROUP_CATEGORY,
    STOCK_GROUP_COWS,
    STOCK_GROUP_OPTIONS,
    STOCK_GROUP_YOUNGSTOCK,
    CowEvent,
    HerdBirth,
    HerdInventory,
    StockAccrualSnapshot,
    StockOpeningBaseline,
    StockPurchaseAnimal,
)
from services.events_common import (
    SALES_TABLE_REASON_ORDER,
    _fiscal_year_calendar_bounds,
    _iter_month_starts,
    _sales_reason_expression,
    death_report_event_clause,
    normalize_farms,
    sales_classified_event_clause,
)
from services.farms import HERD_FARM_OPTIONS
from services.herd_import_utils import BEEF_CBREED_MIN, CATEGORY_BEEF, CATEGORY_DAIRY
from services.stock_group import normalize_stock_group
from services.stock_purchase_derivation import ensure_stock_purchases

_ZERO_SALES = {reason: 0 for reason in SALES_TABLE_REASON_ORDER}
_BASELINE_FISCAL_YEARS = 3


def _month_start(value: dt.date) -> dt.date:
    return value.replace(day=1)


def _month_key(value: dt.date) -> tuple[int, int]:
    return (value.year, value.month)


def _last_day_of_month(value: dt.date) -> dt.date:
    if value.month == 12:
        return dt.date(value.year, 12, 31)
    return dt.date(value.year, value.month + 1, 1) - dt.timedelta(days=1)


def _youngstock_cow_event_filter():
    return (
        CowEvent.lact == 0,
        func.upper(func.coalesce(CowEvent.gndr, "")) == "F",
        CowEvent.cbrd.isnot(None),
        CowEvent.cbrd < BEEF_CBREED_MIN,
    )


def _apply_cow_event_stock_group(query, stock_group: str):
    if stock_group == STOCK_GROUP_COWS:
        return query.where(CowEvent.lact.isnot(None)).where(CowEvent.lact > 0)
    if stock_group == STOCK_GROUP_BEEF:
        query = query.where(CowEvent.lact == 0)
        return query.where(
            or_(
                func.upper(func.coalesce(CowEvent.gndr, "")) != "F",
                CowEvent.cbrd.is_(None),
                CowEvent.cbrd >= BEEF_CBREED_MIN,
            )
        )
    for condition in _youngstock_cow_event_filter():
        query = query.where(condition)
    return query


def _fetch_sales_by_month(
    db: Session,
    *,
    farm: str,
    stock_group: str,
    month_from: dt.date,
    month_to: dt.date,
) -> dict[tuple[int, int], dict[str, int]]:
    reason_expr = _sales_reason_expression()
    query = (
        select(
            extract("year", CowEvent.event_date),
            extract("month", CowEvent.event_date),
            reason_expr.label("reason"),
            func.count(),
        )
        .where(sales_classified_event_clause())
        .where(CowEvent.event_date.isnot(None))
        .where(CowEvent.farm == farm)
        .where(CowEvent.event_date >= month_from)
        .where(CowEvent.event_date <= month_to)
    )
    query = _apply_cow_event_stock_group(query, stock_group)
    query = query.group_by(
        extract("year", CowEvent.event_date),
        extract("month", CowEvent.event_date),
        reason_expr,
    )

    pivot: dict[tuple[int, int], dict[str, int]] = {}
    for year, month, reason, count in db.execute(query).all():
        if year is None or month is None or reason is None:
            continue
        key = (int(year), int(month))
        pivot.setdefault(key, dict(_ZERO_SALES))
        reason_key = str(reason)
        if reason_key in pivot[key]:
            pivot[key][reason_key] = int(count)
    return pivot


def _fetch_event_count_by_month(
    db: Session,
    *,
    farm: str,
    stock_group: str,
    event_type: str,
    month_from: dt.date,
    month_to: dt.date,
    lact_filter: str | None = None,
) -> dict[tuple[int, int], int]:
    query = (
        select(
            extract("year", CowEvent.event_date),
            extract("month", CowEvent.event_date),
            func.count(),
        )
        .where(CowEvent.event_date.isnot(None))
        .where(CowEvent.farm == farm)
        .where(CowEvent.event_date >= month_from)
        .where(CowEvent.event_date <= month_to)
    )
    # DIED+TB/OFS are treated as sales; exclude them from deaths so accruals
    # do not subtract the same exit twice.
    if event_type == "DIED":
        query = query.where(death_report_event_clause())
    else:
        query = query.where(CowEvent.event == event_type)
    if lact_filter == "fresh_heifers":
        query = query.where(CowEvent.lact == 1)
        query = query.where(func.upper(func.coalesce(CowEvent.gndr, "")) == "F")
        query = query.where(CowEvent.cbrd.isnot(None))
        query = query.where(CowEvent.cbrd < BEEF_CBREED_MIN)
    elif lact_filter == "fresh_cows":
        query = query.where(CowEvent.lact == 1)
    else:
        query = _apply_cow_event_stock_group(query, stock_group)

    query = query.group_by(
        extract("year", CowEvent.event_date),
        extract("month", CowEvent.event_date),
    )

    return {
        (int(year), int(month)): int(count)
        for year, month, count in db.execute(query).all()
        if year is not None and month is not None
    }


def _fetch_births_by_month(
    db: Session,
    *,
    farm: str,
    month_from: dt.date,
    month_to: dt.date,
) -> dict[tuple[int, int], int]:
    query = (
        select(
            extract("year", HerdBirth.bdat),
            extract("month", HerdBirth.bdat),
            func.count(),
        )
        .where(HerdBirth.bdat.isnot(None))
        .where(HerdBirth.farm == farm)
        .where(
            or_(
                HerdBirth.category == CATEGORY_DAIRY,
                and_(HerdBirth.category.is_(None), HerdBirth.cbrd < BEEF_CBREED_MIN),
            )
        )
        .where(func.upper(func.coalesce(HerdBirth.gndr, "")) == "F")
        .where(HerdBirth.bdat >= month_from)
        .where(HerdBirth.bdat <= month_to)
        .group_by(
            extract("year", HerdBirth.bdat),
            extract("month", HerdBirth.bdat),
        )
    )
    return {
        (int(year), int(month)): int(count)
        for year, month, count in db.execute(query).all()
        if year is not None and month is not None
    }


def _fetch_beef_births_by_month(
    db: Session,
    *,
    farm: str,
    month_from: dt.date,
    month_to: dt.date,
) -> dict[tuple[int, int], int]:
    query = (
        select(
            extract("year", HerdBirth.bdat),
            extract("month", HerdBirth.bdat),
            func.count(),
        )
        .where(HerdBirth.bdat.isnot(None))
        .where(HerdBirth.farm == farm)
        .where(
            or_(
                HerdBirth.category == CATEGORY_BEEF,
                and_(
                    HerdBirth.category.is_(None),
                    or_(
                        func.upper(func.coalesce(HerdBirth.gndr, "")) != "F",
                        HerdBirth.cbrd.is_(None),
                        HerdBirth.cbrd >= BEEF_CBREED_MIN,
                    ),
                ),
            )
        )
        .where(HerdBirth.bdat >= month_from)
        .where(HerdBirth.bdat <= month_to)
        .group_by(
            extract("year", HerdBirth.bdat),
            extract("month", HerdBirth.bdat),
        )
    )
    return {
        (int(year), int(month)): int(count)
        for year, month, count in db.execute(query).all()
        if year is not None and month is not None
    }


def _fetch_purchases_by_month(
    db: Session,
    *,
    farm: str,
    stock_group: str,
    month_from: dt.date,
    month_to: dt.date,
) -> dict[tuple[int, int], int]:
    rows = db.execute(
        select(
            extract("year", StockPurchaseAnimal.edat),
            extract("month", StockPurchaseAnimal.edat),
            func.count(),
        )
        .where(StockPurchaseAnimal.farm == farm)
        .where(StockPurchaseAnimal.stock_group == stock_group)
        .where(StockPurchaseAnimal.edat >= month_from)
        .where(StockPurchaseAnimal.edat <= month_to)
        .group_by(
            extract("year", StockPurchaseAnimal.edat),
            extract("month", StockPurchaseAnimal.edat),
        )
    ).all()
    return {
        (int(year), int(month)): int(count)
        for year, month, count in rows
        if year is not None and month is not None
    }


def _get_baseline(
    db: Session,
    farm: str,
    stock_group: str,
) -> StockOpeningBaseline | None:
    return db.scalar(
        select(StockOpeningBaseline).where(
            StockOpeningBaseline.farm == farm,
            StockOpeningBaseline.stock_group == stock_group,
        )
    )


def _sum_month_counts(counts: dict[tuple[int, int], int]) -> int:
    return sum(counts.values())


def _movement_totals(
    db: Session,
    *,
    farm: str,
    stock_group: str,
    month_from: dt.date,
    month_to: dt.date,
) -> dict[str, int]:
    sales = _fetch_sales_by_month(
        db, farm=farm, stock_group=stock_group, month_from=month_from, month_to=month_to
    )
    sales_total = sum(sum(month.values()) for month in sales.values())
    deaths = _sum_month_counts(
        _fetch_event_count_by_month(
            db,
            farm=farm,
            stock_group=stock_group,
            event_type="DIED",
            month_from=month_from,
            month_to=month_to,
        )
    )
    purchases = _sum_month_counts(
        _fetch_purchases_by_month(
            db,
            farm=farm,
            stock_group=stock_group,
            month_from=month_from,
            month_to=month_to,
        )
    )
    births = 0
    calvings = 0
    if stock_group == STOCK_GROUP_YOUNGSTOCK:
        births = _sum_month_counts(
            _fetch_births_by_month(db, farm=farm, month_from=month_from, month_to=month_to)
        )
        calvings = -_sum_month_counts(
            _fetch_event_count_by_month(
                db,
                farm=farm,
                stock_group=stock_group,
                event_type="FRESH",
                month_from=month_from,
                month_to=month_to,
                lact_filter="fresh_heifers",
            )
        )
    elif stock_group == STOCK_GROUP_BEEF:
        births = _sum_month_counts(
            _fetch_beef_births_by_month(
                db, farm=farm, month_from=month_from, month_to=month_to
            )
        )
    elif stock_group == STOCK_GROUP_COWS:
        calvings = _sum_month_counts(
            _fetch_event_count_by_month(
                db,
                farm=farm,
                stock_group=stock_group,
                event_type="FRESH",
                month_from=month_from,
                month_to=month_to,
                lact_filter="fresh_cows",
            )
        )
    return {
        "sales_total": sales_total,
        "deaths": deaths,
        "births": births,
        "calvings": calvings,
        "purchases": purchases,
    }


def _inventory_count(db: Session, farm: str, stock_group: str) -> int:
    category = STOCK_GROUP_CATEGORY[stock_group]
    return (
        db.scalar(
            select(func.count())
            .select_from(HerdInventory)
            .where(HerdInventory.farm == farm, HerdInventory.category == category)
        )
        or 0
    )


def _baseline_month_for_farm(db: Session, farm: str) -> dt.date | None:
    min_event = db.scalar(select(func.min(CowEvent.event_date)).where(CowEvent.farm == farm))
    min_birth = db.scalar(select(func.min(HerdBirth.bdat)).where(HerdBirth.farm == farm))
    dates = [value for value in (min_event, min_birth) if value is not None]
    today = dt.date.today()
    if not dates:
        has_inventory = db.scalar(
            select(HerdInventory.id).where(HerdInventory.farm == farm).limit(1)
        )
        return _month_start(today) if has_inventory is not None else None
    earliest = _month_start(min(dates))
    fiscal_year = today.year + 1 if today.month >= 4 else today.year
    cap = dt.date(fiscal_year - _BASELINE_FISCAL_YEARS, 4, 1)
    return max(earliest, cap)


def ensure_stock_opening_baselines(db: Session) -> dict[str, Any]:
    """Seed missing openings by reverse-rolling current inventory through events."""
    today = dt.date.today()
    created = 0
    for farm in HERD_FARM_OPTIONS:
        baseline_month = _baseline_month_for_farm(db, farm)
        if baseline_month is None:
            continue
        for stock_group in STOCK_GROUP_OPTIONS:
            existing = _get_baseline(db, farm, stock_group)
            if existing is not None:
                continue
            inventory = _inventory_count(db, farm, stock_group)
            movements = _movement_totals(
                db,
                farm=farm,
                stock_group=stock_group,
                month_from=baseline_month,
                month_to=today,
            )
            net = (
                -movements["sales_total"]
                - movements["deaths"]
                + movements["births"]
                + movements["calvings"]
                + movements["purchases"]
            )
            db.add(
                StockOpeningBaseline(
                    farm=farm,
                    stock_group=stock_group,
                    month_start=baseline_month,
                    opening_count=inventory - net,
                )
            )
            created += 1
    if created:
        db.flush()
    return {"created": created}


def _compute_farm_rows(
    db: Session,
    *,
    farm: str,
    stock_group: str,
    display_from: dt.date,
    display_to: dt.date,
) -> list[dict[str, Any]]:
    baseline = _get_baseline(db, farm, stock_group)
    if baseline is None:
        return []

    baseline_month = _month_start(baseline.month_start)
    end_month = _month_start(display_to)
    if end_month < baseline_month:
        return []

    calc_end = _last_day_of_month(end_month)
    sales = _fetch_sales_by_month(
        db, farm=farm, stock_group=stock_group, month_from=baseline_month, month_to=calc_end
    )
    deaths = _fetch_event_count_by_month(
        db,
        farm=farm,
        stock_group=stock_group,
        event_type="DIED",
        month_from=baseline_month,
        month_to=calc_end,
    )
    purchases = _fetch_purchases_by_month(
        db,
        farm=farm,
        stock_group=stock_group,
        month_from=baseline_month,
        month_to=calc_end,
    )

    births: dict[tuple[int, int], int] = {}
    calvings: dict[tuple[int, int], int] = {}
    if stock_group == STOCK_GROUP_YOUNGSTOCK:
        births = _fetch_births_by_month(
            db, farm=farm, month_from=baseline_month, month_to=calc_end
        )
        calvings = _fetch_event_count_by_month(
            db,
            farm=farm,
            stock_group=stock_group,
            event_type="FRESH",
            month_from=baseline_month,
            month_to=calc_end,
            lact_filter="fresh_heifers",
        )
    elif stock_group == STOCK_GROUP_BEEF:
        births = _fetch_beef_births_by_month(
            db, farm=farm, month_from=baseline_month, month_to=calc_end
        )
    elif stock_group == STOCK_GROUP_COWS:
        calvings = _fetch_event_count_by_month(
            db,
            farm=farm,
            stock_group=stock_group,
            event_type="FRESH",
            month_from=baseline_month,
            month_to=calc_end,
            lact_filter="fresh_cows",
        )

    all_months = _iter_month_starts(baseline_month, end_month)
    opening = baseline.opening_count
    computed: list[dict[str, Any]] = []

    for month_start in all_months:
        key = _month_key(month_start)
        month_sales = sales.get(key, dict(_ZERO_SALES))
        sales_total = sum(month_sales.values())
        month_deaths = deaths.get(key, 0)
        show_births = stock_group in (STOCK_GROUP_YOUNGSTOCK, STOCK_GROUP_BEEF)
        month_births = births.get(key, 0) if show_births else 0
        raw_calvings = calvings.get(key, 0)
        if stock_group == STOCK_GROUP_COWS:
            month_calvings = raw_calvings
        elif stock_group == STOCK_GROUP_YOUNGSTOCK:
            month_calvings = -raw_calvings
        else:
            month_calvings = 0
        month_purchases = purchases.get(key, 0)

        closing = (
            opening
            - sales_total
            - month_deaths
            + month_births
            + month_calvings
            + month_purchases
        )

        computed.append(
            {
                "month_start": month_start.isoformat(),
                "event_month": month_start.strftime("%b-%y"),
                "opening": opening,
                "sales": dict(month_sales),
                "sales_total": sales_total,
                "deaths": month_deaths,
                "births": month_births,
                "calvings": month_calvings,
                "purchases": month_purchases,
                "closing": closing,
                "warning": closing < 0 or opening < 0,
            }
        )
        opening = closing

    display_from_month = _month_start(display_from)
    display_to_month = _month_start(display_to)
    return [
        row
        for row in computed
        if display_from_month <= dt.date.fromisoformat(row["month_start"]) <= display_to_month
    ]


def _merge_farm_rows(farm_rows: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    if not farm_rows:
        return []
    if len(farm_rows) == 1:
        return farm_rows[0]

    by_month: dict[str, dict[str, Any]] = {}
    for rows in farm_rows:
        for row in rows:
            month = row["month_start"]
            if month not in by_month:
                by_month[month] = {
                    "month_start": month,
                    "event_month": row["event_month"],
                    "opening": 0,
                    "sales": dict(_ZERO_SALES),
                    "sales_total": 0,
                    "deaths": 0,
                    "births": 0,
                    "calvings": 0,
                    "purchases": 0,
                    "closing": 0,
                    "warning": False,
                }
            merged = by_month[month]
            merged["opening"] += row["opening"]
            merged["sales_total"] += row["sales_total"]
            merged["deaths"] += row["deaths"]
            merged["births"] += row["births"]
            merged["calvings"] += row["calvings"]
            merged["purchases"] += row["purchases"]
            merged["closing"] += row["closing"]
            merged["warning"] = merged["warning"] or row["warning"]
            for reason in SALES_TABLE_REASON_ORDER:
                merged["sales"][reason] += row["sales"].get(reason, 0)

    return sorted(by_month.values(), key=lambda row: row["month_start"])


def _accrual_anchor_ts(db: Session) -> dt.datetime | None:
    return db.scalar(select(func.max(HerdInventory.import_timestamp)))


def _fiscal_year_options_for_farms(db: Session, farms: list[str]) -> list[int]:
    return sorted(
        {
            int(value)
            for value in db.scalars(
                select(CowEvent.fiscal_year)
                .where(CowEvent.fiscal_year.isnot(None))
                .where(CowEvent.farm.in_(farms))
                .distinct()
            ).all()
            if value is not None
        },
        reverse=True,
    )


def _accrual_bounds_for_farms(
    db: Session, farms: list[str], baselines: list[StockOpeningBaseline]
) -> tuple[dt.date, dt.date]:
    bounds_min = min(_month_start(b.month_start) for b in baselines)
    bounds_max = dt.date.today().replace(day=1)

    latest_event = db.scalar(
        select(func.max(CowEvent.event_date)).where(CowEvent.farm.in_(farms))
    )
    latest_birth = db.scalar(select(func.max(HerdBirth.bdat)).where(HerdBirth.farm.in_(farms)))
    for candidate in (latest_event, latest_birth):
        if candidate is not None:
            candidate_month = _month_start(candidate)
            if candidate_month > bounds_max:
                bounds_max = candidate_month
    return bounds_min, bounds_max


def _snapshot_from_row(snapshot: StockAccrualSnapshot) -> dict[str, Any]:
    sales = dict(_ZERO_SALES)
    for reason, count in (snapshot.sales or {}).items():
        if reason in sales:
            sales[reason] = int(count)
    month_start = snapshot.month_start
    return {
        "month_start": month_start.isoformat(),
        "event_month": month_start.strftime("%b-%y"),
        "opening": int(snapshot.opening_count),
        "sales": sales,
        "sales_total": int(snapshot.sales_total),
        "deaths": int(snapshot.deaths),
        "births": int(snapshot.births),
        "calvings": int(snapshot.calvings),
        "purchases": int(snapshot.purchases),
        "closing": int(snapshot.closing_count),
        "warning": bool(snapshot.warning),
    }


def _snapshot_from_farm_month(
    *,
    anchor_ts: dt.datetime,
    farm: str,
    stock_group: str,
    row: dict[str, Any],
) -> StockAccrualSnapshot:
    sales = {reason: int(row["sales"].get(reason, 0)) for reason in SALES_TABLE_REASON_ORDER}
    return StockAccrualSnapshot(
        anchor_import_timestamp=anchor_ts,
        farm=farm,
        stock_group=stock_group,
        month_start=dt.date.fromisoformat(row["month_start"]),
        opening_count=int(row["opening"]),
        sales=sales,
        sales_total=int(row["sales_total"]),
        deaths=int(row["deaths"]),
        births=int(row["births"]),
        calvings=int(row["calvings"]),
        purchases=int(row["purchases"]),
        closing_count=int(row["closing"]),
        warning=bool(row["warning"]),
    )


def _accrual_snapshot_has_data(db: Session, anchor_ts: dt.datetime) -> bool:
    try:
        return (
            db.scalar(
                select(func.count())
                .select_from(StockAccrualSnapshot)
                .where(StockAccrualSnapshot.anchor_import_timestamp == anchor_ts)
            )
            or 0
        ) > 0
    except Exception:
        db.rollback()
        return False


def _rows_from_snapshots(
    db: Session,
    *,
    anchor_ts: dt.datetime,
    selected_farms: list[str],
    stock_group: str,
    effective_from: dt.date,
    effective_to: dt.date,
) -> list[dict[str, Any]]:
    snapshots = db.scalars(
        select(StockAccrualSnapshot)
        .where(StockAccrualSnapshot.anchor_import_timestamp == anchor_ts)
        .where(StockAccrualSnapshot.farm.in_(selected_farms))
        .where(StockAccrualSnapshot.stock_group == stock_group)
        .where(StockAccrualSnapshot.month_start >= effective_from)
        .where(StockAccrualSnapshot.month_start <= effective_to)
        .order_by(StockAccrualSnapshot.farm.asc(), StockAccrualSnapshot.month_start.asc())
    ).all()

    by_farm: dict[str, list[dict[str, Any]]] = {farm: [] for farm in selected_farms}
    for snapshot in snapshots:
        by_farm.setdefault(snapshot.farm, []).append(_snapshot_from_row(snapshot))

    return _merge_farm_rows([by_farm[farm] for farm in selected_farms])


def rebuild_stock_accrual_snapshots(db: Session) -> dict[str, Any]:
    """Recompute and persist stock accrual rows for all farms and stock groups."""
    ensure_stock_purchases(db)
    ensure_stock_opening_baselines(db)

    anchor_ts = _accrual_anchor_ts(db)
    if anchor_ts is None:
        db.execute(delete(StockAccrualSnapshot))
        db.flush()
        return {"anchor_import_timestamp": None, "rows_written": 0}

    farms = list(HERD_FARM_OPTIONS)
    bounds_max = dt.date.today().replace(day=1)
    latest_event = db.scalar(select(func.max(CowEvent.event_date)))
    latest_birth = db.scalar(select(func.max(HerdBirth.bdat)))
    for candidate in (latest_event, latest_birth):
        if candidate is not None:
            candidate_month = _month_start(candidate)
            if candidate_month > bounds_max:
                bounds_max = candidate_month

    rows_written = 0
    for farm in farms:
        for stock_group in STOCK_GROUP_OPTIONS:
            baseline = _get_baseline(db, farm, stock_group)
            if baseline is None:
                db.execute(
                    delete(StockAccrualSnapshot).where(
                        StockAccrualSnapshot.farm == farm,
                        StockAccrualSnapshot.stock_group == stock_group,
                    )
                )
                db.flush()
                continue
            baseline_month = _month_start(baseline.month_start)
            farm_rows = _compute_farm_rows(
                db,
                farm=farm,
                stock_group=stock_group,
                display_from=baseline_month,
                display_to=bounds_max,
            )
            db.execute(
                delete(StockAccrualSnapshot).where(
                    StockAccrualSnapshot.farm == farm,
                    StockAccrualSnapshot.stock_group == stock_group,
                )
            )
            for row in farm_rows:
                db.add(
                    _snapshot_from_farm_month(
                        anchor_ts=anchor_ts,
                        farm=farm,
                        stock_group=stock_group,
                        row=row,
                    )
                )
                rows_written += 1
            db.flush()
            db.expire_all()
            gc.collect()

    db.execute(
        delete(StockAccrualSnapshot).where(
            StockAccrualSnapshot.anchor_import_timestamp != anchor_ts
        )
    )
    db.flush()
    gc.collect()
    return {
        "anchor_import_timestamp": anchor_ts.isoformat(timespec="seconds"),
        "rows_written": rows_written,
    }


def _empty_report(stock_group: str, fiscal_year: int | None) -> dict[str, Any]:
    return {
        "rows": [],
        "sales_reasons": list(SALES_TABLE_REASON_ORDER),
        "stock_group": stock_group,
        "date_bounds": None,
        "fiscal_year_options": [],
        "selected_fiscal_year": fiscal_year,
        "from_snapshot": False,
    }


def build_stock_accruals_report(
    db: Session,
    *,
    farms: list[str] | None = None,
    stock_group: str | None = None,
    month_from: dt.date | None = None,
    month_to: dt.date | None = None,
    fiscal_year: int | None = None,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)
    group = normalize_stock_group(stock_group)

    if not selected_farms:
        return _empty_report(group, fiscal_year)

    ensure_stock_purchases(db)
    ensure_stock_opening_baselines(db)

    baselines = list(
        db.scalars(
            select(StockOpeningBaseline).where(
                StockOpeningBaseline.farm.in_(selected_farms),
                StockOpeningBaseline.stock_group == group,
            )
        ).all()
    )
    if not baselines:
        return _empty_report(group, fiscal_year)

    fiscal_year_options = _fiscal_year_options_for_farms(db, selected_farms)
    bounds_min, bounds_max = _accrual_bounds_for_farms(db, selected_farms, baselines)

    if fiscal_year is not None:
        slider_min, slider_max = _fiscal_year_calendar_bounds(fiscal_year)
        slider_min = _month_start(slider_min)
        slider_max = _month_start(slider_max)
    else:
        slider_min, slider_max = bounds_min, bounds_max

    effective_from = month_from if month_from is not None else slider_min
    effective_to = month_to if month_to is not None else slider_max
    effective_from = max(_month_start(effective_from), slider_min, bounds_min)
    effective_to = min(_month_start(effective_to), slider_max, bounds_max)
    if effective_from > effective_to:
        effective_from, effective_to = effective_to, effective_from

    anchor_ts = _accrual_anchor_ts(db)
    from_snapshot = False
    rows = None
    if anchor_ts is not None and _accrual_snapshot_has_data(db, anchor_ts):
        try:
            rows = _rows_from_snapshots(
                db,
                anchor_ts=anchor_ts,
                selected_farms=selected_farms,
                stock_group=group,
                effective_from=effective_from,
                effective_to=effective_to,
            )
            from_snapshot = True
        except Exception:
            db.rollback()
            rows = None

    if rows is None:
        per_farm = [
            _compute_farm_rows(
                db,
                farm=farm,
                stock_group=group,
                display_from=effective_from,
                display_to=effective_to,
            )
            for farm in selected_farms
        ]
        rows = _merge_farm_rows(per_farm)

    return {
        "rows": rows,
        "sales_reasons": list(SALES_TABLE_REASON_ORDER),
        "stock_group": group,
        "date_bounds": {
            "min": max(slider_min, bounds_min).isoformat(),
            "max": _last_day_of_month(min(slider_max, bounds_max)).isoformat(),
        },
        "fiscal_year_options": fiscal_year_options,
        "selected_fiscal_year": fiscal_year,
        "from_snapshot": from_snapshot,
    }
