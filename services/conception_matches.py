"""Saved sire and technician names that should be shown as one row."""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.database import ConceptionNameMatch

KINDS = frozenset({"sire", "technician"})
NOT_RECORDED = "Not recorded"


def _clean(kind: str, value: str) -> str:
    text = (value or "").strip()
    if kind == "sire":
        text = text.upper()
    return text


def _require_kind(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError("Choose sires or technicians.")
    return kind


def load_matches(db: Session, kind: str) -> list[tuple[str, str]]:
    """Spelling and the name it is shown as, for one kind."""
    if kind not in KINDS:
        return []
    rows = db.scalars(
        select(ConceptionNameMatch)
        .where(ConceptionNameMatch.kind == kind)
        .order_by(ConceptionNameMatch.display_name, ConceptionNameMatch.name)
    ).all()
    return [(row.name, row.display_name) for row in rows]


def match_map(records: list[tuple[str, str]]) -> dict[str, str]:
    return {name: display for name, display in records}


def active_spellings(records: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Spellings that still belong to a match, with the name they are shown as."""
    groups: dict[str, list[str]] = defaultdict(list)
    for name, display in records:
        groups[display].append(name)
    spellings: list[tuple[str, str]] = []
    for display, names in groups.items():
        if len(names) > 1 or (len(names) == 1 and names[0] != display):
            spellings.extend((name, display) for name in names)
    spellings.sort(key=lambda item: (item[1].lower(), item[0].lower()))
    return spellings


def _ordered_names(kind: str, names: list[str]) -> list[str]:
    ordered: list[str] = []
    for value in names:
        text = _clean(kind, str(value))
        if not text or text.casefold() == NOT_RECORDED.casefold():
            raise ValueError("Not recorded cannot be matched.")
        if text not in ordered:
            ordered.append(text)
    return ordered


def save_match(db: Session, kind: str, names: list[str]) -> None:
    """Join the selected rows. The first name is the one that stays on the row."""
    kind = _require_kind(kind)
    if not isinstance(names, list):
        raise ValueError("Select the rows to match.")
    ordered = _ordered_names(kind, names)
    if len(ordered) < 2:
        raise ValueError("Select at least two names. The first one you select is the name that stays.")

    rows = db.scalars(select(ConceptionNameMatch).where(ConceptionNameMatch.kind == kind)).all()
    by_display: dict[str, list[ConceptionNameMatch]] = defaultdict(list)
    by_name = {row.name: row for row in rows}
    for row in rows:
        by_display[row.display_name].append(row)

    display = ordered[0]
    spellings: list[str] = []
    for label in ordered:
        members = by_display.get(label) or []
        if members:
            for row in members:
                if row.name not in spellings:
                    spellings.append(row.name)
        elif label not in spellings:
            spellings.append(label)

    for name in spellings:
        existing = by_name.get(name)
        if existing is None:
            db.add(ConceptionNameMatch(kind=kind, name=name, display_name=display))
        else:
            existing.display_name = display
    db.flush()


def remove_match(db: Session, kind: str, names: list[str]) -> None:
    """Separate the selected spellings. Names left unticked stay matched."""
    kind = _require_kind(kind)
    if not isinstance(names, list):
        raise ValueError("Select a name to unmatch.")
    ordered = _ordered_names(kind, names)
    if not ordered:
        raise ValueError("Select a name to unmatch.")

    rows = db.scalars(select(ConceptionNameMatch).where(ConceptionNameMatch.kind == kind)).all()
    chosen = set(ordered)
    removed = False
    for row in rows:
        if row.name in chosen:
            db.delete(row)
            removed = True
    if not removed:
        raise ValueError("Those names are not matched.")
    db.flush()

    remaining = db.scalars(
        select(ConceptionNameMatch).where(ConceptionNameMatch.kind == kind)
    ).all()
    groups: dict[str, list[ConceptionNameMatch]] = defaultdict(list)
    for row in remaining:
        groups[row.display_name].append(row)
    for display, members in groups.items():
        if len(members) == 1 and members[0].name == display:
            db.delete(members[0])
    db.flush()
