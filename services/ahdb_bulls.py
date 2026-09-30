"""Fetch AHDB Holstein bull lists from the public breeding-dairy API.

Uses the same endpoint the tables page calls (not HTML scraping).
AHDB allows individual records to be downloaded for evaluating or comparing
animals; do not republish or commercially exploit the dataset.

https://breedingdairy.ahdb.org.uk/resources/data-usage-statement/
"""

from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.custom_indexes import attach_custom_indexes, load_index_settings
from services.database import (
    AhdbBull,
    _clean_bull_name,
    _float_or_none,
    _int_or_none,
    _str_or_none,
)

AHDB_BASE = "https://breedingdairy.ahdb.org.uk"
BULL_DATA_PATH = "/umbraco/api/bulldata/getbulldata"

REPORTS: dict[str, dict[str, str]] = {
    "genomic": {
        "table": "SAC_Bull_report_pli_Young_Bulls_Active_HOL",
        "label": "HOL bulls - Available genomic",
        "page": "/bull-and-cow-reports/tables?t=SAC_Bull_report_pli_Young_Bulls_Active_HOL",
        "filename": "HOL_bulls_available_genomic.csv",
    },
    "proven": {
        "table": "SAC_Bull_report_pli_Int_semen_available_HOL",
        "label": "HOL bulls - Available proven",
        "page": "/bull-and-cow-reports/tables?t=SAC_Bull_report_pli_Int_semen_available_HOL",
        "filename": "HOL_bulls_available_proven.csv",
    },
    "international": {
        "table": "SAC_Bull_report_pli_Int_HOL",
        "label": "HOL bulls - Top international",
        "page": "/bull-and-cow-reports/tables?t=SAC_Bull_report_pli_Int_HOL",
        "filename": "HOL_bulls_top_international.csv",
    },
}

STORED_TYPES = ("genomic", "proven")
SOURCE_PRIORITY = {"proven": 0, "international": 1, "genomic": 2}

_EXTRA_FIELDS: list[tuple[str, str]] = [
    ("bull_hbn_emn", "HBN"),
    ("bull_name_full", "Bull Name Full"),
    ("sire_name", "Sire Name"),
    ("grandsire_name", "MGS Name"),
    ("straicompany_ni", "Supplier NI"),
    ("straicompanyni", "Supplier NI"),
    ("webaddress", "Supplier URL"),
]

_USER_AGENT = "Thomasson-Dashboard/1.0 (AHDB bull list fetch for on-farm breeding)"


def clean_bull_name(name: str | None) -> str | None:
    """Drop AHDB haplotype / kappa-casein codes from the display name."""
    return _clean_bull_name(name)


class AhdbBullsError(Exception):
    """AHDB bull-list request failed."""


@dataclass(frozen=True)
class AhdbReport:
    key: str
    table: str
    label: str
    rows: list[dict[str, Any]]
    columns: list[tuple[str, str]]
    fetched_at: dt.datetime


def fetch_report(key: str, *, client: httpx.Client | None = None) -> AhdbReport:
    spec = REPORTS.get(key)
    if spec is None:
        raise AhdbBullsError(f"Unknown report {key!r}. Choose: {', '.join(REPORTS)}")

    owns_client = client is None
    if client is None:
        client = httpx.Client(
            base_url=AHDB_BASE,
            timeout=60.0,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
    try:
        response = client.get(BULL_DATA_PATH, params={"strTableName": spec["table"]})
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise AhdbBullsError(f"Failed to fetch {spec['label']}: {exc}") from exc
    finally:
        if owns_client:
            client.close()

    metadata = payload.get("metadata") or []
    data = payload.get("data") or []
    if not isinstance(data, list):
        raise AhdbBullsError(f"Unexpected payload for {spec['label']}")

    columns = _columns_from_metadata(metadata, data)
    return AhdbReport(
        key=key,
        table=spec["table"],
        label=spec["label"],
        rows=data,
        columns=columns,
        fetched_at=dt.datetime.now(dt.timezone.utc),
    )


def write_csv(report: AhdbReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [title for _, title in report.columns]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in report.rows:
            writer.writerow({title: _cell(row, name) for name, title in report.columns})
    return path


def fetch_and_write(
    out_dir: Path,
    *,
    keys: list[str] | None = None,
) -> list[tuple[AhdbReport, Path]]:
    written: list[tuple[AhdbReport, Path]] = []
    for report in fetch_reports(keys=keys):
        dest = out_dir / REPORTS[report.key]["filename"]
        write_csv(report, dest)
        written.append((report, dest))
    return written


def fetch_reports(*, keys: list[str] | None = None) -> list[AhdbReport]:
    chosen = keys or list(REPORTS)
    reports: list[AhdbReport] = []
    with httpx.Client(
        base_url=AHDB_BASE,
        timeout=60.0,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
    ) as client:
        for key in chosen:
            reports.append(fetch_report(key, client=client))
    return reports


def bull_from_row(list_type: str, row: dict[str, Any], fetched_at: dt.datetime) -> AhdbBull | None:
    hbn = _str_or_none(row.get("bull_hbn_emn"))
    if hbn is None:
        return None
    supplier_ni = _str_or_none(row.get("straicompany_ni")) or _str_or_none(
        row.get("straicompanyni")
    )
    return AhdbBull(
        list_type=list_type,
        hbn=hbn,
        rank=_int_or_none(row.get("rank")),
        bull_name=clean_bull_name(row.get("bull_name")),
        bull_name_full=_str_or_none(row.get("bull_name_full"))
        or _str_or_none(row.get("bull_name")),
        breed_code=_str_or_none(row.get("bull_breed_code")),
        pli=_float_or_none(row.get("pli")),
        pli_reliability=_float_or_none(row.get("plireliability")),
        milk_kg=_float_or_none(row.get("pta_milk_lac_all")),
        fat_kg=_float_or_none(row.get("pta_fat_lac_all")),
        protein_kg=_float_or_none(row.get("pta_protein_lac_all")),
        fat_pct=_float_or_none(row.get("pta_fat_perc_lac_all")),
        protein_pct=_float_or_none(row.get("pta_protein_perc_lac_all")),
        healthycow=_float_or_none(row.get("healthycow")),
        envirocow=_float_or_none(row.get("envirocow")),
        fertility_index=_float_or_none(row.get("fertiltiy_index")),
        calf_survival=_float_or_none(row.get("calfsurvival")),
        lifespan_days=_float_or_none(row.get("lifespanindays")),
        scc=_float_or_none(row.get("pta_scc")),
        mastitis=_float_or_none(row.get("mastitis")),
        lameness=_float_or_none(row.get("lameness")),
        digital_dermatitis=_float_or_none(row.get("digitaldermatitis")),
        gestation_length=_float_or_none(row.get("gestationlength")),
        dairy_carcass_index=_float_or_none(row.get("dairycarcassindex")),
        maintenance=_float_or_none(row.get("efficiency")),
        feed_advantage=_float_or_none(row.get("feedadvantage")),
        direct_ce=_float_or_none(row.get("direct_ce")),
        maternal_ce=_float_or_none(row.get("maternal_ce")),
        tb_advantage=_float_or_none(row.get("tbadvantage")),
        legs=_float_or_none(row.get("sbv_fandl")),
        udder=_float_or_none(row.get("sbv_mam")),
        type_merit=_float_or_none(row.get("sbv_tm")),
        supplier_gb=_str_or_none(row.get("straicompany")),
        supplier_ni=supplier_ni,
        genomic_indicator=_str_or_none(row.get("genomicindicator")),
        sexed_gb=_str_or_none(row.get("strsexed")),
        uk_proven=_str_or_none(row.get("uk_proven")),
        sire_name=_str_or_none(row.get("sire_name")),
        grandsire_name=_str_or_none(row.get("grandsire_name")),
        supplier_url=_str_or_none(row.get("webaddress")),
        fetched_at=fetched_at,
    )


def stored_list_type(source_key: str) -> str:
    return "proven" if source_key == "international" else source_key


def _snapshot_bull(bull: AhdbBull) -> AhdbBull:
    values = {
        column.name: getattr(bull, column.name)
        for column in AhdbBull.__table__.columns
        if column.name != "id"
    }
    return AhdbBull(**values)


def _recalculate_ranks(bulls: list[AhdbBull]) -> None:
    ordered = sorted(
        bulls,
        key=lambda item: (
            -(item.pli if item.pli is not None else float("-inf")),
            item.bull_name or "",
            item.hbn,
        ),
    )
    for index, bull in enumerate(ordered, start=1):
        bull.rank = index


def import_reports(db: Session, reports: list[AhdbReport]) -> dict[str, Any]:
    affected = {stored_list_type(report.key) for report in reports}
    by_hbn: dict[str, tuple[int, AhdbBull]] = {}
    if affected != set(STORED_TYPES):
        for existing in db.scalars(select(AhdbBull)).all():
            if existing.list_type in affected:
                continue
            by_hbn[existing.hbn] = (10, _snapshot_bull(existing))

    skipped = 0
    for report in reports:
        stored = stored_list_type(report.key)
        priority = SOURCE_PRIORITY.get(report.key, 9)
        fetched_at = report.fetched_at.replace(tzinfo=None)
        for row in report.rows:
            bull = bull_from_row(stored, row, fetched_at)
            if bull is None:
                skipped += 1
                continue
            current = by_hbn.get(bull.hbn)
            if current is None or priority < current[0]:
                by_hbn[bull.hbn] = (priority, bull)
                continue
            if priority == current[0] and (bull.pli or float("-inf")) > (
                current[1].pli or float("-inf")
            ):
                by_hbn[bull.hbn] = (priority, bull)

    bulls = [item[1] for item in by_hbn.values()]
    _recalculate_ranks(bulls)
    db.query(AhdbBull).delete()
    for bull in bulls:
        db.add(bull)
    db.commit()
    return {"rows_imported": len(bulls), "rows_skipped": skipped}


def refresh_bulls(db: Session, *, keys: list[str] | None = None) -> dict[str, Any]:
    reports = fetch_reports(keys=keys)
    result = import_reports(db, reports)
    listed = list_bulls(db)
    result.update(listed)
    return result


def list_bulls(db: Session) -> dict[str, Any]:
    settings = load_index_settings(db)
    rows = list(db.scalars(select(AhdbBull).order_by(AhdbBull.rank, AhdbBull.bull_name)).all())
    counts = {
        key: db.scalar(
            select(func.count()).select_from(AhdbBull).where(AhdbBull.list_type == key)
        )
        or 0
        for key in STORED_TYPES
    }
    fetched_at = db.scalar(select(func.max(AhdbBull.fetched_at)))
    return {
        "rows": [attach_custom_indexes(row.to_dict(), settings) for row in rows],
        "count": len(rows),
        "counts": counts,
        "fetched_at": fetched_at.isoformat() if fetched_at else None,
        "index_settings": settings,
    }


def ensure_imported(db: Session) -> dict[str, Any]:
    missing_stored = [
        key
        for key in STORED_TYPES
        if not (
            db.scalar(
                select(func.count()).select_from(AhdbBull).where(AhdbBull.list_type == key)
            )
            or 0
        )
    ]
    if not missing_stored:
        return list_bulls(db)
    keys: list[str] = []
    if "genomic" in missing_stored:
        keys.append("genomic")
    if "proven" in missing_stored:
        keys.extend(["proven", "international"])
    return refresh_bulls(db, keys=keys)


def _columns_from_metadata(
    metadata: list[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    columns: list[tuple[str, str]] = []
    seen_titles: set[str] = set()
    for item in metadata:
        name = str(item.get("name") or "").strip()
        title = str(item.get("title") or name).replace("\xa0", " ").strip()
        if not name:
            continue
        columns.append((name, title))
        seen_titles.add(title)

    sample_keys = set(rows[0].keys()) if rows else set()
    used_names = {name for name, _ in columns}
    for name, title in _EXTRA_FIELDS:
        if name in used_names or name not in sample_keys or title in seen_titles:
            continue
        columns.append((name, title))
        seen_titles.add(title)
    return columns


def _cell(row: dict[str, Any], name: str) -> Any:
    value = row.get(name)
    if isinstance(value, str):
        return value.strip()
    return value
