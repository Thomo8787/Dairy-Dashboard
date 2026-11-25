"""BCMS CTS client via DEFRA DDTS SOAP.

Protocol adapted from Chris Webb's cts-tool (MIT):
https://github.com/arachsys/cts-tool
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from services.cts_config import (
    CTS_DDTS_PASSWORD,
    CTS_DDTS_USERNAME,
    cts_ddts_is_configured,
    cts_farm_credential_parts,
    cts_farm_credentials,
    cts_farm_option_values,
    cts_normalize_farm_key,
    cts_ready_farms,
)

logger = logging.getLogger(__name__)

_DDTS_URL = (
    "https://webservice.secure.ddts.defra.gov.uk/"
    "DefraDataTransferPublicNWSE.asmx"
)
# Match Dairy Comp 305’s CTWS client identity (required by some DDTS/CTWS setups).
_PROGRAM_NAME = "Dairy Comp 305"
_PROGRAM_VERSION = "20130510"

_NS_HOLDING_REQ = "http://defra.bcms.ctws/holding_request"
_NS_HOLDING_RES = "http://defra.bcms.ctws/holding_request_results"
_NS_SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
_NS_DEFRA = "http://www.defra.gov.uk"


class CtsError(Exception):
    """CTS / DDTS request failed."""


@dataclass(frozen=True)
class CtsAnimal:
    etag: str
    breed: str
    sex: str
    dob: dt.date | None
    on_date: dt.date | None


def _pad_de_etag(etag: str) -> str:
    """BCMS expects German tags as DE plus 10 digits.

    DairyComp and the sales matcher drop the leading zero (DE362628698). CTS
    rejects that form and accepts the 10-digit form (DE0362628698).
    """
    match = re.fullmatch(r"DE(\d+)", etag)
    if match is None:
        return etag
    digits = match.group(1)
    if len(digits) < 10:
        digits = digits.zfill(10)
    return f"DE{digits}"


def normalize_cts_etag(value: str | None) -> str:
    """Normalize a CTS / inventory ear tag for set comparison and CTWS send.

    Reuses the cattle-sales normalizer so DairyComp zero-padding and spaces match,
    then pads a short German numeric part back out to 10 digits.
    """
    from services.cattle_sale_pdf import normalize_etag

    raw = normalize_etag(value)
    if not raw or raw == "?":
        return ""
    return _pad_de_etag(raw)


def _parse_holding(holding: str) -> tuple[str, str | None]:
    holding = (holding or "").strip()
    match = re.fullmatch(r"(\d+/\d+/\d+)-(\d{2})", holding)
    if match:
        return match.group(1), match.group(2)
    if re.fullmatch(r"\d+/\d+/\d+", holding):
        return holding, None
    raise CtsError(
        f"Invalid holding {holding!r}; expected CC/PPP/HHHH or CC/PPP/HHHH-NN"
    )


def _parse_date(value: str | None) -> dt.date | None:
    if not value or value == "?":
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y%m%d"):
        try:
            return dt.datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def _serialise_soap(element: ET.Element) -> bytes:
    # Match cts-tool: leading space before SOAP envelope bytes.
    return b" " + ET.tostring(element, encoding="utf-8")


def _serialise_ctws(element: ET.Element) -> bytes:
    """Serialise inner CTWS XML.

    Live CTWS rejects payloads without an XML declaration, and also rejects a
    leading space before ``<?xml`` (Dairy Comp sends a declaration with no
    leading whitespace).
    """
    body = ET.tostring(element, encoding="utf-8")
    if body.lstrip().startswith(b"<?xml"):
        return body.lstrip()
    return b'<?xml version="1.0" encoding="UTF-8"?>' + body


def _ctws_exception_info(root: ET.Element) -> tuple[str, str] | None:
    for el in root.iter():
        if el.tag.endswith("SystemException"):
            code = (el.attrib.get("ExNum") or "").strip()
            msg = (el.attrib.get("ExMsg") or "").strip() or "".join(
                el.itertext()
            ).strip()
            return code, msg
    return None


def raise_if_ctws_exception(
    root: ET.Element, *, ignore_codes: frozenset[str] | None = None
) -> None:
    info = _ctws_exception_info(root)
    if info is None:
        return
    code, msg = info
    if ignore_codes and code in ignore_codes:
        return
    detail = f"{code}: {msg}".strip(": ").strip()
    raise CtsError(f"CTWS error {detail}")


# Back-compat alias used inside this module.
_raise_if_ctws_exception = raise_if_ctws_exception


def is_ctws_results_pending(root: ET.Element) -> bool:
    """True when async validation results are not ready yet (CTWS806)."""
    info = _ctws_exception_info(root)
    return info is not None and info[0] == "CTWS806"


def _decode_ddts_result(
    raw: str, *, ignore_exception_codes: frozenset[str] | None = None
) -> ET.Element:
    """Decode TransferDataHexResult; surface plaintext DDTS errors clearly."""
    text = (raw or "").strip()
    if not text:
        raise CtsError("DDTS returned an empty TransferDataHexResult")

    cleaned = "".join(text.split())
    # Plaintext DDTS errors are short and not valid base64 (len % 4 == 1, etc.).
    looks_like_b64 = bool(re.fullmatch(r"[A-Za-z0-9+/]+=*", cleaned)) and len(cleaned) >= 16
    if not looks_like_b64:
        raise CtsError(f"DDTS rejected the request: {text}")

    pad = (-len(cleaned)) % 4
    if pad == 3:
        raise CtsError(f"DDTS rejected the request: {text}")
    try:
        decoded = base64.b64decode(cleaned + ("=" * pad), validate=False).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        raise CtsError(f"DDTS rejected the request: {text}") from exc

    try:
        root = ET.XML(decoded)
    except ET.ParseError as exc:
        raise CtsError(
            f"DDTS returned non-XML payload after decode: {decoded[:300]}"
        ) from exc
    _raise_if_ctws_exception(root, ignore_codes=ignore_exception_codes)
    return root


def _transfer(
    kind: str,
    request: ET.Element,
    *,
    timeout: float = 120.0,
    ignore_exception_codes: frozenset[str] | None = None,
) -> ET.Element:
    if not cts_ddts_is_configured():
        raise CtsError(
            "CTS DDTS is not configured. Set CTS_DDTS_USERNAME and CTS_DDTS_PASSWORD."
        )

    inner_b64 = base64.b64encode(_serialise_ctws(request)).decode("ascii")
    envelope = ET.Element("Envelope", xmlns=_NS_SOAP)
    body = ET.SubElement(envelope, "Body")
    transfer = ET.SubElement(body, "TransferDataHex", xmlns=_NS_DEFRA)
    ET.SubElement(transfer, "username").text = CTS_DDTS_USERNAME
    ET.SubElement(transfer, "password").text = hashlib.md5(
        CTS_DDTS_PASSWORD.encode("utf-8")
    ).hexdigest()
    ET.SubElement(transfer, "serviceName").text = "DEFRA-CTWS"
    ET.SubElement(transfer, "type").text = kind
    ET.SubElement(transfer, "data").text = inner_b64

    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": "http://www.defra.gov.uk/TransferDataHex",
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                _DDTS_URL, content=_serialise_soap(envelope), headers=headers
            )
    except httpx.HTTPError as exc:
        raise CtsError(f"DDTS HTTP request failed: {exc}") from exc

    if response.status_code >= 400:
        raise CtsError(f"DDTS HTTP error {response.status_code}: {response.text[:300]}")

    try:
        soap = ET.XML(response.content)
    except ET.ParseError as exc:
        raise CtsError(f"Invalid DDTS SOAP XML: {exc}") from exc

    fault = soap.find(f".//{{{_NS_SOAP}}}Fault")
    if fault is not None:
        fault_text = "".join(fault.itertext()).strip()
        raise CtsError(f"DDTS SOAP fault: {fault_text[:400]}")

    result_node = soap.find(f".//{{{_NS_DEFRA}}}TransferDataHexResult")
    if result_node is None:
        # Namespace-agnostic fallback
        for el in soap.iter():
            if el.tag.endswith("TransferDataHexResult"):
                result_node = el
                break
    if result_node is None or not (result_node.text or "").strip():
        raise CtsError(
            "DDTS response missing TransferDataHexResult: "
            f"{response.text[:400]}"
        )
    return _decode_ddts_result(
        result_node.text, ignore_exception_codes=ignore_exception_codes
    )


def transfer_ctws(
    kind: str,
    request: ET.Element,
    *,
    timeout: float = 120.0,
    allow_pending: bool = False,
) -> ET.Element:
    """Public DDTS TransferDataHex wrapper for CTWS request/response XML."""
    ignore = frozenset({"CTWS806"}) if allow_pending else None
    return _transfer(
        kind,
        request,
        timeout=timeout,
        ignore_exception_codes=ignore,
    )


def _build_get_holding_request(
    *,
    ctws_username: str,
    ctws_password: str,
    holding: str,
    site: str | None,
) -> ET.Element:
    # Dairy Comp uses naive UTC timestamps without offset/microseconds.
    request = ET.Element(
        "GetHolding",
        xmlns=_NS_HOLDING_REQ,
        SchemaVersion="1.0",
        ProgramName=_PROGRAM_NAME,
        ProgramVersion=_PROGRAM_VERSION,
        RequestTimeStamp=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
    )
    auth = ET.SubElement(request, "Authentication")
    ET.SubElement(auth, "CTS_OL_User", Usr=ctws_username, Pwd=ctws_password)
    holding_el = ET.SubElement(request, "Holding", Loc=holding)
    if site:
        holding_el.attrib["SLoc"] = site
    return request


def parse_holding_animals_xml(root: ET.Element) -> list[CtsAnimal]:
    """Parse GetHolding results XML into animal rows."""
    _raise_if_ctws_exception(root)
    ns = {"results": _NS_HOLDING_RES}
    animals: list[CtsAnimal] = []
    for animal in root.findall(".//results:Animal", ns):
        etag = normalize_cts_etag(animal.attrib.get("Etg"))
        if not etag:
            continue
        animals.append(
            CtsAnimal(
                etag=etag,
                breed=(animal.attrib.get("Brd") or "").strip().upper() or "",
                sex=(animal.attrib.get("Sex") or "").strip().upper()[:1] or "",
                dob=_parse_date(animal.attrib.get("Dob")),
                on_date=_parse_date(animal.attrib.get("OnDate")),
            )
        )
    animals.sort(key=lambda a: (a.on_date or dt.date.min, a.dob or dt.date.min, a.etag))
    return animals


def list_cattle_on_holding(farm: str) -> list[CtsAnimal]:
    """Fetch cattle currently on the farm's CTS holding."""
    farm_key = cts_normalize_farm_key(farm)
    creds = cts_farm_credentials(farm_key)
    if creds is None:
        raise CtsError(
            f"CTS is not configured for farm {farm_key}. "
            f"Set CTS_CTWS_USERNAME_{farm_key}, CTS_CTWS_PASSWORD_{farm_key}, "
            f"and CTS_HOLDING_{farm_key}."
        )
    holding, site = _parse_holding(creds["holding"])
    request = _build_get_holding_request(
        ctws_username=creds["username"],
        ctws_password=creds["password"],
        holding=holding,
        site=site,
    )
    logger.info("CTS GetHolding farm=%s holding=%s", farm_key, holding)
    results = _transfer("Get_Cattle_On_Holding-V1-0", request)
    animals = parse_holding_animals_xml(results)
    logger.info("CTS GetHolding farm=%s animals=%s", farm_key, len(animals))
    return animals


def cts_configured_farms() -> list[str]:
    """CTS UI farms with complete CTWS + holding config (DDTS may still be missing)."""
    return cts_ready_farms()


def cts_status() -> dict[str, Any]:
    option_values = cts_farm_option_values()
    farm_parts = {farm: cts_farm_credential_parts(farm) for farm in option_values}
    return {
        "ddts_configured": cts_ddts_is_configured(),
        "farms": {
            farm: bool(parts) and all(parts.values())
            for farm, parts in farm_parts.items()
        },
        "farm_parts": farm_parts,
        "ready_farms": (
            cts_configured_farms() if cts_ddts_is_configured() else []
        ),
    }
