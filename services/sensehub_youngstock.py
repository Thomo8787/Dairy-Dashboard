"""Young-stock health index history from SenseHub, joined to DairyComp."""

from __future__ import annotations

import datetime as dt
import re
import threading
from collections import defaultdict
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from services.database import (
    CowEvent,
    HerdInventory,
    SenseHubCalfAssignment,
    SenseHubReportSnapshot,
    SenseHubYoungstockHealth,
)
from services.events_common import (
    filter_disease_episode_records,
    is_loxicom_only_remark,
)
from services.sensehub_api import (
    DEFAULT_REPORT,
    HERD_REPORT,
    NO_DATA_REPORT,
    SenseHubError,
    animal_list_as_report,
    assign_sensehub_monitoring_tag,
    compact_report_name,
    create_sensehub_calf,
    cull_sensehub_animals,
    days_with_assigned_tag,
    fetch_named_reports,
    fetch_report,
    flatten_report,
    is_herd_report,
    list_reports,
    list_sensehub_animals,
    list_untagged_sensehub_animals,
    login,
    parse_no_data_rows,
)

_UK = ZoneInfo("Europe/London")
_DIGIT_RE = re.compile(r"\d")
SLOTS: tuple[tuple[int, str], ...] = (
    (0, "midnight"),
    (6, "6am"),
    (12, "midday"),
    (18, "6pm"),
)
LIVE_SLOT = "live"
PERMANENT_HOURS = {hour for hour, _name in SLOTS}
SLOT_LABELS = {
    "midnight": "Midnight",
    "6am": "6am",
    "midday": "Midday",
    "6pm": "6pm",
}
DEFAULT_THRESHOLD = 86.0
ALL_CALVES_THRESHOLD = 100.0
RECENTLY_TREATED_DAYS = 7
MAX_BACKFILL_DAYS = 730
EMPTY_SLOT_STOP = 28
TREND_DOTS = 12
CHART_EVENT_LETTERS: dict[str, str] = {
    "PNEU": "P",
    "SCOURS": "S",
    "SCOUR": "S",
    "NAVAL": "N",
    "NAVEL": "N",
    "VACC": "V",
}
TREATMENT_EVENTS = frozenset({"PNEU", "SCOURS", "SCOUR", "NAVAL", "NAVEL"})
LAST_TREATMENT_EVENTS = frozenset({"PNEU", "SCOURS", "SCOUR", "NAVAL", "NAVEL"})


def health_band(value: float | None) -> str | None:
    if value is None:
        return None
    if value < 80:
        return "red"
    if value < 85:
        return "yellow"
    if value < 90:
        return "blue"
    return "green"


def trend_dots(indexes: list[float | None]) -> list[dict[str, Any]]:
    recent = list(indexes[-TREND_DOTS:])
    padded: list[float | None] = [None] * (TREND_DOTS - len(recent)) + recent
    return [{"health_index": value, "band": health_band(value)} for value in padded]


def strip_sensehub_remark(raw: Any) -> str:
    """Keep the cow ID and drop SenseHub name remarks.

    SenseHub stores many animals as ``535666 - PT`` or ``535666 - Pen 12``.
    Digits in the remark must not be mixed into the ID.
    """
    text = str(raw or "").strip()
    if " - " in text:
        text = text.split(" - ", 1)[0].strip()
    return text


def normalize_animal_id(raw: Any) -> str | None:
    """Keep the first six digits of a SenseHub animal ID; drop letters and the rest."""
    digits = "".join(
        ch for ch in strip_sensehub_remark(raw).replace(" ", "") if ch.isdigit()
    )
    if not digits:
        return None
    return digits[:6]


def etag4(raw: Any) -> str | None:
    """Last four digits of an ID, after stripping spaces."""
    digits = "".join(ch for ch in str(raw or "").replace(" ", "") if ch.isdigit())
    if not digits:
        return None
    return digits[-4:]


def _event_code(name: Any) -> str:
    return str(name or "").strip().upper()


def _event_date_iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    text = str(value)
    return text[:10] if text else None


def _is_counted_treatment_event(event: CowEvent) -> bool:
    code = _event_code(event.event)
    if code not in TREATMENT_EVENTS:
        return False
    if code == "PNEU" and is_loxicom_only_remark(event.remark):
        return False
    return True


def treatment_episodes(events: list[CowEvent]) -> list[dict[str, Any]]:
    """PNEU, SCOURS, and NAVAL events after the shared disease episode gap."""
    records = [
        {
            "cow_id": event.cow_id,
            "etag": event.etag,
            "event": _event_code(event.event),
            "event_date": event.event_date,
            "farm": event.farm,
        }
        for event in events
        if _is_counted_treatment_event(event)
    ]
    return filter_disease_episode_records(records)


def days_since_last_treatment(
    events: list[CowEvent],
    *,
    today: dt.date | None = None,
) -> int | None:
    """Days since the most recent PNEU, SCOURS, or NAVAL event."""
    today = today or dt.date.today()
    latest: dt.date | None = None
    for event in events:
        if _event_code(event.event) not in LAST_TREATMENT_EVENTS:
            continue
        if _event_code(event.event) == "PNEU" and is_loxicom_only_remark(event.remark):
            continue
        if event.event_date is None:
            continue
        if latest is None or event.event_date > latest:
            latest = event.event_date
    if latest is None:
        return None
    return (today - latest).days


def _events_for_animals(
    db: Session,
    animals: list[tuple[str, HerdInventory | None]],
) -> dict[str, list[CowEvent]]:
    cow_ids: set[str] = set()
    etags: set[str] = set()
    for animal_id, inventory in animals:
        cow_ids.add(animal_id)
        if inventory is None:
            continue
        if inventory.cow_id:
            cow_ids.add(inventory.cow_id)
        if inventory.etag:
            etags.add(inventory.etag)
    if not cow_ids and not etags:
        return {animal_id: [] for animal_id, _inventory in animals}
    filters = []
    if cow_ids:
        filters.append(CowEvent.cow_id.in_(cow_ids))
    if etags:
        filters.append(CowEvent.etag.in_(etags))
    events = list(db.scalars(select(CowEvent).where(or_(*filters))).all())
    by_cow: dict[str, list[CowEvent]] = defaultdict(list)
    by_etag: dict[str, list[CowEvent]] = defaultdict(list)
    for event in events:
        if event.cow_id:
            by_cow[event.cow_id].append(event)
        if event.etag:
            by_etag[event.etag].append(event)
    result: dict[str, list[CowEvent]] = {}
    for animal_id, inventory in animals:
        seen: set[int] = set()
        collected: list[CowEvent] = []
        keys = {animal_id}
        if inventory is not None and inventory.cow_id:
            keys.add(inventory.cow_id)
        for key in keys:
            for event in by_cow.get(key, []):
                if event.id not in seen:
                    seen.add(event.id)
                    collected.append(event)
        if inventory is not None and inventory.etag:
            for event in by_etag.get(inventory.etag, []):
                if event.id not in seen:
                    seen.add(event.id)
                    collected.append(event)
        birth = inventory.bdat if inventory is not None else None
        if birth is not None:
            collected = [
                event
                for event in collected
                if event.event_date is None or event.event_date >= birth
            ]
        result[animal_id] = collected
    return result


def treatment_counts(events: list[CowEvent]) -> dict[str, int]:
    """Count pneumonia, scours, and naval episodes, not repeat treatments."""
    counts = {"resp_count": 0, "scours_count": 0, "naval_count": 0}
    for record in treatment_episodes(events):
        code = record["event"]
        if code == "PNEU":
            counts["resp_count"] += 1
        elif code in {"SCOURS", "SCOUR"}:
            counts["scours_count"] += 1
        elif code in {"NAVAL", "NAVEL"}:
            counts["naval_count"] += 1
    return counts


_DRAXXIN_RE = re.compile(r"\bDRAXX?IN\b", re.IGNORECASE)
_FENFLOR_RE = re.compile(r"\bFENFLOR\b", re.IGNORECASE)
DRAXXIN_HIGHLIGHT_DAYS = 7
FENFLOR_HIGHLIGHT_DAYS = 2


def _event_drug_text(event: CowEvent) -> str:
    return f"{event.remark or ''} {event.protocols or ''}"


def recent_antibiotic_highlight(
    events: list[CowEvent],
    *,
    today: dt.date | None = None,
) -> str | None:
    """Return 'draxxin' or 'fenflor' when a recent dose should tint the row green."""
    today = today or dt.date.today()
    found: set[str] = set()
    for event in events:
        if event.event_date is None:
            continue
        days = (today - event.event_date).days
        if days < 0:
            continue
        text = _event_drug_text(event)
        if days < DRAXXIN_HIGHLIGHT_DAYS and _DRAXXIN_RE.search(text):
            found.add("draxxin")
        if days < FENFLOR_HIGHLIGHT_DAYS and _FENFLOR_RE.search(text):
            found.add("fenflor")
    if "draxxin" in found:
        return "draxxin"
    if "fenflor" in found:
        return "fenflor"
    return None


def chart_event_markers(events: list[CowEvent]) -> list[dict[str, str]]:
    """One P/S/N/V icon for every event date. Counts still use the episode gap."""
    seen: set[tuple[str, str]] = set()
    markers: list[dict[str, str]] = []
    for event in events:
        code = _event_code(event.event)
        letter = CHART_EVENT_LETTERS.get(code)
        date_str = _event_date_iso(event.event_date)
        if not letter or not date_str:
            continue
        key = (date_str, letter)
        if key in seen:
            continue
        seen.add(key)
        markers.append({"date": date_str, "letter": letter, "event": code})
    markers.sort(key=lambda item: (item["date"], item["letter"]))
    return markers


def dairycomp_age_days(inventory: HerdInventory | None) -> int | None:
    """Age in days from DairyComp inventory (AGED, else birth date)."""
    if inventory is None:
        return None
    if inventory.aged is not None:
        return int(inventory.aged)
    if inventory.bdat is not None:
        return (dt.date.today() - inventory.bdat).days
    return None


def sample_slot(when: dt.datetime | None = None) -> tuple[dt.datetime, str]:
    """Map a timestamp to the locked UK slot: midnight, 6am, midday, 6pm."""
    now = when or dt.datetime.now(_UK)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_UK)
    else:
        now = now.astimezone(_UK)
    hour = now.hour
    slot_hour, slot_name = SLOTS[0]
    for start, name in SLOTS:
        if hour >= start:
            slot_hour, slot_name = start, name
    sampled = now.replace(hour=slot_hour, minute=0, second=0, microsecond=0, tzinfo=None)
    return sampled, slot_name


def current_hour(when: dt.datetime | None = None) -> dt.datetime:
    """UK local time rounded down to the current hour, stored naive."""
    now = when or dt.datetime.now(_UK)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_UK)
    else:
        now = now.astimezone(_UK)
    return now.replace(minute=0, second=0, microsecond=0, tzinfo=None)


def reading_target(when: dt.datetime | None = None) -> tuple[dt.datetime, str]:
    """Permanent 6-hour slot at 0/6/12/18, otherwise a live hourly reading."""
    hour_ts = current_hour(when)
    if hour_ts.hour in PERMANENT_HOURS:
        return sample_slot(when)
    return hour_ts, LIVE_SLOT


def hour_clock_label(hour: int) -> str:
    if hour == 0:
        return "midnight"
    if hour == 12:
        return "midday"
    if hour < 12:
        return f"{hour}am"
    return f"{hour - 12}pm"


def sample_label(sampled_at: dt.datetime | None, slot: str | None) -> str:
    if sampled_at is None:
        return slot or ""
    clock = hour_clock_label(sampled_at.hour) if slot == LIVE_SLOT else SLOT_LABELS.get(slot or "", slot or "")
    return f"{sampled_at:%d %b} {clock}".strip()


def _clear_live_rows(db: Session) -> None:
    db.execute(delete(SenseHubYoungstockHealth).where(SenseHubYoungstockHealth.slot == LIVE_SLOT))


def save_current_reading(
    db: Session,
    rows: list[dict[str, Any]],
    *,
    when: dt.datetime | None = None,
) -> tuple[dt.datetime, str, int]:
    """Save the latest SenseHub pull as live (overwritten hourly) or a locked 6-hour slot."""
    sampled, slot = reading_target(when)
    _clear_live_rows(db)
    saved = save_rows(db, rows, sampled_at=sampled, slot=slot)
    return sampled, slot, saved


def past_slots(
    days: int,
    *,
    now: dt.datetime | None = None,
) -> list[tuple[dt.datetime, str, int]]:
    """UK midnight/6am/midday/6pm slots in the last `days` days, excluding the future."""
    current = now or dt.datetime.now(_UK)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_UK)
    else:
        current = current.astimezone(_UK)
    slots: list[tuple[dt.datetime, str, int]] = []
    start_day = current.date() - dt.timedelta(days=max(0, days))
    day = start_day
    while day <= current.date():
        for hour, name in SLOTS:
            sampled_uk = dt.datetime(day.year, day.month, day.day, hour, tzinfo=_UK)
            if sampled_uk >= current:
                continue
            naive = sampled_uk.replace(tzinfo=None)
            slots.append((naive, name, int(sampled_uk.timestamp())))
        day += dt.timedelta(days=1)
    return slots


def slots_to_fetch(
    all_slots: list[tuple[dt.datetime, str, int]],
    existing: set[dt.datetime],
    *,
    catch_up: bool,
    current: dt.datetime | None = None,
) -> list[tuple[dt.datetime, str, int]]:
    """Newest-first slots to pull from SenseHub.

    Catch-up keeps the current slot (even if already stored) then walks back
    through missing times until it meets already-saved history. A full fill
    requests every missing slot in the span.
    """
    newest_first = list(reversed(all_slots))
    if not catch_up:
        return [item for item in newest_first if item[0] not in existing]
    pending: list[tuple[dt.datetime, str, int]] = []
    for item in newest_first:
        sampled = item[0]
        if sampled in existing and (current is None or sampled != current):
            break
        pending.append(item)
    return pending


def backfill_span_days(db: Session, *, now: dt.datetime | None = None) -> int:
    """Days back to the oldest current youngstock animal's birth / SenseHub age."""
    current = now or dt.datetime.now(_UK)
    if current.tzinfo is None:
        today = current.date()
    else:
        today = current.astimezone(_UK).date()
    max_age = int(db.scalar(select(func.max(SenseHubYoungstockHealth.age_days))) or 0)
    latest = db.scalar(select(func.max(SenseHubYoungstockHealth.sampled_at)))
    animal_ids: list[str] = []
    if latest is not None:
        animal_ids = list(
            db.scalars(
                select(SenseHubYoungstockHealth.animal_id).where(
                    SenseHubYoungstockHealth.sampled_at == latest
                )
            ).all()
        )
    by_cow, by_tag = _inventory_indexes(db)
    earliest_birth: dt.date | None = None
    for animal_id in animal_ids:
        inventory = match_inventory(animal_id, by_cow, by_tag)
        age = dairycomp_age_days(inventory)
        if age:
            max_age = max(max_age, int(age))
        if inventory is not None and inventory.bdat is not None:
            if earliest_birth is None or inventory.bdat < earliest_birth:
                earliest_birth = inventory.bdat
    if earliest_birth is not None:
        max_age = max(max_age, (today - earliest_birth).days)
    if max_age <= 0:
        max_age = 365
    return max(1, min(max_age, MAX_BACKFILL_DAYS))


_job_lock = threading.Lock()
_job_status: dict[str, Any] = {
    "status": "idle",
    "message": "",
    "slots_done": 0,
    "slots_total": 0,
}


def get_youngstock_job_status() -> dict[str, Any]:
    with _job_lock:
        return dict(_job_status)


def _set_job(**kwargs: Any) -> None:
    with _job_lock:
        _job_status.update(kwargs)


def is_youngstock_job_running() -> bool:
    with _job_lock:
        return _job_status.get("status") == "running"


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    if number is None:
        return None
    return int(number)


def _digit_keys(value: str | None) -> set[str]:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if not digits:
        return set()
    keys = {digits}
    keys.add(digits[:6] if len(digits) >= 6 else digits)
    if len(digits) >= 6:
        keys.add(digits[-6:])
    return keys


def _scr_id_keys(value: str | None) -> set[str]:
    """IDs used to join DairyComp to SenseHub: full digits, and last 6 of a UK tag.

    SenseHub names like ``535666 - PT`` are reduced to the leading cow ID first,
    so digits in the remark are ignored. Do not use the first six digits of a
    12-digit official tag: that is the herd number and would falsely mark calves
    as already on SenseHub.
    """
    digits = "".join(ch for ch in strip_sensehub_remark(value) if ch.isdigit())
    if not digits:
        return set()
    if len(digits) <= 6:
        return {digits}
    return {digits, digits[-6:]}


def save_rows(
    db: Session,
    rows: list[dict[str, Any]],
    *,
    sampled_at: dt.datetime,
    slot: str,
) -> int:
    """Upsert one sample slot of young-stock health rows."""
    saved = 0
    for row in rows:
        animal_id = normalize_animal_id(
            row.get("AnimalID") or row.get("animal_id") or row.get("raw_animal_id")
        )
        if not animal_id:
            continue
        health_index = _to_float(row.get("YoungStockHealthIndex") or row.get("health_index"))
        existing = db.scalar(
            select(SenseHubYoungstockHealth).where(
                SenseHubYoungstockHealth.animal_id == animal_id,
                SenseHubYoungstockHealth.sampled_at == sampled_at,
            )
        )
        raw_id = str(row.get("AnimalID") or row.get("raw_animal_id") or animal_id)
        age_days = _to_int(row.get("AgeInDays") or row.get("age_days"))
        rumination = _to_float(row.get("DailyRumination") or row.get("rumination"))
        eating = _to_float(row.get("DailyEatingTime") or row.get("eating"))
        group_name = row.get("CowGroupName") or row.get("GroupName") or row.get("group_name")
        if existing:
            existing.raw_animal_id = raw_id
            existing.slot = slot
            existing.health_index = health_index
            existing.age_days = age_days
            existing.rumination = rumination
            existing.eating = eating
            existing.group_name = str(group_name) if group_name else None
        else:
            db.add(
                SenseHubYoungstockHealth(
                    animal_id=animal_id,
                    raw_animal_id=raw_id,
                    sampled_at=sampled_at,
                    slot=slot,
                    health_index=health_index,
                    age_days=age_days,
                    rumination=rumination,
                    eating=eating,
                    group_name=str(group_name) if group_name else None,
                )
            )
        saved += 1
    return saved


def save_from_reports(
    db: Session,
    reports: list[dict[str, Any]],
    *,
    sampled_at: dt.datetime | None = None,
) -> int:
    saved = 0
    for report in reports:
        name = str(report.get("report_name") or "")
        if name.casefold() != DEFAULT_REPORT.casefold():
            continue
        _sampled, _slot, count = save_current_reading(
            db,
            list(report.get("rows") or []),
            when=sampled_at,
        )
        saved += count
    return saved


def seed_from_latest_snapshot(db: Session) -> int:
    """If history is empty, copy the latest imported Young Stock Health by Age All report."""
    existing = db.scalar(select(func.count()).select_from(SenseHubYoungstockHealth)) or 0
    if existing:
        return 0
    snapshot = db.scalar(
        select(SenseHubReportSnapshot).where(
            SenseHubReportSnapshot.report_name == DEFAULT_REPORT
        )
    )
    if snapshot is None:
        return 0
    payload = snapshot.payload or {}
    sampled, slot = sample_slot(snapshot.fetched_at)
    saved = save_rows(
        db,
        list(payload.get("rows") or []),
        sampled_at=sampled,
        slot=slot,
    )
    if saved:
        db.commit()
    return saved


def _pending_report_snapshots(db: Session) -> list[SenseHubReportSnapshot]:
    return [
        item
        for item in db.new
        if isinstance(item, SenseHubReportSnapshot)
    ]


def _snapshot_by_report_key(db: Session, report_key: int) -> SenseHubReportSnapshot | None:
    for item in _pending_report_snapshots(db):
        if item.report_key == report_key:
            return item
    return db.scalar(
        select(SenseHubReportSnapshot).where(
            SenseHubReportSnapshot.report_key == report_key
        )
    )


def _upsert_report_snapshot(
    db: Session,
    report: dict[str, Any],
    *,
    fetched_at: dt.datetime,
    farm_id: str | None = None,
    farm_name: str | None = None,
    software_version: str | None = None,
) -> None:
    rows = report.get("rows") or []
    payload = {
        "columns": report.get("columns") or [],
        "rows": rows,
        "report_time": report.get("report_time"),
        "farm_id": farm_id,
        "farm_name": farm_name,
        "software_version": software_version,
    }
    report_key = int(report["report_key"])
    report_name = str(report["report_name"])
    existing = _snapshot_by_report_key(db, report_key)
    if existing is None:
        existing = next(
            (
                item
                for item in (*_pending_report_snapshots(db), *db.scalars(select(SenseHubReportSnapshot)).all())
                if is_herd_report(item.report_name) and is_herd_report(report_name)
            ),
            None,
        )
    title = str(report.get("title") or report_name)
    row_count = int(report.get("row_count") or len(rows))
    category = report.get("category")
    if existing:
        existing.report_key = report_key
        existing.report_name = report_name
        existing.category = category
        existing.title = title
        existing.row_count = row_count
        existing.payload = payload
        existing.fetched_at = fetched_at
        return
    db.add(
        SenseHubReportSnapshot(
            report_key=report_key,
            report_name=report_name,
            category=category,
            title=title,
            row_count=row_count,
            payload=payload,
            fetched_at=fetched_at,
        )
    )


def refresh_sensehub_list_snapshots(
    db: Session,
    *,
    fetched_at: dt.datetime | None = None,
    farm_id: str | None = None,
    farm_name: str | None = None,
    software_version: str | None = None,
) -> dict[str, int]:
    """Refresh stored Animals in Herd, No Data, and Young Stock Health lists."""
    fetched_at = fetched_at or dt.datetime.now()
    result = {"herd_saved": 0, "no_data_saved": 0, "youngstock_saved": 0}
    try:
        animals = list_sensehub_animals()
        if animals:
            _upsert_report_snapshot(
                db,
                animal_list_as_report(animals),
                fetched_at=fetched_at,
                farm_id=farm_id,
                farm_name=farm_name,
                software_version=software_version,
            )
            result["herd_saved"] = len(animals)
    except SenseHubError:
        pass
    try:
        payload = fetch_named_reports([NO_DATA_REPORT])
        for report in payload.get("reports") or []:
            if compact_report_name(report.get("report_name")) != compact_report_name(
                NO_DATA_REPORT
            ):
                continue
            _upsert_report_snapshot(
                db,
                report,
                fetched_at=fetched_at,
                farm_id=payload.get("farm_id") or farm_id,
                farm_name=payload.get("farm_name") or farm_name,
                software_version=payload.get("software_version") or software_version,
            )
            result["no_data_saved"] = len(report.get("rows") or [])
    except SenseHubError:
        pass
    try:
        payload = fetch_named_reports([DEFAULT_REPORT])
        for report in payload.get("reports") or []:
            name = compact_report_name(report.get("report_name"))
            title = compact_report_name(report.get("title"))
            expected = compact_report_name(DEFAULT_REPORT)
            if name != expected and title != expected:
                continue
            _upsert_report_snapshot(
                db,
                report,
                fetched_at=fetched_at,
                farm_id=payload.get("farm_id") or farm_id,
                farm_name=payload.get("farm_name") or farm_name,
                software_version=payload.get("software_version") or software_version,
            )
            result["youngstock_saved"] = len(report.get("rows") or [])
    except SenseHubError:
        pass
    return result


def refresh_tags_to_remove_data(db: Session) -> dict[str, Any]:
    """Refresh Animals in Herd, No Data and Young Stock Health, then auto-cull sold/died."""
    lists = refresh_sensehub_list_snapshots(db)
    auto_culled = 0
    try:
        auto = auto_cull_exited_sensehub_animals(db)
        auto_culled = int(auto.get("culled") or 0)
    except Exception:
        auto_culled = 0
    db.commit()
    return {
        "herd_saved": int(lists.get("herd_saved") or 0),
        "no_data_saved": int(lists.get("no_data_saved") or 0),
        "auto_culled": auto_culled,
    }


def _import_current_slot(db: Session) -> dict[str, Any]:
    payload = fetch_named_reports([DEFAULT_REPORT])
    reports = payload.get("reports") or []
    rows: list[dict[str, Any]] = []
    fetched_at = dt.datetime.now()
    for report in reports:
        name = str(report.get("report_name") or "")
        if name.casefold() == DEFAULT_REPORT.casefold():
            rows.extend(list(report.get("rows") or []))
        if is_herd_report(name):
            _upsert_report_snapshot(
                db,
                report,
                fetched_at=fetched_at,
                farm_id=payload.get("farm_id"),
                farm_name=payload.get("farm_name"),
                software_version=payload.get("software_version"),
            )
    lists = refresh_sensehub_list_snapshots(
        db,
        fetched_at=fetched_at,
        farm_id=payload.get("farm_id"),
        farm_name=payload.get("farm_name"),
        software_version=payload.get("software_version"),
    )
    sampled, slot, saved = save_current_reading(db, rows)
    db.commit()
    return {
        "saved": saved,
        "sampled_at": sampled.isoformat(),
        "slot": slot,
        "farm_name": payload.get("farm_name"),
        "herd_saved": lists["herd_saved"],
        "no_data_saved": lists["no_data_saved"],
    }


def import_youngstock_health(db: Session) -> dict[str, Any]:
    """Fetch Young Stock Health, refresh Animals in Herd, then fill missing slots.

    Hours 0, 6, 12 and 18 are stored permanently. Other hours overwrite the live
    reading so the latest table icon and graph bar stay current. Animals in Herd
    is stored as a report snapshot so newly tagged calves match before they have
    health data.
    """
    result = _import_current_slot(db)
    filled = backfill_youngstock_health(db, days=None, catch_up=False)
    result["saved"] = int(result.get("saved") or 0) + int(filled.get("saved") or 0)
    try:
        auto = auto_cull_exited_sensehub_animals(db)
        result["auto_culled"] = int(auto.get("culled") or 0)
    except Exception:
        result["auto_culled"] = 0
    return result


def backfill_youngstock_health(
    db: Session,
    *,
    days: int | None = None,
    catch_up: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Re-run Young Stock Health by Age All at past UK sample slots.

    When `days` is None, walk back to the oldest current calf's birth (or SenseHub
    age). Slots already stored are skipped unless `force` is set. Force re-downloads
    every slot so animals whose SenseHub ID changed still get history under the new
    ID. Catch-up mode fetches the current slot, then only missing older ones until
    saved history is reached.
    """
    if db.scalar(select(func.count()).select_from(SenseHubYoungstockHealth)) == 0:
        try:
            _import_current_slot(db)
        except Exception:
            db.rollback()
    span = days if days is not None else backfill_span_days(db)
    all_slots = past_slots(span)
    existing: set[dt.datetime] = set()
    if not force:
        existing = set(
            db.scalars(select(SenseHubYoungstockHealth.sampled_at).distinct()).all()
        )
    current, _slot_name = sample_slot()
    slots = slots_to_fetch(
        all_slots,
        existing,
        catch_up=catch_up,
        current=current if catch_up else None,
    )
    stop_on_empty = days is None
    _set_job(
        status="running",
        message=(
            f"Logging in to SenseHub to "
            f"{'re-download' if force else 'fill'} {len(slots)} "
            f"{'time slots' if force else 'missing slots'} "
            f"(up to {span} days)…"
        ),
        slots_done=0,
        slots_total=len(slots),
    )
    saved_total = 0
    errors: list[str] = []
    empty_streak = 0
    fetched = 0
    try:
        with httpx.Client(timeout=90.0, follow_redirects=True) as client:
            token, version = login(client)
            catalog = list_reports(client, token)
            item = next(
                (
                    entry
                    for entry in catalog
                    if str(entry.get("name") or "").casefold() == DEFAULT_REPORT.casefold()
                ),
                None,
            )
            if item is None or item.get("key") is None:
                raise SenseHubError(f"SenseHub catalogue has no {DEFAULT_REPORT}.")
            key = int(item["key"])
            for index, (sampled, slot, unix) in enumerate(slots, 1):
                _set_job(
                    message=f"Backfilling {slot} {sampled:%d %b %Y} ({index}/{len(slots)})…",
                    slots_done=index - 1,
                    slots_total=len(slots),
                )
                try:
                    raw = fetch_report(
                        client,
                        token,
                        key,
                        cloud=True,
                        display_version=version,
                        past_report_time=unix,
                    )
                    rows = list(flatten_report(raw, catalog_item=item).get("rows") or [])
                    saved_total += save_rows(
                        db,
                        rows,
                        sampled_at=sampled,
                        slot=slot,
                    )
                    db.commit()
                    fetched += 1
                    if rows:
                        empty_streak = 0
                    else:
                        empty_streak += 1
                except Exception as exc:
                    db.rollback()
                    errors.append(f"{sampled:%Y-%m-%d} {slot}: {exc}")
                    empty_streak += 1
                if stop_on_empty and empty_streak >= EMPTY_SLOT_STOP:
                    _set_job(
                        message=(
                            f"Reached the start of SenseHub history after "
                            f"{sampled:%d %b %Y}."
                        )
                    )
                    break
        _set_job(
            status="complete",
            message=(
                f"Backfilled {fetched} time slots ({saved_total} rows)"
                + (f", {len(errors)} failed." if errors else ".")
            ),
            slots_done=len(slots),
            slots_total=len(slots),
        )
        return {
            "saved": saved_total,
            "slots": len(slots),
            "span_days": span,
            "errors": errors,
        }
    except Exception as exc:
        _set_job(status="error", message=str(exc))
        raise


def run_backfill_in_background(
    db_factory,
    days: int | None = None,
    *,
    force: bool = True,
) -> None:
    db = db_factory()
    try:
        backfill_youngstock_health(db, days=days, force=force)
    except Exception:
        pass
    finally:
        db.close()


def _inventory_indexes(
    db: Session,
) -> tuple[dict[str, HerdInventory], dict[str, HerdInventory]]:
    by_cow: dict[str, HerdInventory] = {}
    by_tag: dict[str, HerdInventory] = {}
    for record in db.scalars(select(HerdInventory)).all():
        for key in _digit_keys(record.cow_id):
            by_cow.setdefault(key, record)
        for key in _digit_keys(record.etag):
            by_tag.setdefault(key, record)
    return by_cow, by_tag


def match_inventory(
    animal_id: str,
    by_cow: dict[str, HerdInventory],
    by_tag: dict[str, HerdInventory],
) -> HerdInventory | None:
    """Join a SenseHub animal name/ID to DairyComp, ignoring ' - remark' suffixes."""
    for key in _scr_id_keys(animal_id):
        found = by_cow.get(key) or by_tag.get(key)
        if found:
            return found
    return by_cow.get(animal_id) or by_tag.get(animal_id)


def list_low_health(
    db: Session,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    treated_within_days: int | None = None,
) -> dict[str, Any]:
    seed_from_latest_snapshot(db)
    latest = db.scalar(select(func.max(SenseHubYoungstockHealth.sampled_at)))
    empty = {
        "threshold": threshold,
        "treated_within_days": treated_within_days,
        "sampled_at": None,
        "slot": None,
        "count": 0,
        "animals": [],
    }
    if latest is None:
        return empty
    rows = list(
        db.scalars(
            select(SenseHubYoungstockHealth)
            .where(SenseHubYoungstockHealth.sampled_at == latest)
            .where(SenseHubYoungstockHealth.health_index.is_not(None))
            .where(SenseHubYoungstockHealth.health_index <= threshold)
            .order_by(
                SenseHubYoungstockHealth.health_index.asc(),
                SenseHubYoungstockHealth.animal_id.asc(),
            )
        ).all()
    )
    by_cow, by_tag = _inventory_indexes(db)
    matched = [
        (row.animal_id, match_inventory(row.animal_id, by_cow, by_tag))
        for row in rows
    ]
    events_by_animal = _events_for_animals(db, matched)
    selected: list[tuple[SenseHubYoungstockHealth, HerdInventory | None, list[CowEvent], int | None]] = []
    for row, (_animal_id, inventory) in zip(rows, matched, strict=True):
        events = events_by_animal.get(row.animal_id, [])
        dslt = days_since_last_treatment(events)
        if treated_within_days is not None and (
            dslt is None or dslt > treated_within_days
        ):
            continue
        selected.append((row, inventory, events, dslt))
    animal_ids = [row.animal_id for row, _inventory, _events, _dslt in selected]
    history_by_animal: dict[str, list[float | None]] = defaultdict(list)
    if animal_ids:
        history_rows = db.scalars(
            select(SenseHubYoungstockHealth)
            .where(SenseHubYoungstockHealth.animal_id.in_(animal_ids))
            .order_by(
                SenseHubYoungstockHealth.animal_id.asc(),
                SenseHubYoungstockHealth.sampled_at.asc(),
            )
        ).all()
        for sample in history_rows:
            history_by_animal[sample.animal_id].append(sample.health_index)
    animals = []
    for row, inventory, events, dslt in selected:
        etag_value = (inventory.etag if inventory else None) or row.animal_id
        highlight = recent_antibiotic_highlight(events)
        animals.append(
            {
                "animal_id": row.animal_id,
                "etag4": etag4(etag_value) or etag4(row.animal_id),
                "health_index": row.health_index,
                "age_days": dairycomp_age_days(inventory),
                "days_since_last_treatment": dslt,
                "resp_count": treatment_counts(events)["resp_count"],
                "recent_antibiotic": highlight,
                "trend": trend_dots(history_by_animal.get(row.animal_id, [])),
                "group_name": row.group_name,
                "farm": inventory.farm if inventory else None,
                "cow_id": inventory.cow_id if inventory else None,
                "etag": (inventory.etag or "").strip() if inventory and inventory.etag else None,
                "pen": inventory.pen if inventory else None,
                "has_dairycomp": inventory is not None,
            }
        )
    animals.sort(
        key=lambda item: (
            int(item["etag4"]) if str(item.get("etag4") or "").isdigit() else 10**9,
            item["animal_id"],
        )
    )
    slot_row = selected[0][0] if selected else (
        rows[0] if rows else db.scalar(
            select(SenseHubYoungstockHealth).where(
                SenseHubYoungstockHealth.sampled_at == latest
            )
        )
    )
    return {
        "threshold": threshold,
        "treated_within_days": treated_within_days,
        "sampled_at": latest.isoformat(),
        "slot": slot_row.slot if slot_row else None,
        "count": len(animals),
        "animals": animals,
    }


def animal_events(db: Session, animal_id: str) -> dict[str, Any]:
    animal_id = normalize_animal_id(animal_id) or animal_id
    latest = db.scalar(
        select(SenseHubYoungstockHealth)
        .where(SenseHubYoungstockHealth.animal_id == animal_id)
        .order_by(SenseHubYoungstockHealth.sampled_at.desc())
    )
    by_cow, by_tag = _inventory_indexes(db)
    inventory = match_inventory(animal_id, by_cow, by_tag)
    filters = [CowEvent.cow_id == animal_id]
    if inventory:
        if inventory.cow_id:
            filters.append(CowEvent.cow_id == inventory.cow_id)
        if inventory.etag:
            filters.append(CowEvent.etag == inventory.etag)
    events = list(
        db.scalars(
            select(CowEvent)
            .where(or_(*filters))
            .order_by(CowEvent.event_date.desc(), CowEvent.id.desc())
        ).all()
    )
    birth = inventory.bdat if inventory else None
    if birth is None and latest and latest.age_days is not None:
        birth = dt.date.today() - dt.timedelta(days=latest.age_days)
    if birth is not None:
        events = [
            event
            for event in events
            if event.event_date is None or event.event_date >= birth
        ]
    samples = list(
        db.scalars(
            select(SenseHubYoungstockHealth)
            .where(SenseHubYoungstockHealth.animal_id == animal_id)
            .order_by(SenseHubYoungstockHealth.sampled_at.asc())
        ).all()
    )
    health_history = [
        {
            "sampled_at": sample.sampled_at.isoformat() if sample.sampled_at else None,
            "slot": sample.slot,
            "health_index": sample.health_index,
            "label": sample_label(sample.sampled_at, sample.slot),
        }
        for sample in samples
    ]
    markers = chart_event_markers(events)
    sampled_dates = {
        sample.sampled_at.date().isoformat()
        for sample in samples
        if sample.sampled_at
    }
    for marker in markers:
        if marker["date"] in sampled_dates:
            continue
        sampled_dates.add(marker["date"])
        day = dt.date.fromisoformat(marker["date"])
        health_history.append(
            {
                "sampled_at": f"{marker['date']}T12:00:00",
                "slot": None,
                "health_index": None,
                "label": f"{day:%d %b}",
            }
        )
    health_history.sort(key=lambda item: item.get("sampled_at") or "")
    history = [
        {
            "event_date": event.event_date.isoformat() if event.event_date else None,
            "event": event.event,
            "remark": event.remark,
            "protocols": event.protocols,
            "technician": event.technician,
            "dim": event.dim,
            "lact": event.lact,
            "farm": event.farm,
        }
        for event in events
    ]
    return {
        "animal_id": animal_id,
        "health_index": latest.health_index if latest else None,
        "age_days": dairycomp_age_days(inventory),
        "etag4": etag4((inventory.etag if inventory else None) or animal_id),
        "group_name": latest.group_name if latest else None,
        "sampled_at": latest.sampled_at.isoformat() if latest and latest.sampled_at else None,
        "farm": inventory.farm if inventory else None,
        "cow_id": inventory.cow_id if inventory else None,
        "etag": (inventory.etag or "").strip() if inventory and inventory.etag else None,
        "pen": inventory.pen if inventory else None,
        "birth_date": birth.isoformat() if birth else None,
        "has_dairycomp": inventory is not None,
        "health_history": health_history,
        "events": history,
        **treatment_counts(events),
        "chart_markers": markers,
    }


UNASSIGNED_PEN = "110"
REASON_NO_SCR = "Calf doesn't have SCR tag"
REASON_WRONG_SCR = "Calf ID probably wrong on SCR"
REASON_TAG_REMOVED = "SCR Tag has been removed"
WRONG_SCR_MIN_AGE_DAYS = 3
WEANING_EVENT = "WEANING"
WEANED_REMARK = "WEANED"


def inventory_assignment_key(farm: str | None, cow_id: str | None) -> str:
    return f"inventory|{(farm or '').strip()}|{(cow_id or '').strip()}"


def sensehub_assignment_key(animal_id: str | None) -> str:
    return f"sensehub||{(animal_id or '').strip()}"


def _assignment_map(db: Session) -> dict[str, str]:
    rows = db.scalars(select(SenseHubCalfAssignment)).all()
    return {row.row_key: row.scr_tag for row in rows if row.scr_tag}


def _sent_assignment_keys(db: Session) -> set[str]:
    rows = db.scalars(
        select(SenseHubCalfAssignment).where(
            SenseHubCalfAssignment.sent_to_sensehub.is_(True)
        )
    ).all()
    return {row.row_key for row in rows}


def _identity_keys(*values: str | None) -> set[str]:
    keys: set[str] = set()
    for value in values:
        keys.update(_scr_id_keys(value))
    return keys


def _weaned_identity_keys(db: Session) -> set[str]:
    event_name = func.upper(func.trim(CowEvent.event))
    remark = func.upper(func.trim(func.coalesce(CowEvent.remark, "")))
    rows = db.execute(
        select(CowEvent.cow_id, CowEvent.etag).where(
            event_name == WEANING_EVENT,
            remark == WEANED_REMARK,
        )
    ).all()
    keys: set[str] = set()
    for cow_id, etag in rows:
        keys.update(_identity_keys(cow_id, etag))
    return keys


def _animals_by_scr_key(
    animals: list[dict[str, Any]] | None = None,
    *,
    loader,
) -> dict[str, dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    if animals is None:
        try:
            animals = loader()
        except SenseHubError:
            animals = []
    for animal in animals:
        name = str(animal.get("animal_name") or "")
        for key in _scr_id_keys(name):
            by_key.setdefault(key, animal)
    return by_key


def _untagged_animals_by_key(
    animals: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    return _animals_by_scr_key(animals, loader=list_untagged_sensehub_animals)


def _match_untagged_animal(
    cow_id: str | None, etag: str | None, untagged: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    for key in _identity_keys(cow_id, etag):
        match = untagged.get(key)
        if match:
            return match
    return None


def _inventory_birth_date(
    db: Session, farm: str | None, cow_id: str | None
) -> dt.date | None:
    cow = (cow_id or "").strip()
    if not cow:
        return None
    query = select(HerdInventory).where(HerdInventory.cow_id == cow)
    farm_key = (farm or "").strip()
    if farm_key:
        query = query.where(HerdInventory.farm == farm_key)
    record = db.scalar(query)
    return record.bdat if record and record.bdat else None


def save_scr_tag(
    db: Session,
    *,
    row_key: str,
    farm: str | None,
    cow_id: str | None,
    etag: str | None,
    scr_tag: str | None,
) -> dict[str, Any]:
    key = str(row_key or "").strip()
    if not key:
        raise ValueError("Missing row key.")
    tag = str(scr_tag or "").strip()
    existing = db.scalar(
        select(SenseHubCalfAssignment).where(SenseHubCalfAssignment.row_key == key)
    )
    if not tag:
        if existing is not None:
            db.delete(existing)
            db.commit()
        return {
            "row_key": key,
            "scr_tag": None,
            "created_on_sensehub": False,
            "assigned_on_sensehub": False,
        }
    created = False
    assigned = False
    if key.startswith("inventory|"):
        cow = (cow_id or "").strip()
        if not cow:
            raise ValueError("Missing Cow ID.")
        birth = _inventory_birth_date(db, farm, cow)
        if birth is None:
            raise ValueError(
                "This calf has no DairyComp birth date, so it cannot be created on SenseHub."
            )
        untagged = _match_untagged_animal(
            cow, etag, _untagged_animals_by_key()
        )
        already_named = _match_untagged_animal(
            cow, etag, _animals_by_scr_key(loader=list_sensehub_animals)
        )
        if untagged:
            assign_sensehub_monitoring_tag(
                animal_id=int(untagged["animal_id"]),
                scr_tag=tag,
            )
            assigned = True
        elif already_named:
            pass
        else:
            try:
                create_sensehub_calf(
                    animal_name=cow, scr_tag=tag, birth_date=birth
                )
                created = True
            except SenseHubError as exc:
                if "already exists" not in str(exc).casefold():
                    raise
                retry = _match_untagged_animal(
                    cow, etag, _untagged_animals_by_key()
                )
                if retry is not None:
                    assign_sensehub_monitoring_tag(
                        animal_id=int(retry["animal_id"]),
                        scr_tag=tag,
                    )
                    assigned = True
                elif _match_untagged_animal(
                    cow, etag, _animals_by_scr_key(loader=list_sensehub_animals)
                ) is None:
                    raise
    if existing is None:
        existing = SenseHubCalfAssignment(row_key=key)
        db.add(existing)
    existing.farm = (farm or "").strip() or None
    existing.cow_id = (cow_id or "").strip() or None
    existing.etag = (etag or "").strip() or None
    existing.scr_tag = tag
    if created or assigned:
        existing.sent_to_sensehub = True
    db.commit()
    return {
        "row_key": key,
        "scr_tag": tag,
        "created_on_sensehub": created,
        "assigned_on_sensehub": assigned,
    }


def _pen_is_unassigned_pen(pen: str | None) -> bool:
    text = str(pen or "").strip()
    if text == UNASSIGNED_PEN:
        return True
    try:
        return int(float(text)) == 110
    except (TypeError, ValueError):
        return False


def _latest_sensehub_samples(db: Session) -> list[SenseHubYoungstockHealth]:
    latest = db.scalar(select(func.max(SenseHubYoungstockHealth.sampled_at)))
    if latest is None:
        return []
    return list(
        db.scalars(
            select(SenseHubYoungstockHealth).where(
                SenseHubYoungstockHealth.sampled_at == latest
            )
        ).all()
    )


def _sensehub_id_keys(samples: list[SenseHubYoungstockHealth]) -> set[str]:
    keys: set[str] = set()
    for sample in samples:
        for value in (sample.animal_id, sample.raw_animal_id):
            keys.update(_scr_id_keys(value))
            normalized = normalize_animal_id(value)
            if normalized:
                keys.add(normalized)
    return keys


def _id_keys_from_report_rows(rows: list[Any]) -> set[str]:
    keys: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        for field in (
            "AnimalID",
            "animal_id",
            "animalName",
            "AnimalName",
            "Name",
            "CowID",
        ):
            value = row.get(field)
            if value in (None, ""):
                continue
            keys.update(_scr_id_keys(str(value)))
            normalized = normalize_animal_id(value)
            if normalized:
                keys.add(normalized)
    return keys


def _report_snapshot_id_keys(db: Session) -> set[str]:
    """Animal IDs from stored SenseHub reports."""
    keys: set[str] = set()
    for snapshot in db.scalars(select(SenseHubReportSnapshot)).all():
        keys.update(_id_keys_from_report_rows((snapshot.payload or {}).get("rows") or []))
    return keys


def _herd_snapshot(db: Session) -> SenseHubReportSnapshot | None:
    snapshots = list(db.scalars(select(SenseHubReportSnapshot)).all())
    return next(
        (
            item
            for item in snapshots
            if is_herd_report(item.report_name) or is_herd_report(item.title)
        ),
        None,
    )


def _no_data_snapshot(db: Session) -> SenseHubReportSnapshot | None:
    return next(
        (
            item
            for item in db.scalars(select(SenseHubReportSnapshot)).all()
            if compact_report_name(item.report_name) == compact_report_name(NO_DATA_REPORT)
            or compact_report_name(item.title) == compact_report_name(NO_DATA_REPORT)
        ),
        None,
    )


def _animals_from_herd_snapshot(db: Session) -> list[dict[str, Any]]:
    snapshot = _herd_snapshot(db)
    if snapshot is None:
        return []
    animals: list[dict[str, Any]] = []
    for row in (snapshot.payload or {}).get("rows") or []:
        if not isinstance(row, dict):
            continue
        name = str(
            row.get("AnimalID")
            or row.get("animal_name")
            or row.get("animalName")
            or ""
        ).strip()
        if not name:
            continue
        animal_id = row.get("CowDatabaseID") or row.get("animal_id")
        try:
            parsed_id = int(animal_id) if animal_id not in (None, "") else None
        except (TypeError, ValueError):
            parsed_id = None
        tag_known = any(
            key in row for key in ("CowRfidOrScrTagNumber", "scr_tag", "CowScrTagNumber")
        )
        tag = row.get("CowRfidOrScrTagNumber") or row.get("scr_tag")
        tag_text = str(tag).strip() if tag not in (None, "") else None
        if tag_text and tag_text.casefold() in {"none", "null", "-"}:
            tag_text = None
        animals.append(
            {
                "animal_id": parsed_id,
                "animal_name": name,
                "scr_tag": tag_text,
                "tag_known": tag_known,
            }
        )
    return animals


def _untagged_from_herd_snapshot(db: Session) -> list[dict[str, Any]]:
    return [
        item
        for item in _animals_from_herd_snapshot(db)
        if item.get("tag_known")
        and not item.get("scr_tag")
        and item.get("animal_id") is not None
    ]


def _no_data_from_snapshot(db: Session) -> list[dict[str, Any]]:
    snapshot = _no_data_snapshot(db)
    if snapshot is None:
        return []
    return parse_no_data_rows((snapshot.payload or {}).get("rows") or [])


def _youngstock_health_snapshot(db: Session) -> SenseHubReportSnapshot | None:
    return next(
        (
            item
            for item in db.scalars(select(SenseHubReportSnapshot)).all()
            if compact_report_name(item.report_name) == compact_report_name(DEFAULT_REPORT)
            or compact_report_name(item.title) == compact_report_name(DEFAULT_REPORT)
        ),
        None,
    )


def _metric_is_blank(value: Any) -> bool:
    if value is None:
        return True
    text = str(value).strip().casefold()
    return text in {"", "-", "—", "–", "none", "null", "n/a", "na", "."}


def _health_metrics_are_blank(*values: Any) -> bool:
    return all(_metric_is_blank(value) for value in values)


def _row_health_metrics_are_blank(row: dict[str, Any]) -> bool:
    health = (
        row["YoungStockHealthIndex"]
        if "YoungStockHealthIndex" in row
        else row.get("health_index")
    )
    eating = row["DailyEatingTime"] if "DailyEatingTime" in row else row.get("eating")
    rumination = (
        row["DailyRumination"] if "DailyRumination" in row else row.get("rumination")
    )
    return _health_metrics_are_blank(health, eating, rumination)


def _sample_health_metrics_are_blank(sample: SenseHubYoungstockHealth) -> bool:
    return _health_metrics_are_blank(
        sample.health_index, sample.eating, sample.rumination
    )


def _identity_from_sample(sample: SenseHubYoungstockHealth) -> set[str]:
    keys = _scr_id_keys(sample.animal_id) | _scr_id_keys(sample.raw_animal_id)
    for value in (sample.animal_id, sample.raw_animal_id):
        normalized = normalize_animal_id(value)
        if normalized:
            keys.add(normalized)
    return keys


def _youngstock_health_all_keys(db: Session) -> set[str]:
    """IDs that currently have Young Stock Health readings (not blank / '-')."""
    keys: set[str] = set()
    on_snapshot: set[str] = set()
    snapshot = _youngstock_health_snapshot(db)
    if snapshot is not None:
        for row in (snapshot.payload or {}).get("rows") or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("AnimalID") or row.get("animal_name") or "").strip()
            if not name:
                continue
            identity = _row_identity(name)
            on_snapshot |= identity
            if not _row_health_metrics_are_blank(row):
                keys |= identity
    for sample in _latest_sensehub_samples(db):
        identity = _identity_from_sample(sample)
        if identity & on_snapshot:
            continue
        if not _sample_health_metrics_are_blank(sample):
            keys |= identity
    return keys


def _row_identity(name: str) -> set[str]:
    identity = _scr_id_keys(name)
    normalized = normalize_animal_id(name)
    if normalized:
        identity.add(normalized)
    return identity


def _blank_youngstock_health_items(db: Session) -> list[dict[str, Any]]:
    """YSH All animals whose health, eating and rumination are all blank."""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_item(
        name: str,
        *,
        age_days: int | None,
        scr_tag: Any,
        days_with_assigned_tag: int | None,
    ) -> None:
        identity = _row_identity(name)
        if not name or identity & seen:
            return
        seen.update(identity)
        tag = str(scr_tag).strip() if scr_tag not in (None, "") else None
        if tag and tag.casefold() in {"none", "null", "-"}:
            tag = None
        items.append(
            {
                "animal_id": None,
                "animal_name": name,
                "age_days": age_days,
                "scr_tag": tag,
                "days_with_assigned_tag": days_with_assigned_tag,
            }
        )

    snapshot = _youngstock_health_snapshot(db)
    if snapshot is not None:
        for row in (snapshot.payload or {}).get("rows") or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("AnimalID") or row.get("animal_name") or "").strip()
            if not name or not _row_health_metrics_are_blank(row):
                continue
            add_item(
                name,
                age_days=_to_int(row.get("AgeInDays") or row.get("age_days")),
                scr_tag=row.get("CowScrTagNumber") or row.get("CowRfidOrScrTagNumber"),
                days_with_assigned_tag=days_with_assigned_tag(row),
            )
    for sample in _latest_sensehub_samples(db):
        if not _sample_health_metrics_are_blank(sample):
            continue
        name = str(sample.raw_animal_id or sample.animal_id or "").strip()
        add_item(
            name,
            age_days=sample.age_days,
            scr_tag=None,
            days_with_assigned_tag=None,
        )
    return items


def _sensehub_db_id_lookup(db: Session) -> dict[str, int]:
    """Map cow-number keys to SenseHub CowDatabaseID from herd and tag reports."""
    by_key: dict[str, int] = {}

    def add(name: Any, raw_id: Any) -> None:
        try:
            parsed = int(raw_id) if raw_id not in (None, "") else None
        except (TypeError, ValueError):
            parsed = None
        if parsed is None:
            return
        for key in _row_identity(str(name or "")):
            by_key.setdefault(key, parsed)

    for item in _animals_from_herd_snapshot(db):
        add(item.get("animal_name"), item.get("animal_id"))
    for snapshot in db.scalars(select(SenseHubReportSnapshot)).all():
        for row in (snapshot.payload or {}).get("rows") or []:
            if not isinstance(row, dict):
                continue
            raw_id = (
                row.get("CowDatabaseID")
                or row.get("TagCowDatabaseID")
                or row.get("CowDbId")
                or row.get("animal_id")
            )
            name = (
                row.get("AnimalID")
                or row.get("TagAnimalID")
                or row.get("animal_name")
                or row.get("animalName")
            )
            add(name, raw_id)
    return by_key


def _sensehub_tag_lookup(db: Session) -> dict[str, str]:
    by_key: dict[str, str] = {}

    def add(name: Any, tag: Any) -> None:
        text = str(tag).strip() if tag not in (None, "") else None
        if not text or text.casefold() in {"none", "null", "-"}:
            return
        for key in _row_identity(str(name or "")):
            by_key.setdefault(key, text)

    for item in _animals_from_herd_snapshot(db):
        add(item.get("animal_name"), item.get("scr_tag"))
    for snapshot in db.scalars(select(SenseHubReportSnapshot)).all():
        for row in (snapshot.payload or {}).get("rows") or []:
            if not isinstance(row, dict):
                continue
            add(
                row.get("AnimalID") or row.get("TagAnimalID") or row.get("animal_name"),
                row.get("CowScrTagNumber")
                or row.get("CowRfidOrScrTagNumber")
                or row.get("TagNumber"),
            )
    return by_key


def _apply_sensehub_db_ids(
    items: list[dict[str, Any]], lookup: dict[str, int]
) -> None:
    for item in items:
        if item.get("animal_id") is not None:
            continue
        for key in _row_identity(str(item.get("animal_name") or "")):
            found = lookup.get(key)
            if found is not None:
                item["animal_id"] = found
                break


def _apply_sensehub_tags(
    items: list[dict[str, Any]], lookup: dict[str, str]
) -> None:
    for item in items:
        if item.get("scr_tag"):
            continue
        for key in _row_identity(str(item.get("animal_name") or "")):
            found = lookup.get(key)
            if found:
                item["scr_tag"] = found
                break


def _tag_days_from_health_history(db: Session) -> dict[str, int]:
    """Days since the earliest stored young-stock sample, keyed by identity."""
    rows = db.execute(
        select(
            SenseHubYoungstockHealth.animal_id,
            SenseHubYoungstockHealth.raw_animal_id,
            func.min(SenseHubYoungstockHealth.sampled_at),
        ).group_by(
            SenseHubYoungstockHealth.animal_id,
            SenseHubYoungstockHealth.raw_animal_id,
        )
    ).all()
    today = dt.datetime.now(_UK).date()
    by_key: dict[str, int] = {}
    for animal_id, raw_animal_id, sampled_at in rows:
        if sampled_at is None:
            continue
        days = max(0, (today - sampled_at.date()).days)
        identity = _scr_id_keys(animal_id) | _scr_id_keys(raw_animal_id)
        normalized = normalize_animal_id(animal_id) or normalize_animal_id(raw_animal_id)
        if normalized:
            identity.add(normalized)
        for key in identity:
            previous = by_key.get(key)
            if previous is None or days > previous:
                by_key[key] = days
    return by_key


def _resolved_days_with_tag(
    item: dict[str, Any], identity: set[str], history_days: dict[str, int]
) -> int | None:
    days = item.get("days_with_assigned_tag")
    if days is not None:
        try:
            return int(days)
        except (TypeError, ValueError):
            days = None
    inferred: int | None = None
    for key in identity:
        value = history_days.get(key)
        if value is None:
            continue
        inferred = value if inferred is None else max(inferred, value)
    return inferred


def _recently_tagged_ids(
    items: list[dict[str, Any]],
    history_days: dict[str, int] | None = None,
) -> tuple[set[int], set[str]]:
    animal_ids: set[int] = set()
    keys: set[str] = set()
    history_days = history_days or {}
    for item in items:
        identity = _scr_id_keys(str(item.get("animal_name") or ""))
        days = _resolved_days_with_tag(item, identity, history_days)
        if days is None:
            continue
        try:
            if int(days) >= NO_DATA_MIN_TAG_DAYS:
                continue
        except (TypeError, ValueError):
            continue
        try:
            animal_ids.add(int(item["animal_id"]))
        except (TypeError, ValueError, KeyError):
            pass
        keys.update(identity)
    return animal_ids, keys


def _inventory_on_sensehub(record: HerdInventory, sensehub_keys: set[str]) -> bool:
    keys = _scr_id_keys(record.cow_id) | _scr_id_keys(record.etag)
    return bool(keys & sensehub_keys)


def _category_wanted(category: str | None, wanted: set[str]) -> bool:
    cat = (category or "").strip().casefold()
    if "dairy" in wanted and cat in {"dairy", "youngstock", "heifer", "calf", ""}:
        return True
    if "beef" in wanted and cat == "beef":
        return True
    return False


def list_unassigned_calves(
    db: Session,
    *,
    categories: list[str] | None = None,
) -> dict[str, Any]:
    """Pen 110 calves missing SCR, plus SenseHub IDs that do not match inventory."""
    wanted = {
        str(item).strip().casefold()
        for item in (categories or ["Dairy"])
        if str(item).strip()
    }
    samples = _latest_sensehub_samples(db)
    sensehub_keys = _sensehub_id_keys(samples)
    sensehub_keys.update(_report_snapshot_id_keys(db))
    assignments = _assignment_map(db)
    sent_keys = _sent_assignment_keys(db)
    weaned_keys = _weaned_identity_keys(db)
    herd_animals = _animals_from_herd_snapshot(db)
    untagged = _animals_by_scr_key(
        [
            item
            for item in herd_animals
            if item.get("tag_known") and not item.get("scr_tag")
        ],
        loader=lambda: [],
    )
    register = _animals_by_scr_key(herd_animals, loader=lambda: [])
    sensehub_keys.update(set(register) - set(untagged))
    sensehub_keys -= set(untagged)
    animals: list[dict[str, Any]] = []
    if wanted:
        for record in db.scalars(select(HerdInventory)).all():
            if not _pen_is_unassigned_pen(record.pen):
                continue
            category = (record.category or "").strip()
            if not _category_wanted(category, wanted):
                continue
            if _identity_keys(record.cow_id, record.etag) & weaned_keys:
                continue
            if _inventory_on_sensehub(record, sensehub_keys):
                continue
            etag_value = (record.etag or "").strip() or None
            row_key = inventory_assignment_key(record.farm, record.cow_id)
            if row_key in sent_keys:
                continue
            tag_removed = (
                _match_untagged_animal(record.cow_id, etag_value, untagged)
                is not None
            )
            animals.append(
                {
                    "row_key": row_key,
                    "farm": record.farm,
                    "cow_id": record.cow_id,
                    "etag": etag_value,
                    "etag4": etag4(etag_value) or etag4(record.cow_id),
                    "category": category or None,
                    "age_days": dairycomp_age_days(record),
                    "birth_date": record.bdat.isoformat() if record.bdat else None,
                    "pen": str(record.pen).strip() if record.pen else None,
                    "reason": REASON_TAG_REMOVED if tag_removed else REASON_NO_SCR,
                    "scr_tag": assignments.get(row_key),
                }
            )
    by_cow, by_tag = _inventory_indexes(db)
    seen_wrong: set[str] = set()
    for sample in samples:
        animal_id = sample.animal_id
        if not animal_id or animal_id in seen_wrong:
            continue
        if (
            match_inventory(animal_id, by_cow, by_tag) is not None
            or match_inventory(sample.raw_animal_id or "", by_cow, by_tag) is not None
        ):
            continue
        if sample.age_days is not None and sample.age_days < WRONG_SCR_MIN_AGE_DAYS:
            continue
        seen_wrong.add(animal_id)
        etag_value = (sample.raw_animal_id or "").strip() or animal_id
        row_key = sensehub_assignment_key(animal_id)
        animals.append(
            {
                "row_key": row_key,
                "farm": None,
                "cow_id": animal_id,
                "etag": etag_value,
                "etag4": etag4(etag_value) or etag4(animal_id),
                "category": None,
                "age_days": sample.age_days,
                "birth_date": None,
                "pen": sample.group_name,
                "reason": REASON_WRONG_SCR,
                "scr_tag": assignments.get(row_key),
            }
        )
    animals.sort(
        key=lambda row: (
            str(row.get("etag4") or "").isdigit(),
            int(row["etag4"]) if str(row.get("etag4") or "").isdigit() else 0,
            str(row.get("etag4") or ""),
            str(row.get("cow_id") or ""),
        ),
        reverse=True,
    )
    return {
        "pen": UNASSIGNED_PEN,
        "categories": sorted(wanted),
        "count": len(animals),
        "animals": animals,
    }


_CALF_CATEGORIES = {"dairy", "youngstock", "heifer", "calf", ""}
_MAX_TAG_REMOVAL_AGE_DAYS = 400
REASON_NO_TAG = "No SCR tag"
REASON_NO_DATA = "No Data"
REASON_FAULTY_TAG = "Tag most likely removed or faulty"
REASON_SOLD = "Sold"
REASON_DIED = "Died"
_EXIT_EVENTS = ("SOLD", "DIED")
NO_DATA_MIN_TAG_DAYS = 3


def _inventory_by_scr_keys(db: Session) -> dict[str, HerdInventory]:
    by_key: dict[str, HerdInventory] = {}
    for record in db.scalars(select(HerdInventory)).all():
        for key in _scr_id_keys(record.cow_id) | _scr_id_keys(record.etag):
            by_key.setdefault(key, record)
    return by_key


def _historic_youngstock_keys(db: Session) -> set[str]:
    keys: set[str] = set()
    animal_ids = db.scalars(
        select(SenseHubYoungstockHealth.animal_id).distinct()
    ).all()
    for animal_id in animal_ids:
        keys.update(_scr_id_keys(animal_id))
        normalized = normalize_animal_id(animal_id)
        if normalized:
            keys.add(normalized)
    return keys


def _is_tag_removal_candidate(
    record: HerdInventory | None, identity_keys: set[str], historic_keys: set[str]
) -> bool:
    if record is not None:
        if _pen_is_unassigned_pen(record.pen):
            return True
        category = (record.category or "").strip().casefold()
        if category == "beef" and not _pen_is_unassigned_pen(record.pen):
            return False
        age = dairycomp_age_days(record)
        if age is not None and age < _MAX_TAG_REMOVAL_AGE_DAYS:
            return True
        if category in {"youngstock", "heifer", "calf"}:
            return True
        return False
    return bool(identity_keys & historic_keys)


def _latest_exit_by_identity(db: Session) -> dict[str, tuple[dt.date, str]]:
    event_name = func.upper(func.trim(CowEvent.event))
    rows = db.execute(
        select(CowEvent.cow_id, CowEvent.etag, CowEvent.event, CowEvent.event_date).where(
            event_name.in_(_EXIT_EVENTS),
            CowEvent.event_date.isnot(None),
        )
    ).all()
    exits: dict[str, tuple[dt.date, str]] = {}
    for cow_id, etag, event, event_date in rows:
        name = str(event or "").strip().upper()
        for key in _scr_id_keys(cow_id) | _scr_id_keys(etag):
            previous = exits.get(key)
            if previous is None or event_date > previous[0]:
                exits[key] = (event_date, name)
    return exits


def _exit_for_identity(
    identity: set[str], exits: dict[str, tuple[dt.date, str]]
) -> tuple[dt.date, str] | None:
    best: tuple[dt.date, str] | None = None
    for key in identity:
        hit = exits.get(key)
        if hit is None:
            continue
        if best is None or hit[0] > best[0]:
            best = hit
    return best


def _apply_exit_reason(row: dict[str, Any], identity: set[str], exits: dict[str, tuple[dt.date, str]]) -> None:
    exit_info = _exit_for_identity(identity, exits)
    if exit_info is None:
        return
    row["exit_date"] = exit_info[0].isoformat()
    row["reason"] = REASON_DIED if exit_info[1] == "DIED" else REASON_SOLD


def _cull_rows_with_exit_dates(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[dt.date, list[int]] = defaultdict(list)
    for row in rows:
        raw = row.get("exit_date")
        if not raw:
            continue
        groups[dt.date.fromisoformat(str(raw))].append(int(row["animal_id"]))
    culled_ids: list[int] = []
    for day, ids in groups.items():
        try:
            result = cull_sensehub_animals(ids, occurred_on=day)
            culled_ids.extend(int(item) for item in (result.get("animal_ids") or ids))
        except SenseHubError:
            continue
    return {"culled": len(culled_ids), "animal_ids": culled_ids}


def auto_cull_exited_sensehub_animals(db: Session) -> dict[str, Any]:
    """Cull SenseHub animals that already have a DairyComp SOLD or DIED event."""
    listing = list_tags_to_remove(db, auto_cull=False)
    return _cull_rows_with_exit_dates(listing["animals"])


def _inventory_record(
    identity: set[str], inventory: dict[str, HerdInventory]
) -> HerdInventory | None:
    for key in identity:
        record = inventory.get(key)
        if record is not None:
            return record
    return None


def _too_recent_no_data(
    row: dict[str, Any],
    days_with_tag: int | None,
    *,
    require_days: bool = False,
) -> bool:
    if row["reason"] != REASON_NO_DATA:
        return False
    if days_with_tag is None:
        return require_days
    try:
        return int(days_with_tag) < NO_DATA_MIN_TAG_DAYS
    except (TypeError, ValueError):
        return require_days


def list_tags_to_remove(db: Session, *, auto_cull: bool = True) -> dict[str, Any]:
    """Herd animals with no tag, blank/No Data for 3+ days, or tagged but missing from YSH All."""
    id_lookup = _sensehub_db_id_lookup(db)
    tag_lookup = _sensehub_tag_lookup(db)
    untagged = _untagged_from_herd_snapshot(db)
    no_data = _no_data_from_snapshot(db)
    _apply_sensehub_db_ids(no_data, id_lookup)
    _apply_sensehub_tags(no_data, tag_lookup)
    blank_health = _blank_youngstock_health_items(db)
    _apply_sensehub_db_ids(blank_health, id_lookup)
    _apply_sensehub_tags(blank_health, tag_lookup)
    health_keys = _youngstock_health_all_keys(db)
    history_days = _tag_days_from_health_history(db)
    recent_ids, recent_keys = _recently_tagged_ids(
        [*no_data, *blank_health], history_days
    )
    inventory = _inventory_by_scr_keys(db)
    exits = _latest_exit_by_identity(db)
    animals: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in untagged:
        animal_id = int(item["animal_id"])
        if animal_id in seen:
            continue
        name = str(item.get("animal_name") or "").strip()
        identity = _scr_id_keys(name)
        record = _inventory_record(identity, inventory)
        if record is not None:
            identity |= _scr_id_keys(record.cow_id) | _scr_id_keys(record.etag)
        seen.add(animal_id)
        row = {
            "animal_id": animal_id,
            "id": name,
            "age_days": dairycomp_age_days(record),
            "scr_tag": item.get("scr_tag"),
            "days_with_assigned_tag": None,
            "reason": REASON_NO_TAG,
        }
        _apply_exit_reason(row, identity, exits)
        animals.append(row)
    for source, require_days in ((no_data, False), (blank_health, True)):
        for item in source:
            if item.get("animal_id") is None:
                continue
            animal_id = int(item["animal_id"])
            if animal_id in seen:
                continue
            name = str(item.get("animal_name") or "").strip()
            identity = _scr_id_keys(name)
            record = _inventory_record(identity, inventory)
            if record is not None:
                identity |= _scr_id_keys(record.cow_id) | _scr_id_keys(record.etag)
            age = item.get("age_days")
            if age is None:
                age = dairycomp_age_days(record)
            days_with_tag = _resolved_days_with_tag(item, identity, history_days)
            row = {
                "animal_id": animal_id,
                "id": name,
                "age_days": age,
                "scr_tag": item.get("scr_tag"),
                "days_with_assigned_tag": days_with_tag,
                "reason": REASON_NO_DATA,
            }
            _apply_exit_reason(row, identity, exits)
            if _too_recent_no_data(row, days_with_tag, require_days=require_days):
                continue
            seen.add(animal_id)
            animals.append(row)
    if health_keys:
        no_data_days = {
            int(item["animal_id"]): item.get("days_with_assigned_tag")
            for item in (*no_data, *blank_health)
            if item.get("animal_id") is not None
        }
        for item in _animals_from_herd_snapshot(db):
            animal_id = item.get("animal_id")
            if animal_id is None or not item.get("scr_tag"):
                continue
            animal_id = int(animal_id)
            if animal_id in seen:
                continue
            name = str(item.get("animal_name") or "").strip()
            identity = _scr_id_keys(name)
            if identity & health_keys:
                continue
            if animal_id in recent_ids or identity & recent_keys:
                continue
            record = _inventory_record(identity, inventory)
            if record is not None:
                identity |= _scr_id_keys(record.cow_id) | _scr_id_keys(record.etag)
            seen.add(animal_id)
            row = {
                "animal_id": animal_id,
                "id": name,
                "age_days": dairycomp_age_days(record),
                "scr_tag": item.get("scr_tag"),
                "days_with_assigned_tag": no_data_days.get(animal_id),
                "reason": REASON_FAULTY_TAG,
            }
            _apply_exit_reason(row, identity, exits)
            animals.append(row)
    animals.sort(
        key=lambda row: (
            int(row["id"]) if str(row.get("id") or "").isdigit() else 10**9,
            str(row.get("id") or ""),
        )
    )
    auto_culled = 0
    if auto_cull:
        result = _cull_rows_with_exit_dates(animals)
        culled = {int(item) for item in result.get("animal_ids") or []}
        auto_culled = int(result.get("culled") or 0)
        animals = [row for row in animals if int(row["animal_id"]) not in culled]
    herd_snap = _herd_snapshot(db)
    no_data_snap = _no_data_snapshot(db)
    health_snap = _youngstock_health_snapshot(db)
    stamps = [
        item.fetched_at
        for item in (herd_snap, no_data_snap, health_snap)
        if item is not None and item.fetched_at is not None
    ]
    return {
        "count": len(animals),
        "animals": animals,
        "auto_culled": auto_culled,
        "updated_at": max(stamps).isoformat() if stamps else None,
    }


def cull_tags_to_remove(
    db: Session,
    *,
    animal_id: int | None = None,
    animal_ids: list[int] | None = None,
) -> dict[str, Any]:
    listing = list_tags_to_remove(db, auto_cull=False)
    allowed = {int(row["animal_id"]): row for row in listing["animals"]}
    if animal_id is not None:
        wanted_ids = [int(animal_id)]
    elif animal_ids is not None:
        wanted_ids = [int(item) for item in animal_ids]
    else:
        wanted_ids = list(allowed)
    animals: list[dict[str, Any]] = []
    for item_id in wanted_ids:
        row = allowed.get(item_id)
        if row is None:
            raise SenseHubError("That animal is not on the Tags To Remove list.")
        animals.append(row)
    if not animals:
        return {"culled": 0, "count": 0, "failed": [], "animals": []}
    groups: dict[dt.date | None, list[int]] = defaultdict(list)
    for row in animals:
        raw = row.get("exit_date")
        day = dt.date.fromisoformat(str(raw)) if raw else None
        groups[day].append(int(row["animal_id"]))
    culled = 0
    failed: list[Any] = []
    for day, ids in groups.items():
        result = cull_sensehub_animals(ids, occurred_on=day)
        culled += int(result.get("culled") or 0)
        failed.extend(result.get("failed") or [])
    return {
        "culled": culled,
        "count": len(animals),
        "failed": failed,
        "animals": animals,
    }
