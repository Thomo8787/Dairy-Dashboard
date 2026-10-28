"""Sales payment tracking for sold animals (Office)."""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

from sqlalchemy import and_, case, exists, func, literal, or_, select
from sqlalchemy.orm import Session

from services.cattle_sale_pdf import is_rejected_sale, normalize_etag
from services.cattle_sales import (
    BUYER_WARRENDALE,
    EVENT_MATCH_WINDOW_DAYS,
    WARRENDALE_BONUS_GBP,
    _is_warrendale_bonus_line,
    _load_sold_events,
    _match_sale_to_sold_event,
    _merge_sold_events,
    cattle_sales_exit_event_clause,
)

WARRENDALE_BONUS_LOOKAHEAD_DAYS = 28
from services.database import CattleSaleLine, CowEvent, SalesPaymentRecord, User
from services.events_common import (
    OFS_DEST_PREFIXES,
    SALES_BEEF_REMARKS,
    SALES_DAIRY_REMARKS,
    SALES_MAPPED_REMARKS,
    SALES_TABLE_REASON_ORDER,
    SALES_TB_REMARKS,
    died_cert_clause,
    normalize_farms,
    ofs_sales_clause,
)
from services.farms import HERD_FARM_OPTIONS
from services.herd_import_utils import BEEF_CBREED_MIN

def _normalize_key_part(value: str | None) -> str:
    return (value or "").strip()


def _compact_etag(value: str | None) -> str:
    return "".join((value or "").split()).upper()


def _etag_contains(column, fragment: str):
    compact = func.upper(func.replace(func.coalesce(column, ""), " ", ""))
    return compact.contains(fragment, autoescape=True)


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


def _payment_beef_calf_clause():
    """Lactation 0 and a beef breed, excluding fallen stock and TB."""
    return and_(
        func.coalesce(CowEvent.lact, 0) == 0,
        CowEvent.cbrd.isnot(None),
        CowEvent.cbrd >= BEEF_CBREED_MIN,
        ~ofs_sales_clause(),
        ~died_cert_clause(),
        or_(
            CowEvent.remark.is_(None),
            CowEvent.remark.notin_(list(SALES_TB_REMARKS)),
        ),
    )


def _payment_dairy_transfer_clause():
    """Lactation 0 dairy heifer whose remark and dest are the same herd farm."""
    remark = func.upper(func.trim(CowEvent.remark))
    dest = func.upper(func.trim(CowEvent.dest))
    return and_(
        func.coalesce(CowEvent.lact, 0) == 0,
        func.upper(func.trim(CowEvent.gndr)) == "F",
        CowEvent.cbrd.isnot(None),
        CowEvent.cbrd < BEEF_CBREED_MIN,
        remark == dest,
        remark.in_(list(HERD_FARM_OPTIONS)),
    )


def _payment_sales_reason_expression():
    return case(
        (or_(ofs_sales_clause(), died_cert_clause()), literal("OFS")),
        (CowEvent.remark.in_(list(SALES_TB_REMARKS)), literal("TB")),
        (_payment_beef_calf_clause(), literal("Beef")),
        (CowEvent.remark.in_(list(SALES_BEEF_REMARKS)), literal("Beef")),
        (or_(
            CowEvent.remark.in_(list(SALES_DAIRY_REMARKS)),
            _payment_dairy_transfer_clause(),
        ), literal("Dairy")),
        else_=literal("CULL"),
    )


def _reason_filter_conditions(reasons: list[str] | None):
    if not reasons:
        return None
    beef_calf = _payment_beef_calf_clause()
    dairy_transfer = _payment_dairy_transfer_clause()
    ofs = or_(ofs_sales_clause(), died_cert_clause())
    conditions = []
    if "OFS" in reasons:
        conditions.append(ofs)
    if "TB" in reasons:
        conditions.append(
            and_(
                CowEvent.remark.in_(list(SALES_TB_REMARKS)),
                ~ofs,
            )
        )
    if "Beef" in reasons:
        conditions.append(
            and_(
                or_(
                    CowEvent.remark.in_(list(SALES_BEEF_REMARKS)),
                    beef_calf,
                ),
                ~ofs,
            )
        )
    if "Dairy" in reasons:
        conditions.append(
            or_(
                and_(
                    CowEvent.remark.in_(list(SALES_DAIRY_REMARKS)),
                    ~beef_calf,
                    ~ofs,
                ),
                dairy_transfer,
            )
        )
    if "CULL" in reasons:
        conditions.append(
            and_(
                or_(
                    CowEvent.remark.is_(None),
                    CowEvent.remark.notin_(list(SALES_MAPPED_REMARKS)),
                ),
                ~beef_calf,
                ~ofs,
                ~dairy_transfer,
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
    cow_id: str | None = None,
    etag_contains: str | None = None,
):
    query = query.where(cattle_sales_exit_event_clause()).where(
        CowEvent.event_date.isnot(None)
    )
    query = query.where(CowEvent.farm.in_(farms))
    reason_filter = _reason_filter_conditions(reasons)
    if reason_filter is not None:
        query = query.where(reason_filter)
    normalized_cow_id = _normalize_key_part(cow_id)
    if normalized_cow_id:
        query = query.where(func.trim(CowEvent.cow_id) == normalized_cow_id)
        return query
    fragment = _compact_etag(etag_contains)
    if fragment:
        query = query.where(_etag_contains(CowEvent.etag, fragment))
        return query
    if event_from is not None:
        query = query.where(CowEvent.event_date >= event_from)
    if event_to is not None:
        query = query.where(CowEvent.event_date <= event_to)
    return query


def _exact_cow_id_in_view(
    db: Session,
    *,
    status: str,
    farms: list[str],
    reasons: list[str] | None,
    cow_id: str,
) -> bool:
    if status == "archived":
        query = select(SalesPaymentRecord.id).where(
            SalesPaymentRecord.archived_at.isnot(None),
            SalesPaymentRecord.farm.in_(farms),
            func.coalesce(SalesPaymentRecord.cow_id, "") == cow_id,
        )
        return db.scalar(query.limit(1)) is not None
    query = select(CowEvent.id)
    query = _apply_sold_event_filters(
        query,
        farms=farms,
        reasons=reasons,
        event_from=None,
        event_to=None,
        cow_id=cow_id,
    )
    query = _apply_status_filter(query, status)
    return db.scalar(query.limit(1)) is not None


def _append_sale_line(
    grouped: dict[tuple[str, str], list[CattleSaleLine]],
    line: CattleSaleLine,
) -> None:
    etag = normalize_etag(line.etag)
    if not etag:
        return
    for key in ((line.farm, etag), ("", etag)):
        bucket = grouped.setdefault(key, [])
        if any(existing.id == line.id for existing in bucket):
            continue
        bucket.append(line)


def _load_cattle_sale_lines_by_farm_etag(
    db: Session,
    farms: list[str],
    min_date: dt.date,
    max_date: dt.date,
    bonus_etags: set[str] | None = None,
) -> dict[tuple[str, str], list[CattleSaleLine]]:
    window_start = min_date - dt.timedelta(days=EVENT_MATCH_WINDOW_DAYS)
    window_end = max_date + dt.timedelta(days=WARRENDALE_BONUS_LOOKAHEAD_DAYS)
    sale_lines = list(
        db.scalars(
            select(CattleSaleLine).where(
                CattleSaleLine.farm.in_(farms),
                CattleSaleLine.sale_date >= window_start,
                CattleSaleLine.sale_date <= window_end,
            )
        ).all()
    )
    normalized_bonus_etags = {normalize_etag(etag) for etag in (bonus_etags or set())}
    normalized_bonus_etags.discard("")
    if normalized_bonus_etags:
        sale_lines.extend(
            db.scalars(
                select(CattleSaleLine).where(
                    CattleSaleLine.farm.in_(farms),
                    CattleSaleLine.etag.in_(list(normalized_bonus_etags)),
                    CattleSaleLine.is_bonus.is_(True),
                )
            ).all()
        )
    grouped: dict[tuple[str, str], list[CattleSaleLine]] = {}
    for line in sale_lines:
        _append_sale_line(grouped, line)
    for lines in grouped.values():
        lines.sort(key=lambda row: row.sale_date)
    return grouped


_PO_NUMBER_RE = re.compile(r"(PO\d{2}-\d+)", re.IGNORECASE)


_PLACEHOLDER_MESSAGE_IDS = frozenset({"local-file", "outlook"})


def _outlook_message_id(value: str | None) -> str | None:
    """Graph message id. Uploads and disk imports have no mailbox message."""
    message_id = (value or "").strip()
    if not message_id or message_id in _PLACEHOLDER_MESSAGE_IDS:
        return None
    if message_id.lower().startswith("upload:"):
        return None
    return message_id


def sale_has_outlook_message(db: Session, message_id: str | None) -> bool:
    """True when this Graph id is stored on a cattle sale line."""
    cleaned = _outlook_message_id(message_id)
    if cleaned is None:
        return False
    found = db.scalar(
        select(CattleSaleLine.id)
        .where(CattleSaleLine.source_message_id == cleaned)
        .limit(1)
    )
    return found is not None


def _document_from_filename(source_file: str | None) -> dict[str, str] | None:
    """Short document number from a remittance PDF name, plus the full name."""
    name = (source_file or "").strip()
    if not name:
        return None
    stem = name[:-4] if name.lower().endswith(".pdf") else name
    match = _PO_NUMBER_RE.search(name)
    label = match.group(1).upper() if match else stem.strip()
    if not label:
        return None
    return {"label": label, "file": name}


def _documents_for_files(
    *items: tuple[str | None, str | None],
) -> list[dict[str, str]]:
    documents: list[dict[str, str]] = []
    seen: set[str] = set()
    for name, message_id in items:
        document = _document_from_filename(name)
        if document is None or document["label"] in seen:
            continue
        seen.add(document["label"])
        outlook_id = _outlook_message_id(message_id)
        if outlook_id:
            document["message_id"] = outlook_id
        documents.append(document)
    return documents


def _is_warrendale_bonus(amount: float | None) -> bool:
    if amount is None:
        return False
    return abs(float(amount) - WARRENDALE_BONUS_GBP) < 0.005


def _is_warrendale_line(line: CattleSaleLine) -> bool:
    return (line.buyer or "").strip() == BUYER_WARRENDALE


def _warrendale_split_match(
    lines: list[CattleSaleLine],
    event_date: dt.date,
) -> dict[str, Any] | None:
    """Calf price within 14 days. The £80 bonus matches on ear tag, and the nearest bonus wins."""
    calf_line: CattleSaleLine | None = None
    calf_delta: int | None = None
    for line in lines:
        if not _is_warrendale_line(line) or _is_warrendale_bonus(line.amount_gbp):
            continue
        delta = _sale_line_match_delta(line, event_date)
        if delta > EVENT_MATCH_WINDOW_DAYS:
            continue
        if calf_line is None or calf_delta is None or delta < calf_delta:
            calf_line = line
            calf_delta = delta
    bonus_line: CattleSaleLine | None = None
    bonus_delta: int | None = None
    for line in lines:
        if not _is_warrendale_line(line) or not _is_warrendale_bonus(line.amount_gbp):
            continue
        delta = _sale_line_match_delta(line, event_date)
        if bonus_line is None or bonus_delta is None or delta < bonus_delta:
            bonus_line = line
            bonus_delta = delta
    if calf_line is None and bonus_line is None:
        return None
    rejected = False
    if calf_line is not None:
        rejected = is_rejected_sale(
            calf_line.cold_weight_kg, calf_line.reject_kg, calf_line.amount_gbp
        )
    calf_amount = round(float(calf_line.amount_gbp), 2) if calf_line is not None else None
    bonus_amount = round(float(bonus_line.amount_gbp), 2) if bonus_line is not None else None
    has_sale_amount = rejected or calf_amount is not None or bonus_amount is not None
    return {
        "calf_amount_gbp": calf_amount,
        "bonus_gbp": bonus_amount,
        "sale_rejected": rejected,
        "has_sale_amount": has_sale_amount,
        "buyer": BUYER_WARRENDALE,
        "warrendale_split": True,
        "source_file": calf_line.source_file if calf_line is not None else None,
        "bonus_source_file": bonus_line.source_file if bonus_line is not None else None,
        "source_message_id": (
            calf_line.source_message_id if calf_line is not None else None
        ),
        "bonus_source_message_id": (
            bonus_line.source_message_id if bonus_line is not None else None
        ),
    }


def _sale_line_match_delta(line: CattleSaleLine, event_date: dt.date) -> int:
    deltas = [abs((line.sale_date - event_date).days)]
    if line.kill_date is not None:
        deltas.append(abs((line.kill_date - event_date).days))
    return min(deltas)


def _candidate_sale_lines(
    sale_lines_by_key: dict[tuple[str, str], list[CattleSaleLine]],
    farm: str,
    etag: str | None,
) -> list[CattleSaleLine]:
    normalized_etag = normalize_etag(etag)
    if not normalized_etag:
        return []
    return sale_lines_by_key.get((farm, normalized_etag), [])


def _has_other_farm_remittance(
    sale_lines_by_key: dict[tuple[str, str], list[CattleSaleLine]],
    farm: str,
    etag: str | None,
    event_date: dt.date | None,
) -> bool:
    """True when another herd's PDF covers this ear tag within 14 days."""
    normalized_etag = normalize_etag(etag)
    if not normalized_etag or event_date is None:
        return False
    for (line_farm, line_etag), lines in sale_lines_by_key.items():
        if not line_farm or line_farm == farm or line_etag != normalized_etag:
            continue
        for line in lines:
            if _sale_line_match_delta(line, event_date) <= EVENT_MATCH_WINDOW_DAYS:
                return True
    return False


def _closest_sale_match(
    lines: list[CattleSaleLine],
    event_date: dt.date,
) -> dict[str, Any] | None:
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
    amount = best_line.amount_gbp if has_sale_amount else None
    return {
        "amount_gbp": amount,
        "calf_amount_gbp": amount,
        "bonus_gbp": None,
        "sale_rejected": rejected,
        "has_sale_amount": has_sale_amount,
        "buyer": (best_line.buyer or "").strip(),
        "warrendale_split": False,
        "source_file": best_line.source_file,
        "bonus_source_file": None,
        "source_message_id": best_line.source_message_id,
        "bonus_source_message_id": None,
    }


def _sale_match_for_sold_event(
    sale_lines_by_key: dict[tuple[str, str], list[CattleSaleLine]],
    farm: str,
    etag: str | None,
    event_date: dt.date,
    sales_reason: str | None = None,
) -> dict[str, Any] | None:
    lines = _candidate_sale_lines(sale_lines_by_key, farm, etag)
    if sales_reason == "Beef":
        split = _warrendale_split_match(lines, event_date)
        if split is not None:
            return split
    return _closest_sale_match(lines, event_date)


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
    bonus_gbp: float | None = None,
    amount_paid: bool = False,
    bonus_paid: bool = False,
    warrendale_split: bool = False,
    confirm_part: str | None = None,
    dairycomp_farm: str | None = None,
    farm_sold: str | None = None,
    farm_paid: str | None = None,
    documents: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    normalized_cow_id = _normalize_key_part(cow_id)
    normalized_etag = _normalize_key_part(etag)
    gender = _format_gender(gndr)
    age_months = _age_months_at_sale(bdat, event_date)
    sold_name = (farm if farm_sold is None else farm_sold).strip()
    paid_name = (farm_paid or "").strip()
    return {
        "farm": farm,
        "farm_sold": sold_name,
        "farm_paid": paid_name,
        "farms_match": bool(sold_name) and sold_name.upper() == paid_name.upper(),
        "cow_id": normalized_cow_id,
        "etag": normalized_etag,
        "gender": gender,
        "age_months": age_months,
        "dest": dest or "",
        "event_date": event_date.isoformat(),
        "sales_reason": sales_reason,
        "amount_gbp": amount_gbp,
        "bonus_gbp": bonus_gbp,
        "amount_paid": amount_paid,
        "bonus_paid": bonus_paid,
        "warrendale_split": warrendale_split,
        "confirm_part": confirm_part,
        "sale_rejected": sale_rejected,
        "has_sale_amount": has_sale_amount,
        "paid_at": paid_at.isoformat() if paid_at else None,
        "archived_at": archived_at.isoformat() if archived_at else None,
        "dairycomp_farm": dairycomp_farm,
        "documents": documents or [],
        "payment_key": {
            "farm": farm,
            "cow_id": normalized_cow_id,
            "etag": normalized_etag,
            "event_date": event_date.isoformat(),
        },
    }


def _confirm_part(
    *,
    warrendale_split: bool,
    calf_amount: float | None,
    bonus_amount: float | None,
    amount_paid: bool,
    bonus_paid: bool,
) -> str | None:
    if not warrendale_split:
        return None
    if calf_amount is not None and bonus_amount is not None and not amount_paid and not bonus_paid:
        return "both"
    if calf_amount is not None and not amount_paid:
        return "amount"
    if bonus_amount is not None and not bonus_paid:
        return "bonus"
    return None


def _display_amount(
    calf_amount: float | None,
    bonus_amount: float | None,
    amount_paid: bool,
    bonus_paid: bool,
) -> float | None:
    if (
        calf_amount is not None
        and bonus_amount is not None
        and not amount_paid
        and not bonus_paid
    ):
        return round(calf_amount + bonus_amount, 2)
    return calf_amount


def _payment_flags_by_key(
    db: Session,
    farms: list[str],
    min_date: dt.date | None,
    max_date: dt.date | None,
) -> dict[tuple[str, str, str, dt.date], SalesPaymentRecord]:
    query = select(SalesPaymentRecord).where(SalesPaymentRecord.farm.in_(farms))
    if min_date is not None:
        query = query.where(SalesPaymentRecord.event_date >= min_date)
    if max_date is not None:
        query = query.where(SalesPaymentRecord.event_date <= max_date)
    records = db.scalars(query).all()
    return {
        _payment_record_key(record.farm, record.cow_id, record.etag, record.event_date): record
        for record in records
    }


def _identity_is_ofs_or_tb(identity) -> str | None:
    remark = (getattr(identity, "remark", None) or "").strip().upper()
    dest = (getattr(identity, "dest", None) or "").strip().upper()
    event = (getattr(identity, "event", None) or "").strip().upper()
    if remark == "OFS" or any(dest.startswith(prefix) for prefix in OFS_DEST_PREFIXES):
        return "OFS"
    if event == "DIED" and remark.startswith("CERT"):
        return "OFS"
    if remark in SALES_TB_REMARKS:
        return "TB"
    return None


def _identity_is_beef_calf(identity) -> bool:
    if identity is None or _identity_is_ofs_or_tb(identity):
        return False
    try:
        lact = int(identity.lact) if identity.lact is not None else 0
    except (TypeError, ValueError):
        lact = 0
    if lact != 0:
        return False
    try:
        cbrd = int(identity.cbrd) if identity.cbrd is not None else None
    except (TypeError, ValueError):
        return False
    return cbrd is not None and cbrd >= BEEF_CBREED_MIN


def _identity_is_dairy_transfer(identity) -> bool:
    if identity is None:
        return False
    remark = (getattr(identity, "remark", None) or "").strip().upper()
    dest = (getattr(identity, "dest", None) or "").strip().upper()
    if not remark or remark != dest or remark not in HERD_FARM_OPTIONS:
        return False
    if (getattr(identity, "gndr", None) or "").strip().upper() != "F":
        return False
    try:
        lact = int(identity.lact) if identity.lact is not None else 0
    except (TypeError, ValueError):
        lact = 0
    if lact != 0:
        return False
    try:
        cbrd = int(identity.cbrd) if identity.cbrd is not None else None
    except (TypeError, ValueError):
        return False
    return cbrd is not None and cbrd < BEEF_CBREED_MIN


def _remittance_sales_reason(buyer: str | None, identity) -> str:
    prior = _identity_is_ofs_or_tb(identity) if identity is not None else None
    if prior:
        return prior
    if _identity_is_beef_calf(identity):
        return "Beef"
    remark = (getattr(identity, "remark", None) or "").strip().upper()
    if remark in SALES_BEEF_REMARKS:
        return "Beef"
    if remark in {"CAR18", "CAR19"} or _identity_is_dairy_transfer(identity):
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
    shown_farm_etags: set[tuple[str, str]],
    cow_id: str | None = None,
    etag_contains: str | None = None,
) -> list[dict[str, Any]]:
    """Show PDF rows that are not already listed as a sold event on that farm.

    A sold event on another herd supplies the cow ID, age, and gender. The row
    stays on the PDF farm. An ear tag that has already been archived as paid
    stays out of Active.
    """
    query = select(CattleSaleLine).where(CattleSaleLine.farm.in_(farms))
    search_id = _normalize_key_part(cow_id)
    etag_fragment = _compact_etag(etag_contains)
    if etag_fragment:
        query = query.where(_etag_contains(CattleSaleLine.etag, etag_fragment))
    elif search_id:
        raw_etags = {
            etag
            for etag in db.scalars(
                select(CowEvent.etag).where(
                    func.trim(CowEvent.cow_id) == search_id,
                    CowEvent.etag.isnot(None),
                )
            ).all()
            if etag
        }
        if not raw_etags:
            return []
        query = query.where(CattleSaleLine.etag.in_(raw_etags))
    else:
        if event_from is not None:
            query = query.where(CattleSaleLine.sale_date >= event_from)
        if event_to is not None:
            query = query.where(CattleSaleLine.sale_date <= event_to)
    sale_lines = list(db.scalars(query).all())
    reference_dates = [line.sale_date for line in sale_lines]
    reference_dates.extend(line.kill_date for line in sale_lines if line.kill_date is not None)
    sold_by_key = {}
    if reference_dates:
        sold_by_key = _load_sold_events(
            db,
            list(HERD_FARM_OPTIONS),
            {line.etag for line in sale_lines},
            min(reference_dates),
            max(reference_dates),
        )
        bonus_etags = {line.etag for line in sale_lines if _is_warrendale_bonus_line(line)}
        if bonus_etags:
            sold_by_key = _merge_sold_events(
                sold_by_key,
                _load_sold_events(
                    db,
                    list(HERD_FARM_OPTIONS),
                    bonus_etags,
                    min(reference_dates),
                    max(reference_dates),
                    bounded=False,
                ),
            )
    archived_etags = _archived_payment_etags(db) if sale_lines else set()
    extra: list[dict[str, Any]] = []
    for line in sale_lines:
        etag = normalize_etag(line.etag)
        if not etag or etag in archived_etags or (line.farm, etag) in shown_farm_etags:
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
        match, other_farm = _match_sale_to_sold_event(
            sold_by_key,
            farm=line.farm,
            etag=etag,
            sale_date=line.sale_date,
            kill_date=line.kill_date,
            max_days=None if _is_warrendale_bonus_line(line) else EVENT_MATCH_WINDOW_DAYS,
        )
        if search_id and _normalize_key_part(match.cow_id if match else None) != search_id:
            continue
        identity = match
        sales_reason = _remittance_sales_reason(line.buyer, identity)
        if reasons and sales_reason not in reasons:
            continue
        warrendale_split = sales_reason == "Beef" and _is_warrendale_line(line)
        bonus_gbp = (
            round(float(line.amount_gbp), 2)
            if warrendale_split and _is_warrendale_bonus(line.amount_gbp)
            else None
        )
        calf_amount = None if bonus_gbp is not None else (line.amount_gbp if has_sale_amount else None)
        extra.append(
            _row_to_dict(
                line.farm,
                identity.cow_id if identity else None,
                line.etag,
                buyer,
                event_date,
                sales_reason,
                identity.gndr if identity else None,
                identity.bdat if identity else None,
                amount_gbp=calf_amount,
                sale_rejected=rejected,
                has_sale_amount=has_sale_amount,
                bonus_gbp=bonus_gbp,
                warrendale_split=warrendale_split,
                confirm_part=_confirm_part(
                    warrendale_split=warrendale_split,
                    calf_amount=calf_amount,
                    bonus_amount=bonus_gbp,
                    amount_paid=False,
                    bonus_paid=False,
                ),
                dairycomp_farm=other_farm,
                farm_sold=other_farm or "",
                farm_paid=line.farm,
                documents=_documents_for_files(
                    (line.source_file, line.source_message_id)
                ),
            )
        )
    return extra


def _archived_payment_etags(db: Session) -> set[str]:
    """Ear tags whose sale has been archived as paid."""
    archived: set[str] = set()
    for etag in db.scalars(
        select(SalesPaymentRecord.etag).where(
            SalesPaymentRecord.archived_at.isnot(None),
            SalesPaymentRecord.etag.isnot(None),
        )
    ).all():
        normalized = normalize_etag(etag)
        if normalized:
            archived.add(normalized)
    return archived


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
    if status == "archived":
        min_date, max_date = db.execute(
            select(
                func.min(SalesPaymentRecord.event_date),
                func.max(SalesPaymentRecord.event_date),
            ).where(
                SalesPaymentRecord.archived_at.isnot(None),
                SalesPaymentRecord.farm.in_(farms),
            )
        ).one()
    else:
        bounds_query = select(
            func.min(CowEvent.event_date), func.max(CowEvent.event_date)
        ).select_from(CowEvent)
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
    cow_id: str | None = None,
    _live: bool = False,
) -> dict[str, Any]:
    selected_farms = normalize_farms(farms)
    if not selected_farms:
        return {"rows": [], "total": 0, "status": status, "date_bounds": None}

    search_text = _normalize_key_part(cow_id)
    exact_cow_id = bool(
        search_text
        and _exact_cow_id_in_view(
            db,
            status=status,
            farms=selected_farms,
            reasons=reasons,
            cow_id=search_text,
        )
    )
    etag_fragment = "" if exact_cow_id or not search_text else _compact_etag(search_text)

    if status == "archived" and not _live:
        return _list_archived_snapshots(
            db,
            farms=selected_farms,
            reasons=reasons,
            dest=dest,
            event_from=event_from,
            event_to=event_to,
            include_date_bounds=include_date_bounds,
            has_amount=has_amount,
            search_text=search_text,
            exact_cow_id=exact_cow_id,
            etag_fragment=etag_fragment,
        )

    reason_expr = _payment_sales_reason_expression()
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
        cow_id=search_text if exact_cow_id else None,
        etag_contains=etag_fragment or None,
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
                list(HERD_FARM_OPTIONS),
                min_event,
                max_event,
                bonus_etags={normalize_etag(row[2]) for row in result_rows if row[2]},
            )

    flag_min = None
    flag_max = None
    if result_rows:
        flag_dates = []
        for row in result_rows:
            event_date = row[3]
            if event_date is None:
                continue
            if hasattr(event_date, "date"):
                event_date = event_date.date()
            flag_dates.append(event_date)
        if flag_dates:
            flag_min = min(flag_dates)
            flag_max = max(flag_dates)
    payment_flags = _payment_flags_by_key(db, selected_farms, flag_min, flag_max)
    search_cow_id = search_text if exact_cow_id else ""

    rows = []
    for (
        farm,
        event_cow_id,
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
        calf_amount = None
        bonus_gbp = None
        sale_rejected = False
        has_sale_amount = False
        remittance_dest = ""
        warrendale_split = False
        sale_match = _sale_match_for_sold_event(
            sale_lines_by_key, farm, etag, event_date, sales_reason
        )
        if sale_match is None and _has_other_farm_remittance(
            sale_lines_by_key, farm, etag, event_date
        ):
            continue
        if sale_match is not None:
            calf_amount = sale_match.get("calf_amount_gbp")
            bonus_gbp = sale_match.get("bonus_gbp")
            sale_rejected = sale_match["sale_rejected"]
            has_sale_amount = sale_match["has_sale_amount"]
            remittance_dest = sale_match["buyer"]
            warrendale_split = bool(sale_match.get("warrendale_split"))
        record = payment_flags.get(
            _payment_record_key(farm, event_cow_id, etag, event_date)
        )
        amount_paid = bool(record and record.amount_paid_at)
        bonus_paid = bool(record and record.bonus_paid_at)
        if record and record.archived_at and not amount_paid and not bonus_paid and warrendale_split:
            amount_paid = calf_amount is not None or bonus_gbp is None
            bonus_paid = bonus_gbp is not None
        if warrendale_split:
            amount_gbp = _display_amount(calf_amount, bonus_gbp, amount_paid, bonus_paid)
        elif sale_match is not None:
            amount_gbp = sale_match["amount_gbp"]
        if has_amount is True and not has_sale_amount:
            continue
        if has_amount is False and has_sale_amount:
            continue
        if not _dests_match(remittance_dest, dest):
            continue
        rows.append(
            _row_to_dict(
                farm,
                event_cow_id,
                etag,
                remittance_dest,
                event_date,
                sales_reason,
                gndr,
                bdat,
                paid_at if paid_at is not None else (record.paid_at if record else None),
                archived_at if archived_at is not None else (record.archived_at if record else None),
                amount_gbp=amount_gbp,
                sale_rejected=sale_rejected,
                has_sale_amount=has_sale_amount,
                bonus_gbp=bonus_gbp,
                amount_paid=amount_paid,
                bonus_paid=bonus_paid,
                warrendale_split=warrendale_split,
                confirm_part=_confirm_part(
                    warrendale_split=warrendale_split,
                    calf_amount=calf_amount,
                    bonus_amount=bonus_gbp,
                    amount_paid=amount_paid,
                    bonus_paid=bonus_paid,
                ),
                farm_paid=farm if sale_match is not None else "",
                documents=_documents_for_files(
                    (
                        sale_match.get("source_file") if sale_match else None,
                        sale_match.get("source_message_id") if sale_match else None,
                    ),
                    (
                        sale_match.get("bonus_source_file") if sale_match else None,
                        sale_match.get("bonus_source_message_id") if sale_match else None,
                    ),
                ),
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
            shown_farm_etags={
                (row["farm"], normalize_etag(row["etag"])) for row in rows
            },
            cow_id=search_cow_id or None,
            etag_contains=etag_fragment or None,
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


def _encode_archive_snapshot(row: dict[str, Any]) -> str:
    return json.dumps(row)


def _decode_archive_snapshot(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        row = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(row, dict):
        return None
    return row


def _minimal_archive_snapshot(record: SalesPaymentRecord) -> dict[str, Any]:
    """Saved row when the sold event can no longer be rebuilt."""
    return _row_to_dict(
        record.farm,
        record.cow_id,
        record.etag,
        "",
        record.event_date,
        "",
        paid_at=record.paid_at,
        archived_at=record.archived_at,
        amount_paid=bool(record.amount_paid_at),
        bonus_paid=bool(record.bonus_paid_at),
    )


def _snapshot_from_live_row(row: dict[str, Any], record: SalesPaymentRecord) -> dict[str, Any]:
    """Freeze the row that was confirmed, with the paid ticks from this confirmation."""
    frozen = json.loads(json.dumps(row))
    warrendale = bool(frozen.get("warrendale_split"))
    amount_paid = bool(record.amount_paid_at)
    bonus_paid = bool(record.bonus_paid_at)
    if warrendale and not amount_paid and not bonus_paid:
        amount_paid = frozen.get("amount_gbp") is not None or frozen.get("bonus_gbp") is None
        bonus_paid = frozen.get("bonus_gbp") is not None
        amount = frozen.get("amount_gbp")
        bonus = frozen.get("bonus_gbp")
        if (
            amount is not None
            and bonus is not None
            and not frozen.get("amount_paid")
            and not frozen.get("bonus_paid")
        ):
            frozen["amount_gbp"] = round(float(amount) - float(bonus), 2)
    frozen["amount_paid"] = amount_paid
    frozen["bonus_paid"] = bonus_paid
    frozen["paid_at"] = record.paid_at.isoformat() if record.paid_at else None
    frozen["archived_at"] = record.archived_at.isoformat() if record.archived_at else None
    frozen["confirm_part"] = None
    return frozen


def _backfill_archive_snapshots(db: Session) -> None:
    """Save the current archive once, so rows confirmed before snapshots stay as they are now."""
    missing = db.scalars(
        select(SalesPaymentRecord).where(
            SalesPaymentRecord.archived_at.isnot(None),
            or_(
                SalesPaymentRecord.archive_snapshot.is_(None),
                SalesPaymentRecord.archive_snapshot == "",
            ),
        )
    ).all()
    if not missing:
        return
    live = list_sales_payments(
        db,
        status="archived",
        farms=list(HERD_FARM_OPTIONS),
        include_date_bounds=False,
        _live=True,
    )
    by_key: dict[tuple[str, str, str, dt.date], dict[str, Any]] = {}
    for row in live["rows"]:
        key = row.get("payment_key") or {}
        event_raw = key.get("event_date")
        if not event_raw:
            continue
        by_key[
            (
                key.get("farm") or "",
                _normalize_key_part(key.get("cow_id")),
                _normalize_key_part(key.get("etag")),
                dt.date.fromisoformat(event_raw),
            )
        ] = row
    for record in missing:
        key = _payment_record_key(record.farm, record.cow_id, record.etag, record.event_date)
        row = by_key.get(key) or _minimal_archive_snapshot(record)
        record.archive_snapshot = _encode_archive_snapshot(row)


def _archive_row_visible(
    row: dict[str, Any],
    *,
    reasons: list[str] | None,
    dest: str | None,
    event_from: dt.date | None,
    event_to: dt.date | None,
    has_amount: bool | None,
    search_text: str,
    exact_cow_id: bool,
    etag_fragment: str,
) -> bool:
    sales_reason = row.get("sales_reason") or ""
    if reasons:
        if sales_reason:
            if sales_reason not in reasons:
                return False
        elif set(reasons) < set(SALES_TABLE_REASON_ORDER):
            return False
    if not _dests_match(row.get("dest"), dest):
        return False
    has_sale_amount = bool(row.get("has_sale_amount"))
    if has_amount is True and not has_sale_amount:
        return False
    if has_amount is False and has_sale_amount:
        return False
    if exact_cow_id:
        return (row.get("cow_id") or "") == search_text
    if etag_fragment:
        return etag_fragment in _compact_etag(row.get("etag"))
    event_raw = row.get("event_date")
    if not event_raw:
        return False
    event_date = dt.date.fromisoformat(event_raw)
    if event_from is not None and event_date < event_from:
        return False
    if event_to is not None and event_date > event_to:
        return False
    return True


def _list_archived_snapshots(
    db: Session,
    *,
    farms: list[str],
    reasons: list[str] | None,
    dest: str | None,
    event_from: dt.date | None,
    event_to: dt.date | None,
    include_date_bounds: bool,
    has_amount: bool | None,
    search_text: str,
    exact_cow_id: bool,
    etag_fragment: str,
) -> dict[str, Any]:
    _backfill_archive_snapshots(db)
    records = db.scalars(
        select(SalesPaymentRecord).where(
            SalesPaymentRecord.archived_at.isnot(None),
            SalesPaymentRecord.farm.in_(farms),
        )
    ).all()
    rows: list[dict[str, Any]] = []
    for record in records:
        row = _decode_archive_snapshot(record.archive_snapshot)
        if row is None:
            row = _minimal_archive_snapshot(record)
            record.archive_snapshot = _encode_archive_snapshot(row)
        if not _archive_row_visible(
            row,
            reasons=reasons,
            dest=dest,
            event_from=event_from,
            event_to=event_to,
            has_amount=has_amount,
            search_text=search_text,
            exact_cow_id=exact_cow_id,
            etag_fragment=etag_fragment,
        ):
            continue
        rows.append(row)
    rows.sort(key=lambda row: (row.get("event_date") or "", row.get("dest") or "", row.get("etag") or ""))
    date_bounds = None
    if include_date_bounds:
        date_bounds = _compute_date_bounds(
            db,
            status="archived",
            farms=farms,
            reasons=reasons,
        )
    return {"rows": rows, "total": len(rows), "status": "archived", "date_bounds": date_bounds}


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


def _record_for_confirm(
    db: Session,
    existing: dict[tuple[str, str, str, dt.date], SalesPaymentRecord],
    farm: str,
    cow_id: str,
    etag: str,
    event_date: dt.date,
    user: User,
) -> SalesPaymentRecord:
    key = _payment_record_key(farm, cow_id, etag, event_date)
    record = existing.get(key)
    if record is None:
        record = SalesPaymentRecord(
            farm=farm,
            cow_id=cow_id,
            etag=etag,
            event_date=event_date,
            confirmed_by_user_id=user.id,
        )
        db.add(record)
        existing[key] = record
    else:
        record.confirmed_by_user_id = user.id
    return record


def _parse_edit_money(value: Any) -> float | None:
    text = str(value or "").strip().replace("£", "").replace(",", "")
    if not text:
        return None
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def _edited_document(label: str | None, existing: list[dict[str, Any]]) -> dict[str, str] | None:
    text = (label or "").strip()
    if not text:
        return None
    for doc in existing:
        if (doc.get("label") or "").strip().upper() != text.upper():
            continue
        kept = {"label": doc.get("label") or text, "file": doc.get("file") or text}
        if doc.get("message_id"):
            kept["message_id"] = str(doc["message_id"])
        return kept
    return {"label": text, "file": text}


def _apply_payment_edit(row: dict[str, Any], edit: dict[str, Any]) -> dict[str, Any]:
    """Use the popup figures on the saved archive row."""
    farm_paid = (edit.get("farm_paid") or "").strip().upper()
    if farm_paid in HERD_FARM_OPTIONS:
        row["farm_paid"] = farm_paid
    sold_name = (row.get("farm_sold") or "").strip()
    paid_name = (row.get("farm_paid") or "").strip()
    row["farms_match"] = bool(sold_name) and sold_name.upper() == paid_name.upper()
    row["dest"] = (edit.get("dest") or "").strip()[:128]
    amount = _parse_edit_money(edit.get("amount_gbp"))
    bonus = _parse_edit_money(edit.get("bonus_gbp"))
    row["amount_gbp"] = amount
    row["bonus_gbp"] = bonus
    row["has_sale_amount"] = amount is not None or bonus is not None or bool(row.get("sale_rejected"))
    existing = row.get("documents") if isinstance(row.get("documents"), list) else []
    documents: list[dict[str, str]] = []
    amount_doc = _edited_document(edit.get("amount_document"), existing)
    bonus_doc = _edited_document(edit.get("bonus_document"), existing)
    if amount_doc:
        documents.append(amount_doc)
    if bonus_doc and (amount_doc is None or bonus_doc["label"].upper() != amount_doc["label"].upper()):
        documents.append(bonus_doc)
    row["documents"] = documents
    row["note"] = (edit.get("note") or "").strip()[:500]
    if bonus is not None:
        row["warrendale_split"] = True
        row["amount_paid"] = amount is not None
        row["bonus_paid"] = True
    else:
        row["warrendale_split"] = False
        row["amount_paid"] = False
        row["bonus_paid"] = False
    row["confirm_part"] = None
    return row


def confirm_payments(
    db: Session,
    items: list[dict[str, Any]],
    user: User,
) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    existing = _load_payment_records_for_items(db, items)
    confirmed = 0
    to_freeze: list[SalesPaymentRecord] = []
    edits: dict[tuple[str, str, str, dt.date], dict[str, Any]] = {}
    for item in items:
        farm, cow_id, etag, event_date = _parse_payment_item(item)
        record = _record_for_confirm(db, existing, farm, cow_id, etag, event_date, user)
        already_archived = record.archived_at is not None
        part = (item.get("part") or "").strip().lower()
        archive_now = False
        if part == "amount":
            record.amount_paid_at = now
        elif part == "bonus":
            record.bonus_paid_at = now
        elif part == "both":
            record.amount_paid_at = now
            record.bonus_paid_at = now
            archive_now = True
        else:
            archive_now = True
        if part in {"amount", "bonus"} and record.amount_paid_at and record.bonus_paid_at:
            archive_now = True
        confirmed += 1
        if archive_now and not already_archived:
            to_freeze.append(record)
            edit = item.get("edit")
            if isinstance(edit, dict):
                edits[_payment_record_key(farm, cow_id, etag, event_date)] = edit
    if to_freeze:
        _save_archive_snapshots(db, to_freeze, now, edits)
    return {"confirmed": confirmed}


def _save_archive_snapshots(
    db: Session,
    records: list[SalesPaymentRecord],
    now: dt.datetime,
    edits: dict[tuple[str, str, str, dt.date], dict[str, Any]] | None = None,
) -> None:
    """Store the row as it stands, then mark it archived."""
    farms = sorted({record.farm for record in records if record.farm})
    live = list_sales_payments(
        db,
        status="active",
        farms=farms,
        include_date_bounds=False,
    )
    by_key: dict[tuple[str, str, str, dt.date], dict[str, Any]] = {}
    for row in live["rows"]:
        key = row.get("payment_key") or {}
        event_raw = key.get("event_date")
        if not event_raw:
            continue
        by_key[
            (
                key.get("farm") or "",
                _normalize_key_part(key.get("cow_id")),
                _normalize_key_part(key.get("etag")),
                dt.date.fromisoformat(event_raw),
            )
        ] = row
    for record in records:
        record.paid_at = now
        record.archived_at = now
        record.unarchived_at = None
        key = _payment_record_key(record.farm, record.cow_id, record.etag, record.event_date)
        row = by_key.get(key) or _minimal_archive_snapshot(record)
        frozen = _snapshot_from_live_row(row, record)
        edit = (edits or {}).get(key)
        if edit:
            frozen = _apply_payment_edit(frozen, edit)
            if frozen.get("bonus_paid"):
                record.bonus_paid_at = now
            if frozen.get("amount_paid"):
                record.amount_paid_at = now
        record.archive_snapshot = _encode_archive_snapshot(frozen)


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
        record.amount_paid_at = None
        record.bonus_paid_at = None
        record.archive_snapshot = None
        record.unarchived_at = now
        record.confirmed_by_user_id = user.id
        restored += 1
    return {"restored": restored}
