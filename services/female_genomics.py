"""Female genomic list, and the embryo heifers matched to it."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from services.database import EmbryoImplant, GenomicResult, HerdBirth, HerdInventory
from services.embryos import format_date, sire_label
from services.genomic_import import eartag_match_key

TRAIT_FIELDS = (
    "pli",
    "milk_kg",
    "fat_kg",
    "protein_kg",
    "fat_pct",
    "protein_pct",
    "cci",
    "fertility_index",
    "scc",
    "life_span",
    "mastitis",
    "milking_speed",
    "type_merit",
    "mammary",
    "legs_and_feet",
    "stature",
    "chest_width",
    "body_depth",
    "mature_weight",
)


class _Identity:
    __slots__ = ("cow_id", "etag", "bdat", "from_inventory")

    def __init__(self, cow_id: str, etag: str, bdat: dt.date | None, from_inventory: bool) -> None:
        self.cow_id = cow_id
        self.etag = etag
        self.bdat = bdat
        self.from_inventory = from_inventory


def list_females(session: Session, *, embryo_only: bool = False) -> dict[str, Any]:
    """Return genomic heifers, optionally limited to embryo calves."""
    genomics = list(session.scalars(select(GenomicResult)).all())
    by_hbn = {row.hbn: row for row in genomics if row.hbn}
    identities, cow_keys = _identities(session)
    embryo_by_hbn = _embryo_matches(session, by_hbn, cow_keys)

    rows: list[dict[str, Any]] = []
    updated: dt.datetime | None = None
    for genomic in genomics:
        if embryo_only and genomic.hbn not in embryo_by_hbn:
            continue
        identity = identities.get(genomic.hbn or "")
        embryo = embryo_by_hbn.get(genomic.hbn or "")
        if genomic.updated_at is not None and (updated is None or genomic.updated_at > updated):
            updated = genomic.updated_at
        rows.append(_row(genomic, identity, embryo))

    return {
        "rows": rows,
        "count": len(rows),
        "updated_at": updated.isoformat() if updated else None,
    }


def _identities(session: Session) -> tuple[dict[str, _Identity], dict[str, list[str]]]:
    by_key: dict[str, _Identity] = {}
    cow_keys: dict[str, list[str]] = defaultdict(list)
    births = session.execute(select(HerdBirth.cow_id, HerdBirth.etag, HerdBirth.bdat)).all()
    inventory = session.execute(
        select(HerdInventory.cow_id, HerdInventory.etag, HerdInventory.bdat)
    ).all()
    for cow_id, etag, bdat in births:
        _remember(by_key, cow_keys, cow_id, etag, bdat, from_inventory=False)
    for cow_id, etag, bdat in inventory:
        _remember(by_key, cow_keys, cow_id, etag, bdat, from_inventory=True)
    return by_key, cow_keys


def _remember(
    by_key: dict[str, _Identity],
    cow_keys: dict[str, list[str]],
    cow_id: Any,
    etag: Any,
    bdat: dt.date | None,
    *,
    from_inventory: bool,
) -> None:
    key = eartag_match_key(etag)
    if not key:
        return
    incoming = _Identity(_norm_id(cow_id), str(etag or "").strip(), _as_date(bdat), from_inventory)
    by_key[key] = _prefer(by_key.get(key), incoming)
    herd_id = incoming.cow_id
    if herd_id and key not in cow_keys[herd_id]:
        cow_keys[herd_id].append(key)


def _prefer(current: _Identity | None, incoming: _Identity) -> _Identity:
    if current is None:
        return incoming
    if incoming.from_inventory and not current.from_inventory:
        return incoming
    if current.from_inventory and not incoming.from_inventory:
        return current
    if current.bdat is None and incoming.bdat is not None:
        return incoming
    if not current.cow_id and incoming.cow_id:
        return incoming
    return current


def _embryo_matches(
    session: Session,
    by_hbn: dict[str, GenomicResult],
    cow_keys: dict[str, list[str]],
) -> dict[str, EmbryoImplant]:
    matched: dict[str, EmbryoImplant] = {}
    implants = session.scalars(
        select(EmbryoImplant).options(selectinload(EmbryoImplant.batch))
    ).all()
    for implant in implants:
        if not _is_embryo_heifer(implant):
            continue
        hbn = _implant_hbn(implant, by_hbn, cow_keys)
        if not hbn:
            continue
        current = matched.get(hbn)
        if current is None or _implant_sort(implant) >= _implant_sort(current):
            matched[hbn] = implant
    return matched


def _implant_hbn(
    implant: EmbryoImplant,
    by_hbn: dict[str, GenomicResult],
    cow_keys: dict[str, list[str]],
) -> str | None:
    for key in cow_keys.get(_norm_id(implant.calf_id), []):
        if key in by_hbn:
            return key
    return None


def _is_embryo_heifer(implant: EmbryoImplant) -> bool:
    sex = (implant.calf_sex or "").strip().upper()
    if sex == "M" or implant.outcome == "bull":
        return False
    return implant.outcome == "live_heifer" or sex == "F"


def _implant_sort(implant: EmbryoImplant) -> tuple:
    return (implant.implant_date or dt.date.min, implant.id or 0)


def _row(
    genomic: GenomicResult,
    identity: _Identity | None,
    embryo: EmbryoImplant | None,
) -> dict[str, Any]:
    cow_id = identity.cow_id if identity else ""
    eartag = (genomic.eartag or (identity.etag if identity else "") or "").strip()
    animal = cow_id or (genomic.animal_name or "").strip() or eartag
    dob = _as_date(genomic.dob) or (identity.bdat if identity else None)
    if dob is None and embryo is not None:
        dob = _as_date(embryo.calf_dob)
    batch = embryo.batch if embryo is not None else None
    row: dict[str, Any] = {
        "animal": animal,
        "cow_id": cow_id,
        "eartag": eartag,
        "hbn": genomic.hbn or "",
        "dob": dob.isoformat() if dob else "",
        "dob_label": format_date(dob),
        "sire_name": (genomic.sire_name or "").strip(),
        "recipient_id": embryo.recipient_id if embryo else "",
        "dam": batch.dam if batch is not None else "",
        "due_date": embryo.due_date.isoformat() if embryo and embryo.due_date else "",
        "due_label": format_date(embryo.due_date) if embryo else "",
        "flush_sire": sire_label(batch.sire_1, batch.sire_2) if batch else "",
    }
    for field in TRAIT_FIELDS:
        value = getattr(genomic, field)
        row[field] = float(value) if isinstance(value, (int, float)) else None
    return row


def _norm_id(value: Any) -> str:
    text = str(value or "").strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text


def _as_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return None
