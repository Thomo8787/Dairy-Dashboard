"""Build CTWS RegBirths / RegMovs XML payloads for BCMS (preview or live send)."""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal
from xml.etree import ElementTree as ET

from services.cts_config import cts_farm_credentials
from services.cts_client import normalize_cts_etag

_PROGRAM_NAME = "Dairy Comp 305"
_PROGRAM_VERSION = "20130510"
_NS_REG_BIRTHS = "http://defra.bcms.ctws/register_births_request"
_NS_REG_MOVS = "http://defra.bcms.ctws/register_movements_request"

PreviewKind = Literal["births", "movements"]

_MOVEMENT_MTYPES = {
    "sale": "off",
    "death": "death",
    "move_on": "on",
}

_PASSWORD_REDACTED = "***"


class CtsSubmitXmlError(ValueError):
    """Invalid selection or farm credentials for CTWS XML build."""


def _timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _serialise_ctws(element: ET.Element) -> str:
    body = ET.tostring(element, encoding="utf-8")
    if body.lstrip().startswith(b"<?xml"):
        return body.lstrip().decode("utf-8")
    return '<?xml version="1.0" encoding="UTF-8"?>' + body.decode("utf-8")


def _farm_auth(farm: str, *, redact_password: bool) -> tuple[str, str, str]:
    farm_key = farm.strip().upper()
    creds = cts_farm_credentials(farm_key)
    if creds is None:
        raise CtsSubmitXmlError(
            f"CTS is not configured for farm {farm_key}. "
            f"Set CTS_CTWS_USERNAME_{farm_key}, CTS_CTWS_PASSWORD_{farm_key}, "
            f"and CTS_HOLDING_{farm_key}."
        )
    username = creds["username"]
    password = _PASSWORD_REDACTED if redact_password else creds["password"]
    holding = (creds.get("holding") or "").strip()
    if not holding:
        raise CtsSubmitXmlError(f"CTS holding is not configured for farm {farm_key}.")
    # Strip optional site suffix (CC/PPP/HHHH-NN) for Loc attribute.
    holding_loc = holding.split("-", 1)[0]
    return username, password, holding_loc


def _add_auth(parent: ET.Element, username: str, password: str) -> None:
    auth = ET.SubElement(parent, "Authentication")
    ET.SubElement(auth, "CTS_OL_User", Usr=username, Pwd=password)


def build_reg_births_element(
    rows: list[dict[str, Any]],
    *,
    farm: str,
    redact_password: bool = True,
    request_timestamp: str | None = None,
) -> ET.Element:
    """Build RegBirths element (unprefixed tags + default xmlns attr).

    Pass this Element straight to DDTS transfer — do not parse a serialised
    string first, or ElementTree rewrites the default xmlns as ``ns0:`` prefixes
    and CTWS802 reports missing Authentication.
    """
    birth_rows = filter_rows_for_kind(rows, "births")
    if not birth_rows:
        raise CtsSubmitXmlError("No birth rows selected for RegBirths preview.")

    username, password, holding = _farm_auth(farm, redact_password=redact_password)
    stamp = request_timestamp or _timestamp()
    root = ET.Element(
        "RegBirths",
        xmlns=_NS_REG_BIRTHS,
        SchemaVersion="1.0",
        ProgramName=_PROGRAM_NAME,
        ProgramVersion=_PROGRAM_VERSION,
        RequestTimeStamp=stamp,
    )
    _add_auth(root, username, password)
    births_el = ET.SubElement(root, "Births", TxnId=stamp)

    for index, row in enumerate(birth_rows, start=1):
        etag = normalize_cts_etag(row.get("etag"))
        dob = _iso_date(row.get("dob") or row.get("event_date"))
        # RegBirths Sex_Type is lowercase m/f (uppercase fails CTWS802).
        sex = (row.get("sex") or "").strip().lower()[:1]
        breed = (row.get("breed") or "").strip().upper()
        if not etag or not dob or sex not in {"m", "f"} or not breed:
            raise CtsSubmitXmlError(
                f"Birth row missing required fields (etag/dob/sex/breed): {row.get('id')}"
            )
        loc = (row.get("holding") or holding).strip() or holding
        dam = normalize_cts_etag(row.get("dreg"))
        if not dam:
            raise CtsSubmitXmlError(
                f"Birth row missing dam ear tag (required as GdEtg): {row.get('id')}"
            )
        # Match Dairy Comp RegBirths core attrs; SiEtg (sire ear tag) is optional.
        attrs: dict[str, str] = {
            "RowNum": str(index),
            "Etg": etag,
            "Dob": dob,
            "Brd": breed,
            "Sex": sex,
            "GdEtg": dam,
            "BLoc": loc,
            "PLoc": loc,
            "IWarn": "n",
        }
        sire = normalize_cts_etag(row.get("sreg"))
        if sire:
            attrs["SiEtg"] = sire
        ET.SubElement(births_el, "Birth", **attrs)

    return root


def build_reg_movs_element(
    rows: list[dict[str, Any]],
    *,
    farm: str,
    redact_password: bool = True,
    request_timestamp: str | None = None,
) -> ET.Element:
    """Build RegMovs element (unprefixed tags + default xmlns attr)."""
    mov_rows = filter_rows_for_kind(rows, "movements")
    if not mov_rows:
        raise CtsSubmitXmlError(
            "No sale/death/move-on rows selected for RegMovs preview."
        )

    username, password, holding = _farm_auth(farm, redact_password=redact_password)
    stamp = request_timestamp or _timestamp()
    root = ET.Element(
        "RegMovs",
        xmlns=_NS_REG_MOVS,
        SchemaVersion="1.0",
        ProgramName=_PROGRAM_NAME,
        ProgramVersion=_PROGRAM_VERSION,
        RequestTimeStamp=stamp,
    )
    _add_auth(root, username, password)
    moves_el = ET.SubElement(root, "Moves", TxnId=stamp)

    for index, row in enumerate(mov_rows, start=1):
        movement_type = str(row.get("movement_type") or "").strip().lower()
        mtype = _MOVEMENT_MTYPES.get(movement_type)
        etag = normalize_cts_etag(row.get("etag"))
        mdate = _iso_date(row.get("event_date"))
        if not mtype or not etag or not mdate:
            raise CtsSubmitXmlError(
                f"Movement row missing required fields: {row.get('id')}"
            )
        ET.SubElement(
            moves_el,
            "Mov",
            RowNum=str(index),
            Etg=etag,
            Loc=(row.get("holding") or holding).strip() or holding,
            MType=mtype,
            MDate=mdate,
            IWarn="n",
        )

    return root


def _iso_date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    return text[:10]


def filter_rows_for_kind(
    rows: list[dict[str, Any]], kind: PreviewKind
) -> list[dict[str, Any]]:
    if kind == "births":
        return [row for row in rows if row.get("movement_type") == "birth"]
    return [
        row
        for row in rows
        if row.get("movement_type") in _MOVEMENT_MTYPES
    ]


def build_reg_births_xml(
    rows: list[dict[str, Any]],
    *,
    farm: str,
    redact_password: bool = True,
    request_timestamp: str | None = None,
) -> str:
    """Build RegBirths CTWS XML from pending birth rows."""
    return _serialise_ctws(
        build_reg_births_element(
            rows,
            farm=farm,
            redact_password=redact_password,
            request_timestamp=request_timestamp,
        )
    )


def build_reg_movs_xml(
    rows: list[dict[str, Any]],
    *,
    farm: str,
    redact_password: bool = True,
    request_timestamp: str | None = None,
) -> str:
    """Build RegMovs CTWS XML from sale / death / move_on rows."""
    return _serialise_ctws(
        build_reg_movs_element(
            rows,
            farm=farm,
            redact_password=redact_password,
            request_timestamp=request_timestamp,
        )
    )


def build_preview_xml(
    rows: list[dict[str, Any]],
    *,
    farm: str,
    kind: PreviewKind,
    redact_password: bool = True,
    request_timestamp: str | None = None,
) -> tuple[str, str]:
    """Return (filename, xml_text) for the requested preview kind."""
    farm_key = farm.strip().upper()
    stamp = (request_timestamp or _timestamp()).replace(":", "")
    if kind == "births":
        xml = build_reg_births_xml(
            rows,
            farm=farm_key,
            redact_password=redact_password,
            request_timestamp=request_timestamp,
        )
        return f"RegBirths-{farm_key}-{stamp}.xml", xml
    if kind == "movements":
        xml = build_reg_movs_xml(
            rows,
            farm=farm_key,
            redact_password=redact_password,
            request_timestamp=request_timestamp,
        )
        return f"RegMovs-{farm_key}-{stamp}.xml", xml
    raise CtsSubmitXmlError("kind must be 'births' or 'movements'.")
