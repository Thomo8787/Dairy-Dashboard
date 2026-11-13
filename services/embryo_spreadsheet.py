"""Load historical implants from the embryo workbook."""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.database import EmbryoBatch, EmbryoImplant
from services.embryos import GESTATION_DAYS

IMPORT_NOTE = "Imported from the embryo spreadsheet."
_NA = {"", "null", "#n/a", "nan", "none"}
_X_SPLIT = re.compile(r"\s+[xX]\s+")


@dataclass
class SpreadsheetImplant:
    recipient_id: str
    implant_date: dt.date
    due_date: dt.date
    is_fresh: bool
    dam: str
    sire_1: str
    sire_2: str | None
    pd_result: str | None
    outcome: str | None
    calf_id: str | None
    calf_sex: str | None
    calf_dob: dt.date | None
    resolved_sire: str | None


def read_embryos_sheet(path: str) -> list[list[Any]]:
    from python_calamine import CalamineWorkbook

    workbook = CalamineWorkbook.from_path(path)
    return workbook.get_sheet_by_name("EMBRYOS").to_python()


def parse_embryos_sheet(rows: list[list[Any]]) -> tuple[list[SpreadsheetImplant], list[str]]:
    """Turn EMBRYOS rows into implants. The first row is the header."""
    implants: list[SpreadsheetImplant] = []
    skipped: list[str] = []
    for index, row in enumerate(rows[1:], start=2):
        try:
            implant = _parse_row(row)
        except ValueError as exc:
            skipped.append(f"Row {index}: {exc}")
            continue
        if implant is None:
            continue
        implants.append(implant)
    return implants, skipped


def replace_spreadsheet_import(session: Session, implants: list[SpreadsheetImplant]) -> dict[str, int]:
    """Replace a previous spreadsheet import and insert these implants."""
    removed = _delete_previous_import(session)
    groups: dict[tuple, list[SpreadsheetImplant]] = defaultdict(list)
    for implant in implants:
        key = (
            "flush" if implant.is_fresh else "existing",
            implant.dam.casefold(),
            implant.sire_1.casefold(),
            (implant.sire_2 or "").casefold(),
            implant.implant_date if implant.is_fresh else None,
        )
        groups[key].append(implant)

    flush_count = 0
    stock_count = 0
    for key, members in groups.items():
        kind = key[0]
        sample = members[0]
        if kind == "flush":
            batch = EmbryoBatch(
                kind="flush",
                dam=sample.dam,
                sire_1=sample.sire_1,
                sire_2=sample.sire_2,
                event_date=sample.implant_date,
                note=IMPORT_NOTE,
            )
            flush_count += 1
        else:
            batch = EmbryoBatch(
                kind="existing",
                dam=sample.dam,
                sire_1=sample.sire_1,
                sire_2=sample.sire_2,
                event_date=min(item.implant_date for item in members),
                frozen_grade_1=0,
                frozen_grade_2=0,
                freeze_confirmed=True,
                note=IMPORT_NOTE,
            )
            stock_count += 1
        session.add(batch)
        session.flush()
        for item in members:
            session.add(
                EmbryoImplant(
                    batch_id=batch.id,
                    recipient_id=item.recipient_id,
                    implant_date=item.implant_date,
                    due_date=item.due_date,
                    is_fresh=item.is_fresh,
                    grade=None,
                    pd_result=item.pd_result,
                    outcome=item.outcome,
                    calf_id=item.calf_id,
                    calf_sex=item.calf_sex,
                    calf_dob=item.calf_dob,
                    resolved_sire=item.resolved_sire,
                )
            )
    session.flush()
    return {
        "removed_batches": removed,
        "flushes": flush_count,
        "stock_batches": stock_count,
        "implants": len(implants),
    }


def _delete_previous_import(session: Session) -> int:
    batches = session.scalars(select(EmbryoBatch).where(EmbryoBatch.note == IMPORT_NOTE)).all()
    for batch in batches:
        session.delete(batch)
    session.flush()
    return len(batches)


def _parse_row(row: list[Any]) -> SpreadsheetImplant | None:
    recipient = _text(row, 1)
    implant_date = _date(row, 2)
    if not recipient and implant_date is None:
        return None
    if not recipient:
        raise ValueError("missing recipient")
    if implant_date is None:
        raise ValueError(f"recipient {recipient} has no implant date")

    fresh_flag = _text(row, 3).casefold()
    is_fresh = fresh_flag != "frozen"
    embryo = _text(row, 4)
    dam, sire_1, sire_2 = _dam_and_sires(_text(row, 28), embryo)
    due = _date(row, 7) or (implant_date + dt.timedelta(days=GESTATION_DAYS))
    pd_result = _pd(_cell(row, 5))
    outcome = _outcome(row, pd_result)
    calf_dob = _date(row, 19)
    if calf_dob is not None and not 2018 <= calf_dob.year <= 2035:
        calf_dob = None
    return SpreadsheetImplant(
        recipient_id=recipient,
        implant_date=implant_date,
        due_date=due,
        is_fresh=is_fresh,
        dam=dam,
        sire_1=sire_1,
        sire_2=sire_2,
        pd_result=pd_result,
        outcome=outcome,
        calf_id=_text(row, 16) or None,
        calf_sex=_sex(_text(row, 18)),
        calf_dob=calf_dob,
        resolved_sire=_resolved_sire(_text(row, 21), sire_1, sire_2),
    )


def _dam_and_sires(short_name: str, embryo: str) -> tuple[str, str, str | None]:
    dam = short_name
    sires: list[str] = []
    parts = _X_SPLIT.split(embryo, maxsplit=1) if embryo else []
    if len(parts) == 2:
        if not dam:
            dam = parts[0].strip()
        sires = _split_sires(parts[1])
    elif embryo and not dam:
        bits = _split_sires(embryo)
        if len(bits) >= 2:
            dam = bits[0]
            sires = bits[1:]
        elif bits:
            dam = bits[0]
    elif embryo:
        sires = _split_sires(embryo)
    dam = dam or "Unknown"
    sire_1 = sires[0] if sires else "Unknown"
    sire_2 = sires[1] if len(sires) > 1 else None
    return dam, sire_1, sire_2


def _split_sires(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"\s*/\s*", text) if part.strip()]


def _resolved_sire(recorded: str, sire_1: str, sire_2: str | None) -> str | None:
    if not recorded or recorded.casefold() == "unknown":
        return None
    if not sire_2:
        if recorded.casefold() == sire_1.casefold():
            return None
        return recorded
    return recorded


def _outcome(row: list[Any], pd_result: str | None) -> str | None:
    if _flag(row, 15):
        return "live_heifer"
    if _flag(row, 14):
        return "bull"
    if _flag(row, 9):
        return "abort"
    if _flag(row, 12):
        return "stillbirth"
    if _flag(row, 11):
        return "born_early"
    if _flag(row, 10):
        return "recipient_died"
    if _flag(row, 13):
        return "died_sold"
    reason = _text(row, 8).casefold()
    if "abort" in reason:
        return "abort"
    if "still" in reason or "twin" in reason:
        return "stillbirth"
    if "early" in reason:
        return "born_early"
    if "recip" in reason:
        return "recipient_died"
    if reason in {"died", "sold", "dead"} or "died" in reason or "sold" in reason:
        return "died_sold"
    if reason in {"bull", "bull check"}:
        return "bull"
    if pd_result == "negative":
        return "empty"
    if pd_result == "positive":
        return "in_calf"
    return None


def _pd(value: Any) -> str | None:
    if value in (1, 1.0, "1"):
        return "positive"
    if value in (0, 0.0, "0"):
        return "negative"
    return None


def _sex(value: str) -> str | None:
    token = value.casefold()
    if token in {"f", "female", "heifer"}:
        return "F"
    if token in {"m", "male", "bull"}:
        return "M"
    return None


def _flag(row: list[Any], index: int) -> bool:
    value = _cell(row, index)
    if value in (None, "", 0, 0.0, False):
        return False
    return True


def _cell(row: list[Any], index: int) -> Any:
    if index >= len(row):
        return None
    return row[index]


def _text(row: list[Any], index: int) -> str:
    value = _cell(row, index)
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    if text.casefold() in _NA:
        return ""
    return text


def _date(row: list[Any], index: int) -> dt.date | None:
    value = _cell(row, index)
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return None
