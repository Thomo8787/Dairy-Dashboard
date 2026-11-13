"""Embryo flushes, frozen stock, and recipient implants."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from services.database import EmbryoBatch, EmbryoImplant

GESTATION_DAYS = 269

PD_CHOICES: tuple[tuple[str, str], ...] = (
    ("", "Not scanned"),
    ("positive", "Positive"),
    ("negative", "Negative"),
)
OUTCOME_CHOICES: tuple[tuple[str, str], ...] = (
    ("", "No outcome yet"),
    ("in_calf", "In calf"),
    ("empty", "Empty"),
    ("live_heifer", "Live heifer"),
    ("bull", "Bull calf"),
    ("abort", "Aborted"),
    ("stillbirth", "Stillbirth"),
    ("born_early", "Born early"),
    ("recipient_died", "Recipient died"),
    ("died_sold", "Died or sold"),
)
SEX_CHOICES: tuple[tuple[str, str], ...] = (
    ("", ""),
    ("F", "Heifer"),
    ("M", "Bull"),
)

_PD_LABELS = dict(PD_CHOICES)
_OUTCOME_LABELS = dict(OUTCOME_CHOICES)
_KIND_LABELS = {
    "flush": "Flush",
    "existing": "Already in tank",
    "purchased": "Purchased",
}
_TERMINAL_OUTCOMES = {
    "empty",
    "live_heifer",
    "bull",
    "abort",
    "stillbirth",
    "born_early",
    "recipient_died",
    "died_sold",
}
_OTHER_LOSSES = {"stillbirth", "born_early", "recipient_died", "died_sold"}
_HELD_OUTCOMES = {"in_calf", "live_heifer", "bull", "stillbirth", "born_early"}


def sire_label(sire_1: str | None, sire_2: str | None, resolved: str | None = None) -> str:
    if (resolved or "").strip():
        return resolved.strip()
    first = (sire_1 or "").strip()
    second = (sire_2 or "").strip()
    if first and second and first.casefold() != second.casefold():
        return f"{first} or {second}"
    return first or second


def format_date(value: dt.date | None) -> str:
    if value is None:
        return ""
    return value.strftime("%d %b %Y").lstrip("0")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _parse_date(value: Any, label: str) -> dt.date:
    text = _clean(value)
    if not text:
        raise ValueError(f"Enter {label}.")
    try:
        return dt.date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} is not a valid date.") from exc


def _parse_optional_date(value: Any) -> dt.date | None:
    text = _clean(value)
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("Date of birth is not a valid date.") from exc


def _parse_count(value: Any, label: str) -> int:
    text = _clean(value)
    if text == "":
        raise ValueError(f"Enter {label}.")
    try:
        number = int(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be a whole number.") from exc
    if number < 0:
        raise ValueError(f"{label} cannot be negative.")
    return number


def _parse_sires(form: Any) -> tuple[str, str | None]:
    sire_1 = _clean(form.get("sire_1"))
    sire_2 = _clean(form.get("sire_2"))
    if not sire_1:
        raise ValueError("Enter the sire.")
    if sire_2.casefold() == sire_1.casefold():
        sire_2 = ""
    return sire_1, sire_2 or None


def _due_date(implant_date: dt.date) -> dt.date:
    return implant_date + dt.timedelta(days=GESTATION_DAYS)


def _grades_recorded(batch: EmbryoBatch) -> bool:
    return batch.grade_1 is not None and batch.grade_2 is not None


def _implants_of(batch: EmbryoBatch, *, fresh: bool | None = None) -> list[EmbryoImplant]:
    rows = list(batch.implants or [])
    if fresh is None:
        return rows
    return [row for row in rows if bool(row.is_fresh) is fresh]


def _grade_used(implants: list[EmbryoImplant], grade: int) -> int:
    return sum(1 for row in implants if row.grade == grade)


def suggest_freeze(batch: EmbryoBatch) -> tuple[int, int]:
    """Embryos left after fresh implants, assuming grade 1 was used first."""
    if not _grades_recorded(batch):
        return 0, 0
    fresh = _implants_of(batch, fresh=True)
    fresh_g1 = _grade_used(fresh, 1)
    fresh_g2 = _grade_used(fresh, 2)
    return max(0, int(batch.grade_1 or 0) - fresh_g1), max(0, int(batch.grade_2 or 0) - fresh_g2)


def _frozen_remaining(batch: EmbryoBatch) -> tuple[int, int]:
    frozen = _implants_of(batch, fresh=False)
    left_1 = int(batch.frozen_grade_1 or 0) - _grade_used(frozen, 1)
    left_2 = int(batch.frozen_grade_2 or 0) - _grade_used(frozen, 2)
    return max(0, left_1), max(0, left_2)


def _next_fresh_grade(batch: EmbryoBatch) -> int:
    if not _grades_recorded(batch):
        raise ValueError("Enter the grade 1, grade 2, and unfertilised counts first.")
    fresh = _implants_of(batch, fresh=True)
    if _grade_used(fresh, 1) < int(batch.grade_1 or 0):
        return 1
    if _grade_used(fresh, 2) < int(batch.grade_2 or 0):
        return 2
    raise ValueError("Every grade 1 and grade 2 embryo from this flush is already implanted.")


def _next_frozen_grade(batch: EmbryoBatch) -> int:
    left_1, left_2 = _frozen_remaining(batch)
    if left_1 > 0:
        return 1
    if left_2 > 0:
        return 2
    raise ValueError("There are no frozen embryos left from this stock.")


def _recipient_is_open(row: EmbryoImplant) -> bool:
    if (row.pd_result or "") == "negative":
        return False
    return (row.outcome or "") not in _TERMINAL_OUTCOMES


def _assert_recipient_free(session: Session, recipient_id: str) -> None:
    rows = session.scalars(
        select(EmbryoImplant).where(func.lower(EmbryoImplant.recipient_id) == recipient_id.lower())
    ).all()
    for row in rows:
        if _recipient_is_open(row):
            raise ValueError(f"Recipient {recipient_id} already has an implant that is still open.")


def _load_batch(session: Session, batch_id: int) -> EmbryoBatch:
    batch = session.scalar(
        select(EmbryoBatch)
        .where(EmbryoBatch.id == batch_id)
        .options(selectinload(EmbryoBatch.implants))
    )
    if batch is None:
        raise ValueError("That flush or stock batch could not be found.")
    return batch


def _shrink_frozen_for_fresh_implant(batch: EmbryoBatch) -> str | None:
    """Keep frozen counts inside what is left after one more fresh implant."""
    if not batch.freeze_confirmed:
        return None
    fresh = _implants_of(batch, fresh=True)
    fresh_g1 = _grade_used(fresh, 1)
    fresh_g2 = _grade_used(fresh, 2)
    next_grade = 1 if fresh_g1 < int(batch.grade_1 or 0) else 2
    if next_grade == 1:
        fresh_g1 += 1
    else:
        fresh_g2 += 1
    max_1 = max(0, int(batch.grade_1 or 0) - fresh_g1)
    max_2 = max(0, int(batch.grade_2 or 0) - fresh_g2)
    frozen_used = _implants_of(batch, fresh=False)
    used_1 = _grade_used(frozen_used, 1)
    used_2 = _grade_used(frozen_used, 2)
    if used_1 > max_1 or used_2 > max_2:
        raise ValueError("Reduce the frozen implants before adding another fresh recipient.")
    changed = int(batch.frozen_grade_1 or 0) > max_1 or int(batch.frozen_grade_2 or 0) > max_2
    batch.frozen_grade_1 = min(int(batch.frozen_grade_1 or 0), max_1)
    batch.frozen_grade_2 = min(int(batch.frozen_grade_2 or 0), max_2)
    if changed:
        return "Frozen stock was reduced so this recipient could be implanted fresh."
    return None


def create_flush(session: Session, form: Any) -> EmbryoBatch:
    dam = _clean(form.get("dam"))
    if not dam:
        raise ValueError("Enter the dam.")
    sire_1, sire_2 = _parse_sires(form)
    flush_date = _parse_date(form.get("flush_date"), "the flush date")
    batch = EmbryoBatch(
        kind="flush",
        dam=dam,
        sire_1=sire_1,
        sire_2=sire_2,
        event_date=flush_date,
        frozen_grade_1=0,
        frozen_grade_2=0,
        freeze_confirmed=False,
    )
    session.add(batch)
    session.flush()
    return batch


def update_flush_plan(session: Session, batch_id: int, form: Any) -> None:
    batch = _load_batch(session, batch_id)
    if batch.kind != "flush":
        raise ValueError("Only a flush can be edited here.")
    dam = _clean(form.get("dam"))
    if not dam:
        raise ValueError("Enter the dam.")
    sire_1, sire_2 = _parse_sires(form)
    flush_date = _parse_date(form.get("flush_date"), "the flush date")
    batch.dam = dam
    batch.sire_1 = sire_1
    batch.sire_2 = sire_2
    batch.event_date = flush_date
    for row in _implants_of(batch, fresh=True):
        row.implant_date = flush_date
        row.due_date = _due_date(flush_date)


def save_flush_counts(session: Session, batch_id: int, form: Any) -> None:
    batch = _load_batch(session, batch_id)
    if batch.kind != "flush":
        raise ValueError("Counts are recorded on a flush.")
    grade_1 = _parse_count(form.get("grade_1"), "grade 1")
    grade_2 = _parse_count(form.get("grade_2"), "grade 2")
    unfertilised = _parse_count(form.get("unfertilised"), "unfertilised or degenerate")
    fresh = _implants_of(batch, fresh=True)
    if _grade_used(fresh, 1) > grade_1 or _grade_used(fresh, 2) > grade_2:
        raise ValueError("Those counts are lower than the recipients already implanted.")
    if grade_1 + grade_2 < len(fresh):
        raise ValueError("Grade 1 plus grade 2 is lower than the recipients already added.")
    batch.grade_1 = grade_1
    batch.grade_2 = grade_2
    batch.unfertilised = unfertilised
    if batch.freeze_confirmed:
        suggest_1, suggest_2 = suggest_freeze(batch)
        frozen_used = _implants_of(batch, fresh=False)
        batch.frozen_grade_1 = min(int(batch.frozen_grade_1 or 0), suggest_1)
        batch.frozen_grade_2 = min(int(batch.frozen_grade_2 or 0), suggest_2)
        if _grade_used(frozen_used, 1) > batch.frozen_grade_1 or _grade_used(frozen_used, 2) > batch.frozen_grade_2:
            raise ValueError("Lower the counts only after removing frozen implants that would no longer fit.")


def add_fresh_recipient(session: Session, batch_id: int, form: Any) -> str | None:
    batch = _load_batch(session, batch_id)
    if batch.kind != "flush":
        raise ValueError("Fresh recipients are added on flush day.")
    if batch.event_date is None:
        raise ValueError("Enter the flush date first.")
    recipient_id = _clean(form.get("recipient_id"))
    if not recipient_id:
        raise ValueError("Enter the recipient ID.")
    notice = _shrink_frozen_for_fresh_implant(batch)
    grade = _next_fresh_grade(batch)
    _assert_recipient_free(session, recipient_id)
    session.add(
        EmbryoImplant(
            batch_id=batch.id,
            recipient_id=recipient_id,
            implant_date=batch.event_date,
            is_fresh=True,
            grade=grade,
            due_date=_due_date(batch.event_date),
        )
    )
    session.flush()
    if notice:
        return f"Recipient added. {notice}"
    return None


def save_frozen_counts(session: Session, batch_id: int, form: Any) -> None:
    batch = _load_batch(session, batch_id)
    if batch.kind != "flush":
        raise ValueError("Freeze the surplus from a flush.")
    if not _grades_recorded(batch):
        raise ValueError("Enter the embryo counts first.")
    freeze_1 = _parse_count(form.get("frozen_grade_1"), "grade 1 to freeze")
    freeze_2 = _parse_count(form.get("frozen_grade_2"), "grade 2 to freeze")
    suggest_1, suggest_2 = suggest_freeze(batch)
    if freeze_1 > suggest_1 or freeze_2 > suggest_2:
        raise ValueError(
            f"You can freeze up to {suggest_1} grade 1 and {suggest_2} grade 2 "
            "after today's recipients."
        )
    frozen_used = _implants_of(batch, fresh=False)
    used_1 = _grade_used(frozen_used, 1)
    used_2 = _grade_used(frozen_used, 2)
    if freeze_1 < used_1 or freeze_2 < used_2:
        raise ValueError("That is fewer frozen embryos than have already been implanted.")
    batch.frozen_grade_1 = freeze_1
    batch.frozen_grade_2 = freeze_2
    batch.freeze_confirmed = True


def create_stock(session: Session, form: Any) -> EmbryoBatch:
    kind = _clean(form.get("kind"))
    if kind not in {"existing", "purchased"}:
        raise ValueError("Choose already in the tank, or purchased.")
    dam = _clean(form.get("dam"))
    if not dam:
        raise ValueError("Enter the dam.")
    sire_1, sire_2 = _parse_sires(form)
    grade_1 = _parse_count(form.get("grade_1"), "grade 1")
    grade_2 = _parse_count(form.get("grade_2"), "grade 2")
    if grade_1 + grade_2 < 1:
        raise ValueError("Enter at least one embryo.")
    acquired = _clean(form.get("event_date"))
    event_date = _parse_date(acquired, "the date") if acquired else None
    batch = EmbryoBatch(
        kind=kind,
        dam=dam,
        sire_1=sire_1,
        sire_2=sire_2,
        event_date=event_date,
        grade_1=grade_1,
        grade_2=grade_2,
        frozen_grade_1=grade_1,
        frozen_grade_2=grade_2,
        freeze_confirmed=True,
        note=_clean(form.get("note")) or None,
    )
    session.add(batch)
    session.flush()
    return batch


def add_frozen_recipient(session: Session, batch_id: int, form: Any) -> None:
    batch = _load_batch(session, batch_id)
    left_1, left_2 = _frozen_remaining(batch)
    if left_1 + left_2 < 1:
        raise ValueError("There are no frozen embryos left in that stock.")
    recipient_id = _clean(form.get("recipient_id"))
    if not recipient_id:
        raise ValueError("Enter the recipient ID.")
    implant_date = _parse_date(form.get("implant_date"), "the implant date")
    grade = _next_frozen_grade(batch)
    _assert_recipient_free(session, recipient_id)
    session.add(
        EmbryoImplant(
            batch_id=batch.id,
            recipient_id=recipient_id,
            implant_date=implant_date,
            is_fresh=False,
            grade=grade,
            due_date=_due_date(implant_date),
        )
    )
    session.flush()


def delete_implant(session: Session, implant_id: int) -> int:
    row = session.get(EmbryoImplant, implant_id)
    if row is None:
        raise ValueError("That recipient could not be found.")
    batch_id = row.batch_id
    session.delete(row)
    return batch_id


def delete_batch(session: Session, batch_id: int) -> None:
    batch = _load_batch(session, batch_id)
    if batch.implants:
        raise ValueError("Remove the recipients before deleting this.")
    session.delete(batch)


def update_implant(session: Session, implant_id: int, form: Any) -> None:
    row = session.get(EmbryoImplant, implant_id)
    if row is None:
        raise ValueError("That recipient could not be found.")
    pd_result = _clean(form.get("pd_result"))
    outcome = _clean(form.get("outcome"))
    if pd_result not in {key for key, _label in PD_CHOICES}:
        raise ValueError("Choose a PD result.")
    if outcome not in {key for key, _label in OUTCOME_CHOICES}:
        raise ValueError("Choose an outcome.")
    sex = _clean(form.get("calf_sex")).upper()
    if sex not in {key for key, _label in SEX_CHOICES}:
        raise ValueError("Choose heifer or bull for the calf sex.")
    if pd_result == "negative" and not outcome:
        outcome = "empty"
    if outcome == "empty" and not pd_result:
        pd_result = "negative"
    if outcome in _HELD_OUTCOMES and not pd_result:
        pd_result = "positive"
    if pd_result == "positive" and not outcome:
        outcome = "in_calf"
    row.pd_result = pd_result or None
    row.outcome = outcome or None
    row.calf_id = _clean(form.get("calf_id")) or None
    row.calf_sex = sex or None
    row.calf_dob = _parse_optional_date(form.get("calf_dob"))
    row.resolved_sire = _clean(form.get("resolved_sire")) or None


def _implant_dict(row: EmbryoImplant, batch: EmbryoBatch) -> dict[str, Any]:
    return {
        "id": row.id,
        "batch_id": batch.id,
        "recipient_id": row.recipient_id,
        "implant_date": row.implant_date.isoformat() if row.implant_date else "",
        "implant_date_label": format_date(row.implant_date),
        "is_fresh": bool(row.is_fresh),
        "fresh_label": "Fresh" if row.is_fresh else "Frozen",
        "grade": row.grade,
        "due_date_label": format_date(row.due_date),
        "pd_result": row.pd_result or "",
        "pd_label": _PD_LABELS.get(row.pd_result or "", "Not scanned"),
        "outcome": row.outcome or "",
        "outcome_label": _OUTCOME_LABELS.get(row.outcome or "", "No outcome yet"),
        "calf_id": row.calf_id or "",
        "calf_sex": row.calf_sex or "",
        "calf_dob": row.calf_dob.isoformat() if row.calf_dob else "",
        "calf_dob_label": format_date(row.calf_dob),
        "resolved_sire": row.resolved_sire or "",
        "sire_label": sire_label(batch.sire_1, batch.sire_2, row.resolved_sire),
        "dam": batch.dam,
        "open": _recipient_is_open(row),
    }


def _batch_dict(batch: EmbryoBatch) -> dict[str, Any]:
    suggest_1, suggest_2 = suggest_freeze(batch)
    left_1, left_2 = _frozen_remaining(batch)
    fresh = _implants_of(batch, fresh=True)
    grades_recorded = _grades_recorded(batch)
    viable = (int(batch.grade_1 or 0) + int(batch.grade_2 or 0)) if grades_recorded else None
    return {
        "id": batch.id,
        "kind": batch.kind,
        "kind_label": _KIND_LABELS.get(batch.kind, batch.kind),
        "dam": batch.dam,
        "sire_1": batch.sire_1,
        "sire_2": batch.sire_2 or "",
        "sire_label": sire_label(batch.sire_1, batch.sire_2),
        "event_date": batch.event_date.isoformat() if batch.event_date else "",
        "event_date_label": format_date(batch.event_date),
        "grade_1": batch.grade_1,
        "grade_2": batch.grade_2,
        "unfertilised": batch.unfertilised,
        "grades_recorded": grades_recorded,
        "viable": viable,
        "frozen_grade_1": int(batch.frozen_grade_1 or 0),
        "frozen_grade_2": int(batch.frozen_grade_2 or 0),
        "freeze_confirmed": bool(batch.freeze_confirmed),
        "freeze_suggest_1": suggest_1,
        "freeze_suggest_2": suggest_2,
        "ask_freeze": grades_recorded and (suggest_1 + suggest_2 > 0 or bool(batch.freeze_confirmed)),
        "fresh_count": len(fresh),
        "frozen_left_1": left_1,
        "frozen_left_2": left_2,
        "frozen_left": left_1 + left_2,
        "note": batch.note or "",
        "planned": batch.kind == "flush" and not grades_recorded and not batch.implants,
        "implants": [_implant_dict(row, batch) for row in batch.implants],
    }


def _load_batches(session: Session) -> list[EmbryoBatch]:
    return list(
        session.scalars(
            select(EmbryoBatch)
            .options(selectinload(EmbryoBatch.implants))
            .order_by(EmbryoBatch.id.asc())
        ).all()
    )


def list_flushes(session: Session) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    planned: list[dict[str, Any]] = []
    recorded: list[dict[str, Any]] = []
    for batch in _load_batches(session):
        if batch.kind != "flush":
            continue
        item = _batch_dict(batch)
        if item["planned"]:
            planned.append(item)
        else:
            recorded.append(item)
    planned.sort(key=lambda item: item["event_date"] or "")
    recorded.sort(key=lambda item: item["event_date"] or "", reverse=True)
    return planned, recorded


def get_flush(session: Session, batch_id: int) -> dict[str, Any]:
    batch = _load_batch(session, batch_id)
    if batch.kind != "flush":
        raise ValueError("That is frozen stock, not a flush.")
    return _batch_dict(batch)


def list_stock(session: Session) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for batch in _load_batches(session):
        item = _batch_dict(batch)
        if item["frozen_grade_1"] + item["frozen_grade_2"] <= 0:
            continue
        rows.append(item)
    rows.sort(key=lambda item: (item["frozen_left"] == 0, item["dam"].casefold()))
    return rows


def list_implants(session: Session, *, status: str = "all", query: str = "") -> list[dict[str, Any]]:
    wanted = (status or "all").strip().lower()
    needle = _clean(query).casefold()
    rows: list[dict[str, Any]] = []
    for batch in _load_batches(session):
        for implant in reversed(list(batch.implants)):
            item = _implant_dict(implant, batch)
            if needle and needle not in " ".join(
                [item["recipient_id"], item["dam"], item["sire_label"], item["calf_id"]]
            ).casefold():
                continue
            if wanted == "awaiting" and item["pd_result"]:
                continue
            if wanted == "in_calf" and not (item["pd_result"] == "positive" and item["open"]):
                continue
            if wanted == "calved" and item["outcome"] not in {"live_heifer", "bull"}:
                continue
            if wanted == "loss" and item["outcome"] not in {"abort", "stillbirth", "born_early", "recipient_died", "died_sold", "empty"}:
                continue
            rows.append(item)
    rows.sort(key=lambda item: item["implant_date"] or "", reverse=True)
    return rows


def donor_results(session: Session) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for batch in _load_batches(session):
        key = batch.dam.casefold()
        bucket = grouped.get(key)
        if bucket is None:
            bucket = {
                "dam": batch.dam,
                "implants": 0,
                "pd_checked": 0,
                "held": 0,
                "live_heifers": 0,
                "bulls": 0,
                "aborted": 0,
                "other_losses": 0,
            }
            grouped[key] = bucket
        for row in batch.implants:
            bucket["implants"] += 1
            if row.pd_result in {"positive", "negative"}:
                bucket["pd_checked"] += 1
            if row.pd_result == "positive":
                bucket["held"] += 1
            outcome = row.outcome or ""
            if outcome == "live_heifer":
                bucket["live_heifers"] += 1
            elif outcome == "bull":
                bucket["bulls"] += 1
            elif outcome == "abort":
                bucket["aborted"] += 1
            elif outcome in _OTHER_LOSSES:
                bucket["other_losses"] += 1
    results = [row for row in grouped.values() if row["implants"]]
    for row in results:
        if row["pd_checked"]:
            row["held_pct"] = f"{round(100 * row['held'] / row['pd_checked'])}%"
        else:
            row["held_pct"] = ""
    results.sort(key=lambda row: (-row["live_heifers"], -row["implants"], row["dam"].casefold()))
    return results
