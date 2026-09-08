"""SenseHub (SCR Allflex) HTTP client.

Talks to the same REST API the https://st.scrdairy.com Angular app uses:
login, then reports through the farm reverse-proxy.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
import uuid
from typing import Any

import httpx

from zoneinfo import ZoneInfo

from services.sensehub_config import (
    SENSEHUB_DOMAIN_RESOLVER_URL,
    SENSEHUB_FARM_ID,
    SENSEHUB_LOGIN_BASE,
    SENSEHUB_PASSWORD,
    SENSEHUB_PROXY_BASE,
    SENSEHUB_REGION,
    SENSEHUB_USERNAME,
    sensehub_is_configured,
)

REPORT_TITLES: dict[str, str] = {
    "AnimalsInHeat": "Animals in heat",
    "AnimalsToInspect": "Animals to inspect",
    "AnimalDistress": "Animal distress",
    "Health": "Health",
    "YoungStockHealth": "Young Stock Health by Age",
    "Young Stock Health by Age All": "Young Stock Health by Age All",
    "Young Stock Health by Age": "Young Stock Health by Age",
    "AnestrusCows": "Anestrus cows",
    "IrregularHeats": "Irregular heats",
    "SuspectedForAbortion": "Suspected for abortion",
    "PregnancyChance": "Pregnancy chance",
    "HeatReportHiddenByGroupActivityReport": "Heat hidden by group activity",
    "FertilityOverview": "Fertility overview",
    "ExpectedCalvingAndDryOff": "Expected calving and dry-off",
    "EarlyFreshCows": "Early fresh cows",
    "GroupRoutine": "Group routine",
    "GroupRoutineHeatStress": "Group routine — heat stress",
    "TagMaintenanceCalls": "Tag maintenance",
    "Tags": "Tags",
    "ConnectivityProblems": "Connectivity issues",
    "Animals in Herd": "Animals in Herd",
    "AnimalsInHerd": "Animals in Herd",
}

DEFAULT_REPORT = "Young Stock Health by Age All"
HERD_REPORT = "Animals in Herd"
NO_DATA_REPORT = "No Data"
ANIMAL_LIST_SNAPSHOT_KEY = 900001

FIELD_LABELS: dict[str, str] = {
    "AnimalID": "Animal ID",
    "YoungStockHealthIndex": "Health index",
    "AgeInDays": "Age (days)",
    "DailyRumination": "Daily rumination",
    "DailyEatingTime": "Daily eating",
    "CowGroupName": "Group",
    "GroupName": "Group",
}

_YOUNG_STOCK_COLUMNS = (
    "AnimalID",
    "YoungStockHealthIndex",
    "AgeInDays",
    "DailyEatingTime",
    "DailyRumination",
    "CowGroupName",
    "GroupName",
)

REPORT_COLUMN_ORDER: dict[str, tuple[str, ...]] = {
    "YoungStockHealth": _YOUNG_STOCK_COLUMNS,
    "Young Stock Health by Age All": _YOUNG_STOCK_COLUMNS,
    "Young Stock Health by Age": _YOUNG_STOCK_COLUMNS,
}

PRIORITY_REPORTS: tuple[str, ...] = (
    "Young Stock Health by Age All",
    "Young Stock Health by Age",
    "YoungStockHealth",
    HERD_REPORT,
    "AnimalsInHerd",
    "AnimalsInHeat",
    "AnimalsToInspect",
    "AnimalDistress",
    "Health",
    "AnestrusCows",
    "EarlyFreshCows",
    "ExpectedCalvingAndDryOff",
)

_HIDDEN_FIELD_SUFFIXES = ("Badge",)
_HIDDEN_FIELDS = {
    "rowNumber",
    "rowId",
    "rowType",
    "parentRowId",
    "rowNumberInGroup",
    "CowDatabaseID",
    "ExternalID",
    "CowDbId",
}

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_REPORT_NAME_COMPACT_RE = re.compile(r"[^a-z0-9]+")


def compact_report_name(name: str | None) -> str:
    return _REPORT_NAME_COMPACT_RE.sub("", (name or "").casefold())


def is_herd_report(name: str | None) -> bool:
    compact = compact_report_name(name)
    return compact in {"animalsinherd", "animallist"}


class SenseHubError(Exception):
    """SenseHub API or configuration error."""


class SenseHubAuthError(SenseHubError):
    """Login failed or credentials are missing."""


class SenseHubConfigError(SenseHubError):
    """Username / password / farm ID not configured."""


def _unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and "result" in payload:
        return payload["result"]
    return payload


def _basic_auth(username: str, farm_id: str, region: str, password: str) -> str:
    raw = f"{username}_{farm_id}_{region}:{password}"
    return "Basic " + base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _correlation_id() -> str:
    return str(uuid.uuid4())


def _client_headers(
    *,
    token: str | None = None,
    display_version: str | None = None,
) -> dict[str, str]:
    farm_id = SENSEHUB_FARM_ID
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0Z")
    version = display_version or "dashboard"
    headers = {
        "farmId": farm_id,
        "SCR-Farm-Id": farm_id,
        "scr-client-correlationid": _correlation_id(),
        "Accept": "application/json",
        "SCR-Client-Time": json.dumps({"dateTime": now}),
        "SCR-Client-Type": json.dumps(
            {"name": "optional", "type": "web", "version": version}
        ),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def humanize_field(name: str) -> str:
    stripped = name.removesuffix("Calculation")
    if stripped in FIELD_LABELS:
        return FIELD_LABELS[stripped]
    spaced = _CAMEL_RE.sub(" ", stripped.replace("ID", " ID"))
    return re.sub(r"\s+", " ", spaced).strip()


def field_key(name: str) -> str:
    return name.removesuffix("Calculation")


def _is_unix_seconds(value: Any) -> bool:
    return isinstance(value, (int, float)) and 1_000_000_000 <= float(value) < 4_000_000_000


def _stringify(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        primary = (
            value.get("mostImportantReport")
            or value.get("mostImportantReport")
            or value.get("primaryReport")
            or value.get("name")
            or value.get("value")
        )
        rest = (
            value.get("restOfReports")
            or value.get("restOfReports")
            or value.get("otherReports")
            or []
        )
        if primary:
            extras = [str(item) for item in rest if item]
            return ", ".join([str(primary), *extras]) if extras else str(primary)
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return value


def flatten_row(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for raw_key, value in row.items():
        key = field_key(str(raw_key))
        if key in _HIDDEN_FIELDS or key.endswith(_HIDDEN_FIELD_SUFFIXES):
            continue
        if _is_unix_seconds(value) and any(
            token in key.lower() for token in ("time", "updated", "date")
        ):
            out[key] = dt.datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M")
        else:
            out[key] = _stringify(value)
    return out


def flatten_report(raw: dict[str, Any], *, catalog_item: dict[str, Any] | None = None) -> dict[str, Any]:
    body = _unwrap(raw) or {}
    meta = body.get("meta") or body.get("meta") or {}
    rows_in = body.get("rows") or body.get("rows") or []
    rows = [flatten_row(row) for row in rows_in if isinstance(row, dict)]
    seen: list[str] = []
    for row in rows:
        for key in row:
            if key not in seen:
                seen.append(key)
    name = (
        (catalog_item or {}).get("name")
        or meta.get("reportName")
        or meta.get("reportName")
        or "Report"
    )
    preferred = REPORT_COLUMN_ORDER.get(name, ())
    ordered = [key for key in preferred if key in seen]
    ordered.extend(key for key in seen if key not in ordered)
    columns = [{"key": key, "label": humanize_field(key)} for key in ordered]
    return {
        "report_key": int(
            (catalog_item or {}).get("key")
            or meta.get("reportId")
            or meta.get("reportId")
            or 0
        ),
        "report_name": name,
        "title": REPORT_TITLES.get(name, name if " " in str(name) else humanize_field(name)),
        "category": (catalog_item or {}).get("category")
        or ("Custom" if (catalog_item or {}).get("is_custom") else None),
        "row_count": int(
            meta.get("rowCount")
            or meta.get("rowCount")
            or len(rows)
        ),
        "report_time": meta.get("reportTime")
        or meta.get("reportTime"),
        "columns": columns,
        "rows": rows,
    }


def login(client: httpx.Client) -> tuple[str, str | None]:
    if not sensehub_is_configured():
        raise SenseHubConfigError(
            "SenseHub is not configured. Set SENSEHUB_USERNAME, "
            "SENSEHUB_PASSWORD and SENSEHUB_FARM_ID."
        )
    headers = {
        "Authorization": _basic_auth(
            SENSEHUB_USERNAME, SENSEHUB_FARM_ID, SENSEHUB_REGION, SENSEHUB_PASSWORD
        ),
        "farmId": SENSEHUB_FARM_ID,
        "Content-Type": "application/json",
        "scr-client-correlationid": _correlation_id(),
        "Accept": "application/json",
    }
    response = client.post(
        f"{SENSEHUB_PROXY_BASE}/rest/api/v4/auth/login",
        headers=headers,
        json={"username": SENSEHUB_USERNAME, "password": SENSEHUB_PASSWORD},
    )
    if response.status_code in (401, 403):
        raise SenseHubAuthError("SenseHub rejected the username, password, or farm ID.")
    if response.status_code >= 400:
        raise SenseHubError(f"SenseHub login failed ({response.status_code}).")
    body = _unwrap(response.json()) or {}
    token = body.get("accessToken")
    if not token:
        raise SenseHubAuthError("SenseHub login did not return an access token.")
    display_version = body.get("displayVersion") or body.get("displayVersion")
    return str(token), str(display_version) if display_version else None


def catalog_from_v5_body(body: dict[str, Any]) -> list[dict[str, Any]]:
    """System reports plus Custom reports (Menu → Reports → Custom reports)."""
    items: list[dict[str, Any]] = []
    for item in body.get("reports") or []:
        if isinstance(item, dict):
            tagged = dict(item)
            tagged["is_custom"] = False
            items.append(tagged)
    for item in body.get("customReports") or []:
        if isinstance(item, dict):
            tagged = dict(item)
            tagged["is_custom"] = True
            tagged.setdefault("category", "Custom")
            items.append(tagged)
    return items


def list_reports(client: httpx.Client, token: str) -> list[dict[str, Any]]:
    response = client.get(
        f"{SENSEHUB_PROXY_BASE}/rest/api/v5/reports",
        headers=_client_headers(token=token),
    )
    response.raise_for_status()
    body = _unwrap(response.json()) or {}
    if not isinstance(body, dict):
        return []
    return catalog_from_v5_body(body)


def fetch_report(
    client: httpx.Client,
    token: str,
    report_key: int,
    *,
    cloud: bool = False,
    display_version: str | None = None,
    past_report_time: int | None = None,
) -> dict[str, Any]:
    version_path = "v3" if cloud else "v2"
    headers = _client_headers(
        token=token,
        display_version=(display_version or "8.3.2.357") if cloud else None,
    )
    params: dict[str, Any] = {"offset": 0, "limit": 0, "type": "full"}
    if past_report_time is not None:
        params["pastReportTime"] = past_report_time
    response = client.get(
        f"{SENSEHUB_PROXY_BASE}/rest/api/{version_path}/reports/{report_key}",
        headers=headers,
        params=params,
    )
    response.raise_for_status()
    return response.json()


def fetch_farm_about(client: httpx.Client, token: str) -> dict[str, Any]:
    response = client.get(
        f"{SENSEHUB_PROXY_BASE}/rest/api/system/about",
        headers=_client_headers(token=token),
    )
    if response.status_code >= 400:
        return {}
    body = _unwrap(response.json()) or {}
    return body if isinstance(body, dict) else {}


def fetch_named_reports(
    names: list[str],
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Log in and download only the named SenseHub reports."""
    return fetch_all_reports(client=client, names=names)


def fetch_all_reports(
    client: httpx.Client | None = None,
    *,
    names: list[str] | None = None,
) -> dict[str, Any]:
    """Log in and download SenseHub reports. Returns flattened snapshots."""
    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=120.0, follow_redirects=True)
    try:
        token, display_version = login(client)
        about = fetch_farm_about(client, token)
        catalog = list_reports(client, token)
        if names:
            wanted = {compact_report_name(name) for name in names}
            catalog = [
                item
                for item in catalog
                if compact_report_name(item.get("name")) in wanted
            ]
            if not catalog:
                raise SenseHubError(
                    "SenseHub catalogue does not include: " + ", ".join(names)
                )
        reports: list[dict[str, Any]] = []
        errors: list[str] = []
        for item in catalog:
            key = item.get("key")
            if key is None:
                continue
            try:
                raw = fetch_report(
                    client,
                    token,
                    int(key),
                    cloud=bool(item.get("is_custom")),
                    display_version=display_version,
                )
                reports.append(flatten_report(raw, catalog_item=item))
            except httpx.HTTPError as exc:
                errors.append(f"{item.get('name') or key}: {exc}")
        required = names[0] if names else DEFAULT_REPORT
        if not any(
            compact_report_name(item.get("report_name"))
            == compact_report_name(required)
            for item in reports
        ):
            detail = "; ".join(errors[:3]) if errors else "report missing from catalog"
            raise SenseHubError(
                f"Could not fetch {required} ({detail})."
            )
        return {
            "farm_id": about.get("farmId") or SENSEHUB_FARM_ID,
            "farm_name": about.get("farmName"),
            "software_version": about.get("version"),
            "reports": reports,
        }
    finally:
        if own_client:
            client.close()


def resolve_proxy_domain(farm_id: str | None = None) -> str:
    """Public helper used by tests; login already uses the resolved default proxy."""
    farm = farm_id or SENSEHUB_FARM_ID
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        response = client.get(
            SENSEHUB_DOMAIN_RESOLVER_URL,
            headers={"farmId": farm, "Accept": "application/json"},
        )
        response.raise_for_status()
        body = response.json()
        return str(body.get("domain") or SENSEHUB_PROXY_BASE)


SUCKLING_CALVES_GROUP = "Suckling Calves"
_UK = ZoneInfo("Europe/London")


def birth_date_to_epoch(birth_date: dt.date) -> int:
    """Midnight Europe/London on the DairyComp birth date, as SenseHub expects."""
    local = dt.datetime(
        birth_date.year, birth_date.month, birth_date.day, tzinfo=_UK
    )
    return int(local.timestamp())


def _walk_named_groups(payload: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        name = payload.get("name") or payload.get("groupName")
        group_id = payload.get("id")
        number = payload.get("number")
        if name and (group_id is not None or number is not None):
            found.append(payload)
        for value in payload.values():
            found.extend(_walk_named_groups(value))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(_walk_named_groups(item))
    return found


def pick_suckling_calves_group(metadata: Any) -> dict[str, Any]:
    wanted = SUCKLING_CALVES_GROUP.casefold()
    for group in _walk_named_groups(metadata):
        name = str(group.get("name") or group.get("groupName") or "").strip()
        if name.casefold() != wanted:
            continue
        return {
            "id": group.get("id"),
            "number": group.get("number"),
            "name": name,
        }
    raise SenseHubError("SenseHub has no Suckling Calves group.")


def _sensehub_error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        text = (response.text or "").strip()
        return text[:300] if text else f"SenseHub returned {response.status_code}."
    unwrapped = _unwrap(body) if isinstance(body, dict) else body
    if isinstance(unwrapped, dict):
        failures = unwrapped.get("failures") or unwrapped.get("errors")
        if isinstance(failures, list) and failures:
            first = failures[0]
            if isinstance(first, dict):
                return str(
                    first.get("message")
                    or first.get("key")
                    or first.get("fieldName")
                    or first
                )
            return str(first)
        for key in ("message", "error", "detail"):
            value = unwrapped.get(key)
            if value:
                return str(value)
    return f"SenseHub returned {response.status_code}."


def _animal_write_headers(token: str, display_version: str | None) -> dict[str, str]:
    headers = _client_headers(
        token=token, display_version=display_version or "8.3.3.396"
    )
    headers["farmid"] = SENSEHUB_FARM_ID
    headers["scr-farm-id"] = SENSEHUB_FARM_ID
    headers["Content-Type"] = "application/json"
    headers["Origin"] = SENSEHUB_LOGIN_BASE
    headers["Referer"] = f"{SENSEHUB_LOGIN_BASE}/"
    return headers


def calf_create_payload(
    *,
    animal_name: str,
    scr_tag: str,
    birth_date: dt.date,
    group: dict[str, Any],
) -> dict[str, Any]:
    return {
        "orn": None,
        "rfidTag": None,
        "scrTag": {
            "tagNumber": str(scr_tag).strip(),
            "id": None,
            "tagType": "scr",
        },
        "animalName": str(animal_name).strip(),
        "birthDate": birth_date_to_epoch(birth_date),
        "breedingDate": None,
        "breedingSire": None,
        "calvingDate": None,
        "dryOffDate": None,
        "group": group,
        "lactation": 0,
        "pregnancyCheckDate": None,
    }


def create_sensehub_calf(
    *,
    animal_name: str,
    scr_tag: str,
    birth_date: dt.date,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Create a female calf on SenseHub in Suckling Calves with this monitoring tag."""
    cow_id = str(animal_name or "").strip()
    tag = str(scr_tag or "").strip()
    if not cow_id:
        raise SenseHubError("Missing Cow ID.")
    if not tag:
        raise SenseHubError("Missing SCR monitoring tag.")
    if not sensehub_is_configured():
        raise SenseHubConfigError(
            "SenseHub is not configured. Set SENSEHUB_USERNAME, "
            "SENSEHUB_PASSWORD and SENSEHUB_FARM_ID."
        )
    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=60.0, follow_redirects=True)
    try:
        token, display_version = login(client)
        headers = _animal_write_headers(token, display_version)
        meta_response = client.get(
            f"{SENSEHUB_PROXY_BASE}/rest/api/animals",
            headers=headers,
            params={"projection": "cowsMetaData"},
        )
        if meta_response.status_code >= 400:
            raise SenseHubError(
                f"Could not load SenseHub groups ({meta_response.status_code})."
            )
        metadata = _unwrap(meta_response.json()) or meta_response.json()
        group = pick_suckling_calves_group(metadata)
        payload = calf_create_payload(
            animal_name=cow_id,
            scr_tag=tag,
            birth_date=birth_date,
            group=group,
        )
        response = client.post(
            f"{SENSEHUB_PROXY_BASE}/rest/api/animals/cows",
            headers=headers,
            json=payload,
        )
        if response.status_code not in (200, 201):
            raise SenseHubError(_sensehub_error_text(response))
        return {
            "animal_name": cow_id,
            "scr_tag": tag,
            "group": group.get("name") or SUCKLING_CALVES_GROUP,
            "status": response.status_code,
        }
    finally:
        if own_client:
            client.close()


def _parse_sensehub_animals(payload: Any) -> list[dict[str, Any]]:
    body = _unwrap(payload)
    rows = body
    if isinstance(body, dict):
        rows = None
        for key in ("animals", "cows", "items", "animalList"):
            value = body.get(key)
            if isinstance(value, list):
                rows = value
                break
        if rows is None:
            return []
    if not isinstance(rows, list):
        return []
    animals: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        name = str(
            item.get("animalName") or item.get("name") or item.get("AnimalID") or ""
        ).strip()
        animal_id = item.get("animalId")
        if animal_id is None:
            animal_id = item.get("id")
        if not name or animal_id is None:
            continue
        try:
            animals.append({"animal_id": int(animal_id), "animal_name": name})
        except (TypeError, ValueError):
            continue
    return animals


_ANIMAL_LIST_PAGE_SIZE = 200


def _animal_list_total(body: dict[str, Any]) -> int | None:
    meta = body.get("meta") or {}
    if not isinstance(meta, dict):
        return None
    for key in ("rowTotalAfterFilter", "rowTotal"):
        value = meta.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return None


def _monitoring_tag_value(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text or text.casefold() in {"none", "null", "-"}:
        return None
    return text


def _parse_animal_list_rows(payload: Any) -> tuple[list[dict[str, Any]], int | None]:
    """Parse the Animals in Herd grid (SenseHub AnimalList / type=full)."""
    body = _unwrap(payload)
    if not isinstance(body, dict):
        return [], None
    rows = body.get("rows")
    if not isinstance(rows, list):
        return [], _animal_list_total(body)
    animals: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        name = str(
            item.get("AnimalIDCalculation")
            or item.get("AnimalID")
            or item.get("animalName")
            or item.get("name")
            or ""
        ).strip()
        animal_id = item.get("CowDatabaseIDCalculation")
        if animal_id is None:
            animal_id = item.get("CowDatabaseID")
        if animal_id is None:
            animal_id = item.get("animalId")
        if animal_id is None:
            animal_id = item.get("id")
        if not name or animal_id in (None, ""):
            continue
        tag = _monitoring_tag_value(
            item.get("CowRfidOrScrTagNumberCalculation")
            or item.get("CowRfidOrScrTagNumber")
            or item.get("CowRfidOrScrTagNumberCalculation")
            or item.get("CowRfidOrScrTagNumber")
            or item.get("CowScrTagNumberCalculation")
            or item.get("CowScrTagNumber")
        )
        try:
            animals.append(
                {
                    "animal_id": int(animal_id),
                    "animal_name": name,
                    "scr_tag": tag,
                }
            )
        except (TypeError, ValueError):
            continue
    return animals, _animal_list_total(body)


def animal_list_as_report(animals: list[dict[str, Any]]) -> dict[str, Any]:
    """Turn the live Animals in Herd grid into a stored report snapshot."""
    rows: list[dict[str, Any]] = []
    for animal in animals:
        rows.append(
            {
                "AnimalID": animal.get("animal_name"),
                "CowDatabaseID": animal.get("animal_id"),
                "CowRfidOrScrTagNumber": animal.get("scr_tag"),
            }
        )
    return {
        "report_key": ANIMAL_LIST_SNAPSHOT_KEY,
        "report_name": HERD_REPORT,
        "title": HERD_REPORT,
        "category": None,
        "row_count": len(rows),
        "columns": [
            {"key": "AnimalID", "label": "Animal ID"},
            {"key": "CowDatabaseID", "label": "SenseHub ID"},
            {"key": "CowRfidOrScrTagNumber", "label": "Monitoring tag"},
        ],
        "rows": rows,
    }


def parse_no_data_rows(rows: list[Any] | None) -> list[dict[str, Any]]:
    animals: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = str(_raw_report_value(row, "AnimalID") or "").strip()
        if not name:
            continue
        animal_id = _as_int(
            _raw_report_value(row, "CowDatabaseID", "CowDbId", "CowDatabaseID")
        )
        tag = _raw_report_value(row, "CowScrTagNumber", "CowRfidOrScrTagNumber")
        age = _raw_report_value(row, "AgeInDays")
        animals.append(
            {
                "animal_id": animal_id,
                "animal_name": name,
                "age_days": _as_int(age),
                "scr_tag": str(tag).strip() if tag not in (None, "") else None,
                "days_with_assigned_tag": days_with_assigned_tag(row),
            }
        )
    return animals


def _fetch_sensehub_animals(
    client: httpx.Client,
    headers: dict[str, str],
    *,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    response = client.get(
        f"{SENSEHUB_PROXY_BASE}/rest/api/animals",
        headers=headers,
        params=params,
    )
    if response.status_code >= 400:
        return []
    parsed = _parse_sensehub_animals(response.json())
    if parsed:
        return parsed
    rows, _total = _parse_animal_list_rows(response.json())
    return rows


def _fetch_sensehub_animal_list(
    client: httpx.Client,
    headers: dict[str, str],
) -> list[dict[str, Any]]:
    animals: list[dict[str, Any]] = []
    offset = 0
    total: int | None = None
    while True:
        response = client.get(
            f"{SENSEHUB_PROXY_BASE}/rest/api/animals",
            headers=headers,
            params={
                "offset": offset,
                "limit": _ANIMAL_LIST_PAGE_SIZE,
                "type": "full",
                "includeFilterMetaData": "true" if offset == 0 else "false",
                "isRefresh": "true" if offset == 0 else "false",
            },
        )
        if response.status_code >= 400:
            return animals
        page, page_total = _parse_animal_list_rows(response.json())
        if page_total is not None:
            total = page_total
        if not page:
            break
        animals.extend(page)
        offset += _ANIMAL_LIST_PAGE_SIZE
        if total is not None and offset >= total:
            break
        if offset > 20_000:
            break
    return animals


def list_sensehub_animals(
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Animals currently on SenseHub, including names like ``535666 - PT``.

    Uses the Animals in Herd grid (``AnimalList``, ``type=full``). A plain
    ``GET /rest/api/animals`` returns 500 on this farm.
    """
    if not sensehub_is_configured():
        return []
    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=90.0, follow_redirects=True)
    try:
        token, display_version = login(client)
        headers = _animal_write_headers(token, display_version)
        animals = _fetch_sensehub_animal_list(client, headers)
        if animals:
            return animals
        return _fetch_sensehub_animals(
            client, headers, params={"projection": "canAssignTag"}
        )
    finally:
        if own_client:
            client.close()


def list_untagged_sensehub_animals(
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Animals in Herd with no monitoring tag (blank RFID/SCR tag number)."""
    if not sensehub_is_configured():
        return []
    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=90.0, follow_redirects=True)
    try:
        token, display_version = login(client)
        headers = _animal_write_headers(token, display_version)
        herd = _fetch_sensehub_animal_list(client, headers)
        if herd:
            return [item for item in herd if not item.get("scr_tag")]
        response = client.get(
            f"{SENSEHUB_PROXY_BASE}/rest/api/animals",
            headers=headers,
            params={"projection": "canAssignTag"},
        )
        if response.status_code >= 400:
            raise SenseHubError(
                f"Could not load untagged SenseHub animals ({response.status_code})."
            )
        return _parse_sensehub_animals(response.json())
    finally:
        if own_client:
            client.close()


def _unassigned_scr_tag(
    client: httpx.Client, headers: dict[str, str], tag_number: str
) -> dict[str, Any]:
    response = client.get(f"{SENSEHUB_PROXY_BASE}/rest/api/tags", headers=headers)
    if response.status_code >= 400:
        return {"tagNumber": tag_number, "id": None, "tagType": "scr"}
    body = _unwrap(response.json()) or response.json()
    scr_tags = body.get("scrTags") if isinstance(body, dict) else None
    unassigned = []
    if isinstance(scr_tags, dict):
        unassigned = scr_tags.get("unassignedTags") or []
    for tag in unassigned:
        if not isinstance(tag, dict):
            continue
        if str(tag.get("tagNumber") or "").strip() == tag_number:
            return tag
    return {"tagNumber": tag_number, "id": None, "tagType": "scr"}


def assign_sensehub_monitoring_tag(
    *,
    animal_id: int,
    scr_tag: str,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Assign a monitoring tag to an existing SenseHub animal."""
    tag_number = str(scr_tag or "").strip()
    if not tag_number:
        raise SenseHubError("Missing SCR monitoring tag.")
    if not sensehub_is_configured():
        raise SenseHubConfigError(
            "SenseHub is not configured. Set SENSEHUB_USERNAME, "
            "SENSEHUB_PASSWORD and SENSEHUB_FARM_ID."
        )
    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=60.0, follow_redirects=True)
    try:
        token, display_version = login(client)
        headers = _animal_write_headers(token, display_version)
        tag = _unassigned_scr_tag(client, headers, tag_number)
        payload = {
            "type": "AssignScrTag",
            "startDateTime": int(dt.datetime.now(dt.timezone.utc).timestamp()),
            "tag": tag,
            "animalIds": [int(animal_id)],
        }
        response = client.post(
            f"{SENSEHUB_PROXY_BASE}/rest/api/v2/events/createevent",
            headers=headers,
            json=payload,
        )
        if response.status_code not in (200, 201):
            raise SenseHubError(_sensehub_error_text(response))
        return {
            "animal_id": int(animal_id),
            "scr_tag": tag_number,
            "status": response.status_code,
        }
    finally:
        if own_client:
            client.close()


def _cull_event_failed(payload: Any) -> str | None:
    body = _unwrap(payload) if isinstance(payload, dict) else payload
    if not isinstance(body, dict):
        return None
    if body.get("succeeded") is False:
        return str(body.get("message") or body.get("error") or "SenseHub cull was not applied.")
    failures = body.get("failures") or body.get("errors")
    if isinstance(failures, list) and failures:
        first = failures[0]
        if isinstance(first, dict):
            return str(first.get("message") or first.get("key") or first)
        return str(first)
    return None


def _raw_report_value(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
        calc = name if str(name).endswith("Calculation") else f"{name}Calculation"
        if calc in row and row[calc] not in (None, ""):
            return row[calc]
    return None


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return int(float(text.split()[0]))
    except (TypeError, ValueError):
        return None


def _normalized_field_key(name: str) -> str:
    text = str(name or "").casefold()
    if text.endswith("calculation"):
        text = text[: -len("calculation")]
    return re.sub(r"[^a-z0-9]+", "", text)


def days_with_assigned_tag(row: dict[str, Any]) -> int | None:
    """Days since the monitoring tag was assigned, from a No Data report row."""
    direct = _raw_report_value(
        row,
        "DaysWithAssignedTag",
        "DaysWithAssignedTag",
        "AssignedTagDays",
        "TagAssignedDays",
        "DaysSinceTagAssigned",
        "DaysWithTagAssigned",
        "CowDaysWithAssignedTag",
    )
    parsed = _as_int(direct)
    if parsed is not None:
        return parsed
    for key, value in row.items():
        norm = _normalized_field_key(str(key))
        if "day" in norm and "assign" in norm and "tag" in norm:
            parsed = _as_int(value)
            if parsed is not None:
                return parsed
    return None


def list_no_data_sensehub_animals(
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Animals from the SenseHub custom No Data report."""
    if not sensehub_is_configured():
        return []
    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=90.0, follow_redirects=True)
    try:
        token, display_version = login(client)
        catalog = list_reports(client, token)
        item = next(
            (
                entry
                for entry in catalog
                if str(entry.get("name") or "").strip().casefold() == "no data"
            ),
            None,
        )
        if item is None or item.get("key") is None:
            return []
        raw = fetch_report(
            client,
            token,
            int(item["key"]),
            cloud=bool(item.get("is_custom")),
            display_version=display_version,
        )
        body = _unwrap(raw) or raw
        rows = body.get("rows") if isinstance(body, dict) else []
        return parse_no_data_rows(rows)
    finally:
        if own_client:
            client.close()


def cull_sensehub_animals(
    animal_ids: list[int],
    *,
    occurred_on: dt.date | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Create one SenseHub culling event covering these animal IDs."""
    ids = [int(item) for item in animal_ids if item is not None]
    if not ids:
        return {"culled": 0, "animal_ids": [], "failed": []}
    if not sensehub_is_configured():
        raise SenseHubConfigError(
            "SenseHub is not configured. Set SENSEHUB_USERNAME, "
            "SENSEHUB_PASSWORD and SENSEHUB_FARM_ID."
        )
    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=60.0, follow_redirects=True)
    try:
        token, display_version = login(client)
        headers = _animal_write_headers(token, display_version)
        start = (
            birth_date_to_epoch(occurred_on)
            if occurred_on is not None
            else int(dt.datetime.now(dt.timezone.utc).timestamp())
        )
        payload = {
            "clientType": "Web",
            "startDateTime": start,
            "type": "culling",
            "animalIds": ids,
        }
        response = client.post(
            f"{SENSEHUB_PROXY_BASE}/rest/api/v2/events/createevent",
            headers=headers,
            json=payload,
        )
        if response.status_code not in (200, 201):
            raise SenseHubError(_sensehub_error_text(response))
        try:
            body = response.json()
        except ValueError:
            body = {}
        reason = _cull_event_failed(body)
        if reason:
            raise SenseHubError(reason)
        return {"culled": len(ids), "animal_ids": ids, "failed": []}
    finally:
        if own_client:
            client.close()
