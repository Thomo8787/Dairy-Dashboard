"""Female genomic list, and the embryo heifers matched to it."""

from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from services.database import EmbryoImplant, GenomicResult, HerdBirth, HerdInventory
from services.embryos import format_date, sire_label
from services.genomic_import import eartag_match_key, load_female_columns

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
    visual_to_hbn = _visual_index(genomics)
    identities, cow_keys = _identities(session)
    embryo_by_hbn = _embryo_matches(session, by_hbn, cow_keys, visual_to_hbn)
    farm_by_id, farm_by_tag = _inventory_farms(session)

    rows: list[dict[str, Any]] = []
    updated: dt.datetime | None = None
    for genomic in genomics:
        if embryo_only and genomic.hbn not in embryo_by_hbn:
            continue
        identity = identities.get(genomic.hbn or "")
        embryo = embryo_by_hbn.get(genomic.hbn or "")
        if genomic.updated_at is not None and (updated is None or genomic.updated_at > updated):
            updated = genomic.updated_at
        rows.append(_row(genomic, identity, embryo, farm_by_id, farm_by_tag))

    return {
        "rows": rows,
        "count": len(rows),
        "columns": load_female_columns(session),
        "updated_at": updated.isoformat() if updated else None,
    }


def _inventory_farms(session: Session) -> tuple[dict[str, str], dict[str, str]]:
    """Map current inventory cow numbers and eartag keys to a farm code."""
    by_id: dict[str, str] = {}
    by_tag: dict[str, str] = {}
    for cow_id, etag, farm in session.execute(
        select(HerdInventory.cow_id, HerdInventory.etag, HerdInventory.farm)
    ):
        code = str(farm or "").strip()
        herd_id = _norm_id(cow_id)
        if herd_id and herd_id not in by_id:
            by_id[herd_id] = code
        key = eartag_match_key(etag)
        if key and key not in by_tag:
            by_tag[key] = code
    return by_id, by_tag


def _herd_farm(
    genomic: GenomicResult,
    identity: _Identity | None,
    farm_by_id: dict[str, str],
    farm_by_tag: dict[str, str],
) -> str | None:
    if genomic.hbn and genomic.hbn in farm_by_tag:
        return farm_by_tag[genomic.hbn]
    visual = _norm_id(genomic.visual_id)
    if visual and visual in farm_by_id:
        return farm_by_id[visual]
    if identity and identity.from_inventory and identity.cow_id in farm_by_id:
        return farm_by_id[identity.cow_id]
    return None


def _visual_index(genomics: list[GenomicResult]) -> dict[str, str]:
    found: dict[str, str] = {}
    for genomic in genomics:
        herd_id = _norm_id(genomic.visual_id)
        if herd_id and genomic.hbn and herd_id not in found:
            found[herd_id] = genomic.hbn
    return found


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
    visual_to_hbn: dict[str, str],
) -> dict[str, EmbryoImplant]:
    matched: dict[str, EmbryoImplant] = {}
    implants = session.scalars(
        select(EmbryoImplant).options(selectinload(EmbryoImplant.batch))
    ).all()
    for implant in implants:
        if not _is_embryo_heifer(implant):
            continue
        hbn = _implant_hbn(implant, by_hbn, cow_keys, visual_to_hbn)
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
    visual_to_hbn: dict[str, str],
) -> str | None:
    herd_id = _norm_id(implant.calf_id)
    visual = visual_to_hbn.get(herd_id)
    if visual and visual in by_hbn:
        return visual
    for key in cow_keys.get(herd_id, []):
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
    farm_by_id: dict[str, str],
    farm_by_tag: dict[str, str],
) -> dict[str, Any]:
    cow_id = identity.cow_id if identity else ""
    visual_id = _norm_id(genomic.visual_id) or cow_id
    eartag = (genomic.eartag or (identity.etag if identity else "") or "").strip()
    animal = visual_id or (genomic.animal_name or "").strip() or eartag
    dob = _as_date(genomic.dob) or (identity.bdat if identity else None)
    if dob is None and embryo is not None:
        dob = _as_date(embryo.calf_dob)
    batch = embryo.batch if embryo is not None else None
    herd_farm = _herd_farm(genomic, identity, farm_by_id, farm_by_tag)
    row: dict[str, Any] = {
        "animal": animal,
        "cow_id": cow_id,
        "visual_id": visual_id,
        "eartag": eartag,
        "hbn": genomic.hbn or "",
        "dob": dob.isoformat() if dob else "",
        "dob_label": format_date(dob),
        "sire_name": (genomic.sire_name or "").strip(),
        "data_type": (genomic.data_type or "").strip(),
        "herd_status": "on_farm" if herd_farm is not None else "left",
        "herd_farm": herd_farm if herd_farm else "LEFT",
        "recipient_id": embryo.recipient_id if embryo else "",
        "dam": batch.dam if batch is not None else "",
        "due_date": embryo.due_date.isoformat() if embryo and embryo.due_date else "",
        "due_label": format_date(embryo.due_date) if embryo else "",
        "flush_sire": sire_label(batch.sire_1, batch.sire_2) if batch else "",
    }
    for field in TRAIT_FIELDS:
        value = getattr(genomic, field)
        row[field] = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    traits = _trait_values(genomic.traits)
    row.update(traits)
    row["data_type"] = (genomic.data_type or "").strip()
    row["herd_farm"] = herd_farm if herd_farm else "LEFT"
    return row


def _trait_values(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


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
