"""Cattle sales listing with herd event linkage."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from services.cattle_sale_pdf import is_rejected_sale, normalize_etag
from services.database import STOCK_GROUP_CATEGORY, CattleSaleLine, CowEvent
from services.events_common import normalize_farms, sales_classified_event_clause
from services.farms import HERD_FARM_OPTIONS
from services.stock_group import stock_group_from_event_fields

EVENT_MATCH_WINDOW_DAYS = 14
# DairyComp JV exits on remittances (Game Changer / Pathway) instead of SOLD.
# PATHWAY is the historical JV event name; PATH is also accepted.
CATTLE_SALES_JV_EXIT_EVENTS: tuple[str, ...] = ("GAME", "PATH", "PATHWAY")
CATTLE_CATEGORIES: tuple[str, ...] = ("Dairy", "Youngstock", "Beef")
CATTLE_GENDERS: tuple[str, ...] = ("Male", "Female")
AGE_MIN_DAYS_DEFAULT = 0
AGE_MAX_DAYS_DEFAULT = 9999
BUYER_EUROFARM = "Euro Farm Wales"
BUYER_PATHWAY = "Pathway"
BUYER_BUITELAAR = "Buitelaar"
BUYER_GAME_CHANGER = "Game Changer"
BUYER_GAMECHANGER = "Gamechanger"
BUYER_PICKSTOCK = "Pickstock"
BUYER_NEILDS = "Neilds"
BUYER_MARKET_DRAYTON = "Market Drayton"
BUYER_WARRENDALE = "Warrendale"
KNOWN_BUYERS: tuple[str, ...] = (
    BUYER_NEILDS,
    BUYER_PICKSTOCK,
    BUYER_GAMECHANGER,
    BUYER_GAME_CHANGER,
    BUYER_MARKET_DRAYTON,
    BUYER_WARRENDALE,
    BUYER_EUROFARM,
    BUYER_PATHWAY,
    BUYER_BUITELAAR,
)


def cattle_sales_exit_event_clause():
    """Sale-exit events for remittance matching and the sales-payments queue."""
    return or_(
        sales_classified_event_clause(),
        CowEvent.event.in_(list(CATTLE_SALES_JV_EXIT_EVENTS)),
    )


def infer_cattle_sale_buyer(
    *,
    buyer: str | None = None,
    source_file: str | None = None,
) -> str | None:
    """Resolve display buyer from stored value or remittance filename."""
    if buyer and buyer.strip():
        return buyer.strip()
    name = (source_file or "").lower()
    if not name:
        return None
    compact = name.replace(" ", "").replace("_", "")
    if "neild" in name:
        return BUYER_NEILDS
    if "drayton" in name or "barber" in name:
        return BUYER_MARKET_DRAYTON
    if "warrendale" in name:
        return BUYER_WARRENDALE
    if "pathway" in name or name.startswith("pwa") or " pwa" in name:
        return BUYER_PATHWAY
    if "buitelaar" in name or "vendbill" in name:
        return BUYER_BUITELAAR
    if "pickstock" in name or name.startswith("fpf") or " fpf" in name:
        return BUYER_PICKSTOCK
    if "gamechanger" in compact or "game changer" in name or "paymentadvice" in compact:
        return BUYER_GAMECHANGER
    if "cheque" in name or "eurofarm" in name or "euro farm" in name:
        return BUYER_EUROFARM
    return None


def format_age_years_months(age_days: int | None) -> str | None:
    if age_days is None or age_days < 0:
        return None
    years = age_days // 365
    months = (age_days % 365) // 30
    if years and months:
        return f"{years}y {months}m"
    if years:
        return f"{years}y"
    if months:
        return f"{months}m"
    return "0m"


def _normalize_lact(lact: int | float | None) -> int:
    if lact is None:
        return 0
    try:
        return int(lact)
    except (TypeError, ValueError):
        return 0


def compute_dim_at_cull(
    *,
    lact: int | None,
    event_date: dt.date,
    bdat: dt.date | None,
    fdat: dt.date | None,
    dim_field: float | None,
) -> int | None:
    lact_n = _normalize_lact(lact)
    if lact_n > 0:
        if dim_field is not None:
            try:
                return int(round(float(dim_field)))
            except (TypeError, ValueError):
                pass
        if fdat is not None:
            days = (event_date - fdat).days
            return days if days >= 0 else None
        return None
    if bdat is not None:
        days = (event_date - bdat).days
        return days if days >= 0 else None
    return None


def compute_price_per_kg(amount_gbp: float, cold_weight_kg: float) -> float | None:
    if cold_weight_kg <= 0:
        return None
    return round(amount_gbp / cold_weight_kg, 2)


def _category_from_event(lact: int | None, cbrd: int | None, gndr: str | None) -> str:
    stock_group = stock_group_from_event_fields(lact, cbrd, gndr)
    return STOCK_GROUP_CATEGORY[stock_group]


def _load_sold_events(
    db: Session,
    farms: list[str],
    etags: set[str],
    min_date: dt.date,
    max_date: dt.date,
) -> dict[tuple[str, str], list[CowEvent]]:
    if not etags:
        return {}
    normalized_etags = {normalize_etag(etag) for etag in etags}
    normalized_etags.discard("")
    if not normalized_etags:
        return {}

    window_start = min_date - dt.timedelta(days=EVENT_MATCH_WINDOW_DAYS)
    window_end = max_date + dt.timedelta(days=EVENT_MATCH_WINDOW_DAYS)
    rows = db.scalars(
        select(CowEvent).where(
            cattle_sales_exit_event_clause(),
            CowEvent.farm.in_(farms),
            CowEvent.event_date.isnot(None),
            CowEvent.event_date >= window_start,
            CowEvent.event_date <= window_end,
        )
    ).all()
    grouped: dict[tuple[str, str], list[CowEvent]] = {}
    for row in rows:
        etag = normalize_etag(row.etag)
        if not etag or etag not in normalized_etags:
            continue
        key = (row.farm, etag)
        grouped.setdefault(key, []).append(row)
    for events in grouped.values():
        events.sort(key=lambda event: event.event_date or dt.date.min)
    return grouped


def _best_sold_match(
    events: list[CowEvent],
    sale_date: dt.date,
    kill_date: dt.date | None = None,
) -> CowEvent | None:
    if not events:
        return None
    reference_dates = [sale_date]
    if kill_date is not None and kill_date != sale_date:
        reference_dates.append(kill_date)
    best: CowEvent | None = None
    best_delta: int | None = None
    for event in events:
        if event.event_date is None:
            continue
        for reference_date in reference_dates:
            delta = abs((event.event_date - reference_date).days)
            if delta > EVENT_MATCH_WINDOW_DAYS:
                continue
            if best is None or best_delta is None or delta < best_delta:
                best = event
                best_delta = delta
    return best


def format_cattle_gender(gndr: str | None) -> str | None:
    normalized = (gndr or "").strip().upper()
    if normalized in {"M", "MALE"}:
        return "Male"
    if normalized in {"F", "FEMALE"}:
        return "Female"
    return None


def normalize_categories(categories: list[str] | None) -> list[str] | None:
    if not categories:
        return None
    selected = [category for category in categories if category in CATTLE_CATEGORIES]
    return selected or None


def normalize_genders(genders: list[str] | None) -> list[str] | None:
    if not genders:
        return None
    selected: list[str] = []
    for raw in genders:
        mapped = format_cattle_gender(raw)
        if mapped and mapped not in selected:
            selected.append(mapped)
    return selected or None


def normalize_age_days_range(
    age_min: int | None,
    age_max: int | None,
) -> tuple[int, int]:
    lo = AGE_MIN_DAYS_DEFAULT if age_min is None else int(age_min)
    hi = AGE_MAX_DAYS_DEFAULT if age_max is None else int(age_max)
    if lo < 0:
        lo = 0
    if hi < 0:
        hi = 0
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def normalize_buyers(buyers: list[str] | None) -> list[str] | None:
    if not buyers:
        return None
    selected = [buyer.strip() for buyer in buyers if buyer and buyer.strip()]
    return selected or None


def list_cattle_sales(
    db: Session,
    *,
    farms: list[str] | None = None,
    categories: list[str] | None = None,
    genders: list[str] | None = None,
    buyers: list[str] | None = None,
    age_min_days: int | None = None,
    age_max_days: int | None = None,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    include_unmatched: bool = True,
    include_date_bounds: bool = True,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)
    selected_categories = normalize_categories(categories)
    selected_genders = normalize_genders(genders)
    selected_buyers = normalize_buyers(buyers)
    age_lo, age_hi = normalize_age_days_range(age_min_days, age_max_days)
    age_filter_active = (age_lo, age_hi) != (
        AGE_MIN_DAYS_DEFAULT,
        AGE_MAX_DAYS_DEFAULT,
    )
    empty_charts = {"cold_weight_vs_date": [], "amount_vs_date": [], "amount_vs_dim": []}
    if not selected_farms:
        return {
            "rows": [],
            "total": 0,
            "date_bounds": None,
            "buyers": [],
            "charts": empty_charts,
            "categories": list(CATTLE_CATEGORIES),
            "farms": list(HERD_FARM_OPTIONS),
        }

    query = select(CattleSaleLine).where(CattleSaleLine.farm.in_(selected_farms))
    if date_from is not None:
        query = query.where(CattleSaleLine.sale_date >= date_from)
    if date_to is not None:
        query = query.where(CattleSaleLine.sale_date <= date_to)
    query = query.order_by(
        CattleSaleLine.sale_date.desc(),
        CattleSaleLine.farm.asc(),
        CattleSaleLine.etag.asc(),
    )
    sale_lines = list(db.scalars(query).all())

    date_bounds = None
    if include_date_bounds:
        bounds = db.execute(
            select(
                func.min(CattleSaleLine.sale_date),
                func.max(CattleSaleLine.sale_date),
            ).where(CattleSaleLine.farm.in_(selected_farms))
        ).one()
        min_date, max_date = bounds[0], bounds[1]
        if min_date and max_date:
            date_bounds = {"min": min_date.isoformat(), "max": max_date.isoformat()}

    if not sale_lines:
        return {
            "rows": [],
            "total": 0,
            "date_bounds": date_bounds,
            "buyers": [],
            "charts": empty_charts,
            "categories": list(CATTLE_CATEGORIES),
            "farms": list(HERD_FARM_OPTIONS),
        }

    etags = {line.etag for line in sale_lines}
    reference_dates: list[dt.date] = []
    for line in sale_lines:
        reference_dates.append(line.sale_date)
        if line.kill_date is not None:
            reference_dates.append(line.kill_date)
    min_sale = min(reference_dates)
    max_sale = max(reference_dates)
    sold_by_key = _load_sold_events(db, selected_farms, etags, min_sale, max_sale)

    rows: list[dict[str, Any]] = []
    available_buyer_set: set[str] = set()
    for line in sale_lines:
        buyer = infer_cattle_sale_buyer(buyer=line.buyer, source_file=line.source_file)
        norm_etag = normalize_etag(line.etag)
        match = _best_sold_match(
            sold_by_key.get((line.farm, norm_etag), []),
            line.sale_date,
            line.kill_date,
        )
        event_matched = match is not None
        if not event_matched and not include_unmatched:
            continue

        cow_id = None
        age_display = None
        age_days = None
        dim_value = None
        lact = None
        category = None
        gender = None
        event_date = None

        if match is not None:
            cow_id = (match.cow_id or "").strip() or None
            lact = _normalize_lact(match.lact)
            category = _category_from_event(match.lact, match.cbrd, match.gndr)
            gender = format_cattle_gender(match.gndr)
            event_date = match.event_date
            if match.bdat and match.event_date:
                age_days = (match.event_date - match.bdat).days
                age_display = format_age_years_months(age_days)
            dim_value = compute_dim_at_cull(
                lact=lact,
                event_date=match.event_date,
                bdat=match.bdat,
                fdat=match.fdat,
                dim_field=match.dim,
            )

        if selected_categories and category is not None and category not in selected_categories:
            continue
        if (
            selected_genders
            and set(selected_genders) != set(CATTLE_GENDERS)
            and gender not in selected_genders
        ):
            continue
        if age_filter_active and (age_days is None or age_days < age_lo or age_days > age_hi):
            continue

        if buyer:
            available_buyer_set.add(buyer)
        if selected_buyers and (buyer is None or buyer not in selected_buyers):
            continue

        rejected = is_rejected_sale(line.cold_weight_kg, line.reject_kg, line.amount_gbp)
        price_per_kg = (
            None if rejected else compute_price_per_kg(line.amount_gbp, line.cold_weight_kg)
        )
        rows.append(
            {
                "farm": line.farm,
                "cow_id": cow_id,
                "etag": line.etag,
                "age": age_display,
                "age_days": age_days,
                "dim": dim_value,
                "lact": lact,
                "category": category,
                "gender": gender,
                "buyer": buyer,
                "cold_weight_kg": line.cold_weight_kg,
                "reject_kg": line.reject_kg,
                "kill_date": line.kill_date.isoformat() if line.kill_date else None,
                "amount_gbp": line.amount_gbp,
                "is_rejected": rejected,
                "price_per_kg": price_per_kg,
                "sale_date": line.sale_date.isoformat(),
                "event_date": event_date.isoformat() if event_date else None,
                "event_matched": event_matched,
            }
        )

    charts = {
        "cold_weight_vs_date": [
            {"x": row["sale_date"], "y": row["cold_weight_kg"], "farm": row["farm"], "etag": row["etag"]}
            for row in rows
            if row["cold_weight_kg"] is not None
        ],
        "amount_vs_date": [
            {"x": row["sale_date"], "y": row["amount_gbp"], "farm": row["farm"], "etag": row["etag"]}
            for row in rows
            if row["amount_gbp"] is not None and not row.get("is_rejected")
        ],
        "amount_vs_dim": [
            {"x": row["dim"], "y": row["amount_gbp"], "farm": row["farm"], "etag": row["etag"]}
            for row in rows
            if row["dim"] is not None and row["amount_gbp"] is not None and not row.get("is_rejected")
        ],
    }

    return {
        "rows": rows,
        "total": len(rows),
        "date_bounds": date_bounds,
        "buyers": sorted(available_buyer_set),
        "charts": charts,
        "categories": list(CATTLE_CATEGORIES),
        "farms": list(HERD_FARM_OPTIONS),
    }
