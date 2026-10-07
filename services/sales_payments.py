"""Sales payment tracking for sold animals (Office)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import and_, exists, func, literal, or_, select
from sqlalchemy.orm import Session

from services.cattle_sale_pdf import is_rejected_sale, normalize_etag
from services.cattle_sales import EVENT_MATCH_WINDOW_DAYS, cattle_sales_exit_event_clause
from services.database import CattleSaleLine, CowEvent, SalesPaymentRecord, User
from services.events_common import (
    SALES_BEEF_REMARKS,
    SALES_DAIRY_REMARKS,
    SALES_MAPPED_REMARKS,
    SALES_TABLE_REASON_ORDER,
    SALES_TB_REMARKS,
    _sales_reason_expression,
    normalize_farms,
)

def _normalize_key_part(value: str | None) -> str:
    return (value or "").strip()


def _format_gender(gndr: str | None) -> str:
    normalized = (gndr or "").strip().upper()
    if normalized == "M":
        return "Male"
    if normalized == "F":
        return "Female"
    return (gndr or "").strip()


def _age_months_at_sale(bdat: dt.date | None, event_date: dt.date) -> int | None:
    if bdat is None:
        return None
    days = (event_date - bdat).days
    if days < 0:
        return None
    return days // 30


def _payment_match_conditions():
    return and_(
        SalesPaymentRecord.farm == CowEvent.farm,
        SalesPaymentRecord.cow_id == CowEvent.cow_id,
        SalesPaymentRecord.etag == CowEvent.etag,
        SalesPaymentRecord.event_date == CowEvent.event_date,
    )


def _archived_payment_exists():
    return (
        select(literal(1))
        .select_from(SalesPaymentRecord)
        .where(
            _payment_match_conditions(),
            SalesPaymentRecord.archived_at.isnot(None),
        )
        .correlate(CowEvent)
    )


def _reason_filter_conditions(reasons: list[str] | None):
    if not reasons:
        return None
    conditions = []
    if "OFS" in reasons:
        conditions.append(CowEvent.remark == "OFS")
    if "TB" in reasons:
        conditions.append(CowEvent.remark.in_(list(SALES_TB_REMARKS)))
    if "Beef" in reasons:
        conditions.append(CowEvent.remark.in_(list(SALES_BEEF_REMARKS)))
    if "Dairy" in reasons:
        conditions.append(CowEvent.remark.in_(list(SALES_DAIRY_REMARKS)))
    if "CULL" in reasons:
        conditions.append(
            or_(
                CowEvent.remark.is_(None),
                CowEvent.remark.notin_(list(SALES_MAPPED_REMARKS)),
            )
        )
    if not conditions:
        return None
    return or_(*conditions)


def _dests_match(row_dest: str | None, selected: str | None) -> bool:
    wanted = (selected or "").strip().upper()
    if not wanted:
        return True
    return (row_dest or "").strip().upper() == wanted


def _apply_sold_event_filters(
    query,
    *,
    farms: list[str],
    reasons: list[str] | None,
    event_from: dt.date | None,
    event_to: dt.date | None,
):
    query = query.where(cattle_sales_exit_event_clause()).where(
        CowEvent.event_date.isnot(None)
    )
    query = query.where(CowEvent.farm.in_(farms))
    reason_filter = _reason_filter_conditions(reasons)
    if reason_filter is not None:
        query = query.where(reason_filter)
    if event_from is not None:
        query = query.where(CowEvent.event_date >= event_from)
    if event_to is not None:
        query = query.where(CowEvent.event_date <= event_to)
    return query


def _load_cattle_sale_lines_by_farm_etag(
    db: Session,
    farms: list[str],
    min_date: dt.date,
    max_date: dt.date,
) -> dict[tuple[str, str], list[CattleSaleLine]]:
    window_start = min_date - dt.timedelta(days=EVENT_MATCH_WINDOW_DAYS)
    window_end = max_date + dt.timedelta(days=EVENT_MATCH_WINDOW_DAYS)
    sale_lines = db.scalars(
        select(CattleSaleLine).where(
            CattleSaleLine.farm.in_(farms),
            CattleSaleLine.sale_date >= window_start,
            CattleSaleLine.sale_date <= window_end,
        )
    ).all()
    grouped: dict[tuple[str, str], list[CattleSaleLine]] = {}
    for line in sale_lines:
        etag = normalize_etag(line.etag)
        if not etag:
            continue
        grouped.setdefault((line.farm, etag), []).append(line)
        grouped.setdefault(("", etag), []).append(line)
    for lines in grouped.values():
        lines.sort(key=lambda row: row.sale_date)
    return grouped


def _sale_line_match_delta(line: CattleSaleLine, event_date: dt.date) -> int:
    deltas = [abs((line.sale_date - event_date).days)]
    if line.kill_date is not None:
        deltas.append(abs((line.kill_date - event_date).days))
    return min(deltas)


def _sale_match_for_sold_event(
    sale_lines_by_key: dict[tuple[str, str], list[CattleSaleLine]],
    farm: str,
    etag: str | None,
    event_date: dt.date,
) -> dict[str, Any] | None:
    normalized_etag = normalize_etag(etag)
    if not normalized_etag:
        return None
    lines = sale_lines_by_key.get((farm, normalized_etag), [])
    if not lines:
        lines = sale_lines_by_key.get(("", normalized_etag), [])
    best_line: CattleSaleLine | None = None
    best_delta: int | None = None
    for line in lines:
        delta = _sale_line_match_delta(line, event_date)
        if delta > EVENT_MATCH_WINDOW_DAYS:
            continue
        if best_line is None or best_delta is None or delta < best_delta:
            best_line = line
            best_delta = delta
    if best_line is None:
        return None

    rejected = is_rejected_sale(
        best_line.cold_weight_kg, best_line.reject_kg, best_line.amount_gbp
    )
    has_sale_amount = rejected or best_line.amount_gbp > 0
    return {
        "amount_gbp": best_line.amount_gbp if has_sale_amount else None,
        "sale_rejected": rejected,
        "has_sale_amount": has_sale_amount,
        "buyer": (best_line.buyer or "").strip(),
    }


def _row_to_dict(
    farm: str,
    cow_id: str | None,
    etag: str | None,
    dest: str | None,
    event_date: dt.date,
    sales_reason: str,
    gndr: str | None = None,
    bdat: dt.date | None = None,
    paid_at: dt.datetime | None = None,
    archived_at: dt.datetime | None = None,
    amount_gbp: float | None = None,
    sale_rejected: bool = False,
    has_sale_amount: bool = False,
) -> dict[str, Any]:
    normalized_cow_id = _normalize_key_part(cow_id)
    normalized_etag = _normalize_key_part(etag)
    gender = _format_gender(gndr)
    age_months = _age_months_at_sale(bdat, event_date)
    return {
        "farm": farm,
        "cow_id": normalized_cow_id,
        "etag": normalized_etag,
        "gender": gender,
        "age_months": age_months,
        "dest": dest or "",
        "event_date": event_date.isoformat(),
        "sales_reason": sales_reason,
        "amount_gbp": amount_gbp,
        "sale_rejected": sale_rejected,
        "has_sale_amount": has_sale_amount,
        "paid_at": paid_at.isoformat() if paid_at else None,
        "archived_at": archived_at.isoformat() if archived_at else None,
        "payment_key": {
            "farm": farm,
            "cow_id": normalized_cow_id,
            "etag": normalized_etag,
            "event_date": event_date.isoformat(),
        },
    }


def _latest_event_for_normalized_etag(db: Session, farm: str, etag: str) -> CowEvent | None:
    exact = db.scalars(
        select(CowEvent)
        .where(CowEvent.farm == farm, CowEvent.etag == etag)
        .order_by(CowEvent.event_date.desc(), CowEvent.id.desc())
    ).first()
    if exact is not None:
        return exact
    suffix = etag[-8:] if len(etag) >= 8 else etag
    rows = db.scalars(
        select(CowEvent).where(
            CowEvent.farm == farm,
            CowEvent.etag.isnot(None),
            CowEvent.etag.like(f"%{suffix}"),
        )
    ).all()
    matches = [row for row in rows if normalize_etag(row.etag) == etag]
    if not matches:
        return None
    matches.sort(key=lambda row: (row.event_date or dt.date.min, row.id or 0), reverse=True)
    return matches[0]


def _remittance_sales_reason(buyer: str | None, identity) -> str:
    remark = (getattr(identity, "remark", None) or "").strip().upper()
    if remark in SALES_BEEF_REMARKS:
        return "Beef"
    if remark in {"CAR18", "CAR19"}:
        return "Dairy"
    buyer_u = (buyer or "").upper()
    if any(token in buyer_u for token in ("DRAYTON", "BLADE", "GAMECHANGER", "WARRENDALE", "WARREN")):
        return "Beef"
    return "CULL"


def _unmatched_remittance_rows(
    db: Session,
    *,
    farms: list[str],
    reasons: list[str] | None,
    dest: str | None,
    event_from: dt.date | None,
    event_to: dt.date | None,
    has_amount: bool | None,
    existing_etags: set[str],
) -> list[dict[str, Any]]:
    """Show remittance lines that do not yet have a DairyComp SOLD event."""
    query = select(CattleSaleLine).where(CattleSaleLine.farm.in_(farms))
    if event_from is not None:
        query = query.where(CattleSaleLine.sale_date >= event_from)
    if event_to is not None:
        query = query.where(CattleSaleLine.sale_date <= event_to)
    extra: list[dict[str, Any]] = []
    for line in db.scalars(query).all():
        etag = normalize_etag(line.etag)
        if not etag or etag in existing_etags:
            continue
        buyer = (line.buyer or "").strip()
        if not _dests_match(buyer, dest):
            continue
        event_date = line.kill_date or line.sale_date
        rejected = is_rejected_sale(line.cold_weight_kg, line.reject_kg, line.amount_gbp)
        has_sale_amount = rejected or line.amount_gbp > 0
        if has_amount is True and not has_sale_amount:
            continue
        if has_amount is False and has_sale_amount:
            continue
        identity = _latest_event_for_normalized_etag(db, line.farm, etag)
        sales_reason = _remittance_sales_reason(line.buyer, identity)
        if reasons and sales_reason not in reasons:
            continue
        extra.append(
            _row_to_dict(
                line.farm,
                identity.cow_id if identity else None,
                identity.etag if identity else line.etag,
                buyer,
                event_date,
                sales_reason,
                identity.gndr if identity else None,
                identity.bdat if identity else None,
                amount_gbp=line.amount_gbp if has_sale_amount else None,
                sale_rejected=rejected,
                has_sale_amount=has_sale_amount,
            )
        )
    return extra


def _apply_status_filter(query, status: str):
    if status == "archived":
        return query.where(SalesPaymentRecord.archived_at.isnot(None))
    return query.where(~exists(_archived_payment_exists()))


def _compute_date_bounds(
    db: Session,
    *,
    status: str,
    farms: list[str],
    reasons: list[str] | None,
) -> dict[str, str] | None:
    bounds_query = select(func.min(CowEvent.event_date), func.max(CowEvent.event_date)).select_from(
        CowEvent
    )
    if status == "archived":
        bounds_query = bounds_query.join(
            SalesPaymentRecord,
            _payment_match_conditions(),
        )
    bounds_query = _apply_sold_event_filters(
        bounds_query,
        farms=farms,
        reasons=reasons,
        event_from=None,
        event_to=None,
    )
    bounds_query = _apply_status_filter(bounds_query, status)
    min_date, max_date = db.execute(bounds_query).one()
    if min_date is None or max_date is None:
        return None
    if hasattr(min_date, "date"):
        min_date = min_date.date()
    if hasattr(max_date, "date"):
        max_date = max_date.date()
    return {"min": min_date.isoformat(), "max": max_date.isoformat()}


def list_sales_payments(
    db: Session,
    *,
    status: str = "active",
    farms: list[str] | None = None,
    reasons: list[str] | None = None,
    dest: str | None = None,
    event_from: dt.date | None = None,
    event_to: dt.date | None = None,
    include_date_bounds: bool = True,
    has_amount: bool | None = None,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)
    if not selected_farms:
        return {"rows": [], "total": 0, "status": status, "date_bounds": None}

    reason_expr = _sales_reason_expression()
    etag_suffix = func.substr(func.coalesce(CowEvent.etag, ""), -5)

    if status == "archived":
        query = (
            select(
                CowEvent.farm,
                CowEvent.cow_id,
                CowEvent.etag,
                CowEvent.event_date,
                reason_expr.label("sales_reason"),
                CowEvent.gndr,
                CowEvent.bdat,
                SalesPaymentRecord.paid_at,
                SalesPaymentRecord.archived_at,
            )
            .select_from(CowEvent)
            .join(SalesPaymentRecord, _payment_match_conditions())
        )
    else:
        query = select(
            CowEvent.farm,
            CowEvent.cow_id,
            CowEvent.etag,
            CowEvent.event_date,
            reason_expr.label("sales_reason"),
            CowEvent.gndr,
            CowEvent.bdat,
            literal(None).label("paid_at"),
            literal(None).label("archived_at"),
        )

    query = _apply_sold_event_filters(
        query,
        farms=selected_farms,
        reasons=reasons,
        event_from=event_from,
        event_to=event_to,
    )
    query = _apply_status_filter(query, status)
    query = query.order_by(
        CowEvent.event_date.asc(),
        etag_suffix.asc(),
    )

    date_bounds = None
    if include_date_bounds:
        date_bounds = _compute_date_bounds(
            db,
            status=status,
            farms=selected_farms,
            reasons=reasons,
        )

    result_rows = list(db.execute(query).all())
    sale_lines_by_key: dict[tuple[str, str], list[CattleSaleLine]] = {}
    if result_rows:
        event_dates = [row[3] for row in result_rows if row[3] is not None]
        if event_dates:
            min_event = min(event_dates)
            max_event = max(event_dates)
            if hasattr(min_event, "date"):
                min_event = min_event.date()
            if hasattr(max_event, "date"):
                max_event = max_event.date()
            sale_lines_by_key = _load_cattle_sale_lines_by_farm_etag(
                db,
                selected_farms,
                min_event,
                max_event,
            )

    rows = []
    for (
        farm,
        cow_id,
        etag,
        event_date,
        sales_reason,
        gndr,
        bdat,
        paid_at,
        archived_at,
    ) in result_rows:
        if hasattr(event_date, "date"):
            event_date = event_date.date()
        amount_gbp = None
        sale_rejected = False
        has_sale_amount = False
        remittance_dest = ""
        sale_match = _sale_match_for_sold_event(
            sale_lines_by_key, farm, etag, event_date
        )
        if sale_match is not None:
            amount_gbp = sale_match["amount_gbp"]
            sale_rejected = sale_match["sale_rejected"]
            has_sale_amount = sale_match["has_sale_amount"]
            remittance_dest = sale_match["buyer"]
        if has_amount is True and not has_sale_amount:
            continue
        if has_amount is False and has_sale_amount:
            continue
        if not _dests_match(remittance_dest, dest):
            continue
        rows.append(
            _row_to_dict(
                farm,
                cow_id,
                etag,
                remittance_dest,
                event_date,
                sales_reason,
                gndr,
                bdat,
                paid_at,
                archived_at,
                amount_gbp=amount_gbp,
                sale_rejected=sale_rejected,
                has_sale_amount=has_sale_amount,
            )
        )

    if status != "archived":
        extra_rows = _unmatched_remittance_rows(
            db,
            farms=selected_farms,
            reasons=reasons,
            dest=dest,
            event_from=event_from,
            event_to=event_to,
            has_amount=has_amount,
            existing_etags={normalize_etag(row["etag"]) for row in rows},
        )
        rows.extend(extra_rows)
    rows.sort(key=lambda row: (row["event_date"], row["dest"], row["etag"]))

    return {"rows": rows, "total": len(rows), "status": status, "date_bounds": date_bounds}


def list_dest_filter_options(
    db: Session,
    *,
    status: str = "active",
    farms: list[str] | None = None,
    reasons: list[str] | None = None,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)
    if not selected_farms:
        return {"dest_options": [], "date_bounds": None}

    dest_options = [
        buyer
        for buyer in db.scalars(
            select(func.distinct(CattleSaleLine.buyer)).where(
                CattleSaleLine.farm.in_(selected_farms),
                CattleSaleLine.buyer.isnot(None),
                func.trim(CattleSaleLine.buyer) != "",
            )
        ).all()
        if buyer and buyer.strip()
    ]
    dest_options.sort(key=str.upper)
    date_bounds = _compute_date_bounds(
        db,
        status=status,
        farms=selected_farms,
        reasons=reasons,
    )
    return {
        "dest_options": dest_options,
        "date_bounds": date_bounds,
    }


def normalize_sales_reasons(reasons: list[str] | None) -> list[str]:
    if not reasons:
        return list(SALES_TABLE_REASON_ORDER)
    selected: list[str] = []
    for value in reasons:
        normalized = value.strip().upper()
        for reason in SALES_TABLE_REASON_ORDER:
            if reason.upper() == normalized and reason not in selected:
                selected.append(reason)
    return selected or list(SALES_TABLE_REASON_ORDER)


def _payment_record_key(
    farm: str,
    cow_id: str,
    etag: str,
    event_date: dt.date,
) -> tuple[str, str, str, dt.date]:
    return (farm, _normalize_key_part(cow_id), _normalize_key_part(etag), event_date)


def _parse_payment_item(item: dict[str, Any]) -> tuple[str, str, str, dt.date]:
    farm = item["farm"]
    cow_id = _normalize_key_part(item.get("cow_id"))
    etag = _normalize_key_part(item.get("etag"))
    event_date = item["event_date"]
    if isinstance(event_date, str):
        event_date = dt.date.fromisoformat(event_date)
    return farm, cow_id, etag, event_date


def _load_payment_records_for_items(
    db: Session,
    items: list[dict[str, Any]],
) -> dict[tuple[str, str, str, dt.date], SalesPaymentRecord]:
    parsed = [_parse_payment_item(item) for item in items]
    if not parsed:
        return {}

    conditions = [
        and_(
            SalesPaymentRecord.farm == farm,
            func.coalesce(SalesPaymentRecord.cow_id, "") == cow_id,
            func.coalesce(SalesPaymentRecord.etag, "") == etag,
            SalesPaymentRecord.event_date == event_date,
        )
        for farm, cow_id, etag, event_date in parsed
    ]
    records = db.scalars(select(SalesPaymentRecord).where(or_(*conditions))).all()
    return {
        _payment_record_key(
            record.farm,
            record.cow_id,
            record.etag,
            record.event_date,
        ): record
        for record in records
    }


def confirm_payments(
    db: Session,
    items: list[dict[str, Any]],
    user: User,
) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    existing = _load_payment_records_for_items(db, items)
    confirmed = 0
    for item in items:
        farm, cow_id, etag, event_date = _parse_payment_item(item)
        key = _payment_record_key(farm, cow_id, etag, event_date)
        record = existing.get(key)
        if record:
            record.paid_at = now
            record.archived_at = now
            record.confirmed_by_user_id = user.id
            record.unarchived_at = None
        else:
            db.add(
                SalesPaymentRecord(
                    farm=farm,
                    cow_id=cow_id,
                    etag=etag,
                    event_date=event_date,
                    paid_at=now,
                    archived_at=now,
                    confirmed_by_user_id=user.id,
                )
            )
        confirmed += 1
    return {"confirmed": confirmed}


def unarchive_payments(
    db: Session,
    items: list[dict[str, Any]],
    user: User,
) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    existing = _load_payment_records_for_items(db, items)
    restored = 0
    for item in items:
        farm, cow_id, etag, event_date = _parse_payment_item(item)
        key = _payment_record_key(farm, cow_id, etag, event_date)
        record = existing.get(key)
        if record is None or record.archived_at is None:
            continue
        record.archived_at = None
        record.paid_at = None
        record.unarchived_at = now
        record.confirmed_by_user_id = user.id
        restored += 1
    return {"restored": restored}
