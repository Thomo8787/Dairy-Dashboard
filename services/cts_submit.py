"""Live BCMS submission for pending births / movements via DDTS async CTWS."""

from __future__ import annotations

import logging
import time
from typing import Any
from xml.etree import ElementTree as ET

from sqlalchemy.orm import Session

from services.cts_config import (
    cts_ddts_is_configured,
    cts_farm_credentials,
    cts_farm_option_values,
    cts_is_ui_farm,
    cts_normalize_farm_key,
)
from services.cts_client import (
    CtsError,
    cts_configured_farms,
    is_ctws_results_pending,
    normalize_cts_etag,
    raise_if_ctws_exception,
    transfer_ctws,
)
from services.cts_movements import (
    deadline_day_rows,
    list_pending_movements,
    mark_movements_reported,
)
from services.cts_submit_xml import (
    CtsSubmitXmlError,
    build_reg_births_element,
    build_reg_movs_element,
    filter_rows_for_kind,
)

logger = logging.getLogger(__name__)

# Match Dairy Comp 305 / cts-tool DDTS type names.
_KIND_BIRTHS = "births"
_KIND_MOVEMENTS = "movements"

_SUBMIT_TYPE = {
    _KIND_BIRTHS: "Register_Births_Asynchronous-V1-0",
    _KIND_MOVEMENTS: "Register_Movements_Asynchronous-V1-0",
}
_RESULTS_TYPE = {
    _KIND_BIRTHS: "Get_Register_Births_Validation_Results-V1-0",
    _KIND_MOVEMENTS: "Get_Register_Movements_Validation_Results-V1-0",
}
_RESULTS_NS = {
    _KIND_BIRTHS: "http://defra.bcms.ctws/register_births_request_results",
    _KIND_MOVEMENTS: "http://defra.bcms.ctws/register_movements_request_results",
}

_NS_RECEIPT = "http://defra.bcms.ctws/asynchronous_receipt"
# Dairy Comp uses the misspelled "asynchronus" namespace; also try the correct spelling.
_NS_GET_RESULTS = "http://defra.bcms.ctws/get_asynchronus_results"
_NS_GET_RESULTS_ALT = "http://defra.bcms.ctws/get_asynchronous_results"
_PROGRAM_NAME = "Dairy Comp 305"
_PROGRAM_VERSION = "20130510"

# BCMS often stays on CTWS806 for several minutes after issuing a receipt.
_DEFAULT_POLL_ATTEMPTS = 90
_DEFAULT_POLL_SLEEP_S = 4.0


class CtsSubmitError(CtsError):
    """Live RegBirths / RegMovs submission failed."""


def _timestamp() -> str:
    import datetime as dt

    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _parse_receipt(root: ET.Element) -> str:
    for el in root.iter():
        if el.tag.endswith("Receipt"):
            num = (el.attrib.get("Num") or "").strip()
            if num:
                return num
    # Namespace-aware fallback
    receipt = root.find(f".//{{{_NS_RECEIPT}}}Receipt")
    if receipt is not None:
        num = (receipt.attrib.get("Num") or "").strip()
        if num:
            return num
    raise CtsSubmitError(
        "BCMS did not return a receipt number for the submission."
    )


def _build_get_results(
    farm: str, receipt: str, *, xmlns: str = _NS_GET_RESULTS
) -> ET.Element:
    creds = cts_farm_credentials(farm)
    if creds is None:
        raise CtsSubmitError(f"CTS is not configured for farm {farm}.")
    request = ET.Element(
        "GetResults",
        xmlns=xmlns,
        SchemaVersion="1.0",
        ProgramName=_PROGRAM_NAME,
        ProgramVersion=_PROGRAM_VERSION,
        RequestTimeStamp=_timestamp(),
    )
    auth = ET.SubElement(request, "Authentication")
    ET.SubElement(
        auth, "CTS_OL_User", Usr=creds["username"], Pwd=creds["password"]
    )
    ET.SubElement(request, "Receipt", Num=str(receipt))
    return request


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _xml_snippet(root: ET.Element, *, limit: int = 800) -> str:
    import re

    text = ET.tostring(root, encoding="unicode")
    text = re.sub(r'Pwd="[^"]*"', 'Pwd="***"', text)
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _find_result_items(root: ET.Element, names: set[str]) -> list[ET.Element]:
    return [el for el in root.iter() if _local(el.tag) in names]


def _etag_from_element(el: ET.Element) -> str:
    for key in ("Etg", "Eartag", "EarTag", "etag"):
        value = normalize_cts_etag(el.attrib.get(key))
        if value:
            return value
    return ""


def _parse_validation_results(
    root: ET.Element, *, kind: str, rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Map Accept/Reject rows back to pending movement rows by ear tag / RowNum."""
    results_ns = _RESULTS_NS[kind]
    accepted_els = root.findall(f".//{{{results_ns}}}Accept")
    rejected_els = root.findall(f".//{{{results_ns}}}Reject")
    # Namespace-agnostic + alternate local names used by some CTWS payloads.
    if not accepted_els and not rejected_els:
        accepted_els = _find_result_items(root, {"Accept", "Accepted"})
        rejected_els = _find_result_items(root, {"Reject", "Rejected"})

    by_rownum = {str(i): row for i, row in enumerate(rows, start=1)}
    by_etag: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        etag = normalize_cts_etag(row.get("etag"))
        if etag:
            by_etag.setdefault(etag, []).append(row)

    def _match_row(container: ET.Element) -> dict[str, Any] | None:
        # Prefer explicit Birth/Mov children, then any descendant/self with Etg/RowNum.
        candidates = [container, *list(container.iter())]
        for el in candidates:
            local = _local(el.tag)
            rownum = (el.attrib.get("RowNum") or "").strip()
            etag = _etag_from_element(el)
            if local in {"Birth", "Mov", "Animal"} or rownum or etag:
                if rownum and rownum in by_rownum:
                    return by_rownum[rownum]
                if etag and etag in by_etag:
                    return by_etag[etag][0]
        return None

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    used_ids: set[str] = set()

    for el in accepted_els:
        row = _match_row(el)
        if row is None:
            continue
        rid = str(row.get("id") or "")
        if rid and rid in used_ids:
            continue
        if rid:
            used_ids.add(rid)
        accepted.append(row)

    for el in rejected_els:
        row = _match_row(el)
        causes: list[str] = []
        for cause in el.iter():
            if _local(cause.tag) == "Cause":
                desc = (cause.attrib.get("Desc") or "").strip()
                if desc:
                    causes.append(desc)
        etag = ""
        if row is not None:
            etag = normalize_cts_etag(row.get("etag"))
        if not etag:
            for node in el.iter():
                etag = _etag_from_element(node)
                if etag:
                    break
        detail = {
            "row": row,
            "etag": etag,
            "reasons": causes or ["Rejected by BCMS"],
        }
        rejected.append(detail)
        if row is not None:
            rid = str(row.get("id") or "")
            if rid:
                used_ids.add(rid)

    # If Accept nodes exist but Etg/RowNum mapping failed, and counts align,
    # treat the whole submitted batch as accepted (common for birth results).
    if (
        not accepted
        and not rejected
        and accepted_els
        and not rejected_els
        and len(accepted_els) == len(rows)
    ):
        accepted = list(rows)

    return {
        "accepted": accepted,
        "rejected": rejected,
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "accept_nodes": len(accepted_els),
        "reject_nodes": len(rejected_els),
    }


def _poll_validation(
    *,
    farm: str,
    kind: str,
    receipt: str,
    rows: list[dict[str, Any]],
    poll_attempts: int = _DEFAULT_POLL_ATTEMPTS,
    poll_sleep_s: float = _DEFAULT_POLL_SLEEP_S,
) -> dict[str, Any]:
    results_type = _RESULTS_TYPE[kind]
    last_root: ET.Element | None = None
    namespaces = (_NS_GET_RESULTS, _NS_GET_RESULTS_ALT)
    for attempt in range(1, poll_attempts + 1):
        xmlns = namespaces[(attempt - 1) % len(namespaces)]
        request = _build_get_results(farm, receipt, xmlns=xmlns)
        last_root = transfer_ctws(results_type, request, allow_pending=True)
        if is_ctws_results_pending(last_root):
            logger.info(
                "CTS validation pending farm=%s kind=%s receipt=%s attempt=%s",
                farm,
                kind,
                receipt,
                attempt,
            )
            time.sleep(poll_sleep_s)
            continue
        raise_if_ctws_exception(last_root)
        parsed = _parse_validation_results(last_root, kind=kind, rows=rows)
        if parsed["accepted_count"] == 0 and parsed["rejected_count"] == 0:
            if parsed.get("accept_nodes") or parsed.get("reject_nodes"):
                raise CtsSubmitError(
                    f"BCMS returned Accept/Reject nodes that could not be matched "
                    f"(receipt {receipt}). Last response: {_xml_snippet(last_root)}"
                )
            # Empty shell (no CTWS806) usually means results not ready yet.
            logger.info(
                "CTS validation empty farm=%s kind=%s receipt=%s attempt=%s "
                "xmlns=%s root=%s xml=%s",
                farm,
                kind,
                receipt,
                attempt,
                xmlns,
                _local(last_root.tag),
                _xml_snippet(last_root, limit=400),
            )
            time.sleep(poll_sleep_s)
            continue
        parsed["receipt"] = receipt
        parsed["kind"] = kind
        return parsed

    snippet = _xml_snippet(last_root) if last_root is not None else "(no response)"
    logger.warning(
        "CTS validation still queued farm=%s kind=%s receipt=%s after %s polls. %s",
        farm,
        kind,
        receipt,
        poll_attempts,
        snippet,
    )
    return {
        "accepted": list(rows),
        "rejected": [],
        "accepted_count": len(rows),
        "rejected_count": 0,
        "accept_nodes": 0,
        "reject_nodes": 0,
        "receipt": receipt,
        "kind": kind,
        "queued": True,
    }


def _submit_kind(
    *,
    farm: str,
    kind: str,
    rows: list[dict[str, Any]],
    poll_attempts: int,
    poll_sleep_s: float,
) -> dict[str, Any]:
    if kind == _KIND_BIRTHS:
        request = build_reg_births_element(rows, farm=farm, redact_password=False)
    else:
        request = build_reg_movs_element(rows, farm=farm, redact_password=False)
    submit_type = _SUBMIT_TYPE[kind]
    logger.info(
        "CTS submit farm=%s kind=%s rows=%s type=%s",
        farm,
        kind,
        len(rows),
        submit_type,
    )
    receipt_root = transfer_ctws(submit_type, request)
    receipt = _parse_receipt(receipt_root)
    logger.info("CTS submit receipt farm=%s kind=%s receipt=%s", farm, kind, receipt)
    return _poll_validation(
        farm=farm,
        kind=kind,
        receipt=receipt,
        rows=rows,
        poll_attempts=poll_attempts,
        poll_sleep_s=poll_sleep_s,
    )


def send_pending_movements(
    db: Session,
    *,
    farm: str,
    rows: list[dict[str, Any]],
    poll_attempts: int = _DEFAULT_POLL_ATTEMPTS,
    poll_sleep_s: float = _DEFAULT_POLL_SLEEP_S,
) -> dict[str, Any]:
    """Submit selected pending rows to BCMS and mark accepted ones reported."""
    farm_key = farm.strip().upper()
    if not rows:
        raise CtsSubmitError("No pending movements selected.")

    batches: list[tuple[str, list[dict[str, Any]]]] = []
    births = filter_rows_for_kind(rows, "births")
    movements = filter_rows_for_kind(rows, "movements")
    if births:
        batches.append((_KIND_BIRTHS, births))
    if movements:
        batches.append((_KIND_MOVEMENTS, movements))
    if not batches:
        raise CtsSubmitError(
            "Selection has no birth / sale / death / move-on rows to send."
        )

    batch_results: list[dict[str, Any]] = []
    all_accepted: list[dict[str, Any]] = []
    all_rejected: list[dict[str, Any]] = []

    for kind, batch_rows in batches:
        try:
            result = _submit_kind(
                farm=farm_key,
                kind=kind,
                rows=batch_rows,
                poll_attempts=poll_attempts,
                poll_sleep_s=poll_sleep_s,
            )
        except CtsSubmitXmlError as exc:
            raise CtsSubmitError(str(exc)) from exc
        batch_results.append(result)
        all_accepted.extend(result["accepted"])
        all_rejected.extend(result["rejected"])
        receipt = result.get("receipt")
        if result["accepted"]:
            mark_movements_reported(
                db,
                farm=farm_key,
                items=result["accepted"],
                status="accepted",
                receipt=str(receipt) if receipt else None,
            )

    accepted_count = len(all_accepted)
    rejected_count = len(all_rejected)
    receipts = [r.get("receipt") for r in batch_results if r.get("receipt")]
    queued = any(r.get("queued") for r in batch_results)
    parts = [
        f"{accepted_count} accepted",
        f"{rejected_count} rejected",
    ]
    if receipts:
        parts.append("receipt " + ", ".join(str(r) for r in receipts))
    if queued:
        message = (
            f"Submitted to BCMS ({', '.join(parts)}). Validation was still queued; "
            "movements were moved to Awaiting CTS so they are not sent again."
        )
    else:
        message = f"Sent to BCMS: {', '.join(parts)}."
    if rejected_count:
        sample = all_rejected[0]
        reason = (sample.get("reasons") or ["Rejected"])[0]
        etag = sample.get("etag") or "?"
        message += f" First rejection: {etag} — {reason}"

    return {
        "ok": rejected_count == 0 and accepted_count > 0,
        "queued": queued,
        "message": message,
        "farm": farm_key,
        "accepted_count": accepted_count,
        "rejected_count": rejected_count,
        "receipts": receipts,
        "batches": [
            {
                "kind": r["kind"],
                "receipt": r.get("receipt"),
                "accepted_count": r["accepted_count"],
                "rejected_count": r["rejected_count"],
                "rejected": [
                    {
                        "etag": item.get("etag"),
                        "reasons": item.get("reasons"),
                        "id": (item.get("row") or {}).get("id"),
                    }
                    for item in r["rejected"]
                ],
            }
            for r in batch_results
        ],
    }


def send_deadline_day_movements(
    db: Session,
    *,
    farms: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Send pending movements that are exactly on the UK reporting deadline day.

    Births on day 17, sales/move-ons on day 3, deaths on day 7. Already-sent
    rows are not in the pending queue. Overdue rows are left for manual send.
    """
    if not cts_ddts_is_configured():
        raise CtsSubmitError(
            "CTS is not configured. Set CTS_DDTS_USERNAME and CTS_DDTS_PASSWORD."
        )

    requested = [
        cts_normalize_farm_key(f) for f in (farms or []) if f and str(f).strip()
    ]
    # Collapse ALH/BNK duplicates after normalize.
    seen: set[str] = set()
    ordered_requested: list[str] = []
    for farm in requested:
        if farm in seen:
            continue
        seen.add(farm)
        ordered_requested.append(farm)
    requested = ordered_requested
    ready = cts_configured_farms()
    if requested:
        unknown = [f for f in requested if not cts_is_ui_farm(f)]
        if unknown:
            raise CtsSubmitError(f"Unsupported farm: {', '.join(unknown)}")
        farm_keys = [f for f in requested if f in ready]
    else:
        farm_keys = list(ready)

    results: list[dict[str, Any]] = []
    ok = True
    for farm_key in farm_keys:
        pending = list_pending_movements(db, farm_key).get("rows") or []
        due = deadline_day_rows(pending)
        if not due:
            results.append(
                {
                    "farm": farm_key,
                    "ok": True,
                    "due_count": 0,
                    "accepted_count": 0,
                    "rejected_count": 0,
                    "message": "No deadline-day movements to send.",
                    "ids": [],
                }
            )
            continue
        ids = [row.get("id") for row in due]
        if dry_run:
            results.append(
                {
                    "farm": farm_key,
                    "ok": True,
                    "due_count": len(due),
                    "accepted_count": 0,
                    "rejected_count": 0,
                    "message": f"Dry run: would send {len(due)} deadline-day movement(s).",
                    "ids": ids,
                }
            )
            continue
        try:
            sent = send_pending_movements(db, farm=farm_key, rows=due)
        except (CtsSubmitError, CtsError, CtsSubmitXmlError) as exc:
            ok = False
            results.append(
                {
                    "farm": farm_key,
                    "ok": False,
                    "due_count": len(due),
                    "accepted_count": 0,
                    "rejected_count": 0,
                    "message": str(exc),
                    "ids": ids,
                }
            )
            continue
        farm_ok = bool(sent.get("ok"))
        if not farm_ok:
            ok = False
        results.append(
            {
                "farm": farm_key,
                "ok": farm_ok,
                "due_count": len(due),
                "accepted_count": sent.get("accepted_count", 0),
                "rejected_count": sent.get("rejected_count", 0),
                "message": sent.get("message") or "",
                "ids": ids,
            }
        )

    return {"ok": ok, "dry_run": dry_run, "results": results}


def record_deadline_day_receipt(
    db: Session,
    *,
    receipt: str,
    farms: list[str] | None = None,
) -> dict[str, Any]:
    """Mark today's deadline-day pending rows as accepted with an existing receipt.

    Use when BCMS already issued a receipt but the app timed out before validation.
    """
    receipt_num = str(receipt or "").strip()
    if not receipt_num:
        raise CtsSubmitError("Receipt number is required.")

    requested = [
        cts_normalize_farm_key(f) for f in (farms or []) if f and str(f).strip()
    ]
    seen: set[str] = set()
    ordered: list[str] = []
    for farm in requested:
        if farm in seen:
            continue
        seen.add(farm)
        ordered.append(farm)
    option_values = set(cts_farm_option_values())
    farm_keys = [
        f for f in (ordered or list(cts_farm_option_values())) if f in option_values
    ]

    results: list[dict[str, Any]] = []
    for farm_key in farm_keys:
        pending = list_pending_movements(db, farm_key).get("rows") or []
        due = deadline_day_rows(pending)
        if not due:
            results.append(
                {
                    "farm": farm_key,
                    "ok": True,
                    "due_count": 0,
                    "accepted_count": 0,
                    "rejected_count": 0,
                    "message": "No deadline-day movements to record.",
                    "ids": [],
                }
            )
            continue
        mark_movements_reported(
            db,
            farm=farm_key,
            items=due,
            status="accepted",
            receipt=receipt_num,
            error_message="Recorded from BCMS receipt after validation stayed queued.",
        )
        results.append(
            {
                "farm": farm_key,
                "ok": True,
                "due_count": len(due),
                "accepted_count": len(due),
                "rejected_count": 0,
                "message": (
                    f"Recorded receipt {receipt_num} for {len(due)} deadline-day "
                    "movement(s); moved to Awaiting CTS."
                ),
                "ids": [row.get("id") for row in due],
            }
        )
    return {"ok": True, "results": results}
