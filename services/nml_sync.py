"""Background NML PDF import so the web worker can keep answering requests."""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

from services.nml_email import NML_LOOKBACK_DAYS
from services.nml_import import import_nml_results, nml_is_configured

logger = logging.getLogger(__name__)

_job_lock = threading.Lock()
_job_state: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "days": None,
    "summary": None,
    "error": None,
}


def get_nml_import_status() -> dict[str, Any]:
    with _job_lock:
        return dict(_job_state)


def start_nml_import_job(
    *,
    days: int | None = None,
    full_history: bool = False,
    force: bool = False,
) -> tuple[bool, str]:
    if force:
        if not (os.environ.get("OUTLOOK_MAILBOX") or "").strip():
            return False, "Set OUTLOOK_MAILBOX to fetch NML emails."
    elif not nml_is_configured():
        return False, "NML import is not configured. Set OUTLOOK_MAILBOX or LOCAL_NML_DIR."
    with _job_lock:
        if _job_state["running"]:
            return False, "NML import already in progress."
        _job_state.update(
            {
                "running": True,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": None,
                "days": days,
                "summary": None,
                "error": None,
            }
        )

    def _run() -> None:
        try:
            result = import_nml_results(full_history=full_history, days=days, force=force)
            with _job_lock:
                _job_state["summary"] = result
                _job_state["error"] = None
        except Exception as exc:
            logger.exception("NML import failed")
            with _job_lock:
                _job_state["summary"] = None
                _job_state["error"] = str(exc)
        finally:
            with _job_lock:
                _job_state["running"] = False
                _job_state["finished_at"] = datetime.now(timezone.utc).isoformat()

    threading.Thread(target=_run, daemon=True, name="nml-import").start()
    lookback = days or NML_LOOKBACK_DAYS
    action = "Re-scanning mailbox" if force else "Scanning mailbox"
    return True, f"{action} for NML PDFs (last {lookback} day(s))…"
