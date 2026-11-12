"""Background Müller collection download so the web worker stays free."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from services.muller_portal import MullerPortalError, muller_is_configured, sync_muller_collections

logger = logging.getLogger(__name__)

_job_lock = threading.Lock()
_job_state: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "summary": None,
    "error": None,
}


def get_muller_fetch_status() -> dict[str, Any]:
    with _job_lock:
        return dict(_job_state)


def start_muller_fetch_job() -> tuple[bool, str]:
    if not muller_is_configured():
        return (
            False,
            "Müller login is not set. Add MULLER_USERNAME and MULLER_PASSWORD "
            "on the dairy-dashboard service in Render.",
        )
    with _job_lock:
        if _job_state["running"]:
            return False, "Müller download already in progress."
        _job_state.update(
            {
                "running": True,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": None,
                "summary": None,
                "error": None,
            }
        )

    def _run() -> None:
        try:
            result = sync_muller_collections()
            with _job_lock:
                _job_state["summary"] = result
                _job_state["error"] = None
        except MullerPortalError as exc:
            logger.warning("Müller download failed: %s", exc)
            with _job_lock:
                _job_state["summary"] = None
                _job_state["error"] = str(exc)
        except Exception as exc:
            logger.exception("Müller download failed")
            with _job_lock:
                _job_state["summary"] = None
                _job_state["error"] = str(exc)
        finally:
            with _job_lock:
                _job_state["running"] = False
                _job_state["finished_at"] = datetime.now(timezone.utc).isoformat()

    threading.Thread(target=_run, daemon=True, name="muller-fetch").start()
    return True, "Downloading this month's Müller collections…"
