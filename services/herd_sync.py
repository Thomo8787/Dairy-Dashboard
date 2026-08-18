"""Background OneDrive DCEXPORT import for the Events pages."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_job_lock = threading.Lock()
_job_state: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "summary": None,
    "error": None,
}


def get_herd_import_status() -> dict[str, Any]:
    with _job_lock:
        return dict(_job_state)


def herd_import_status_payload() -> dict[str, Any]:
    """JSON-safe peek at the background import (does not consume the result)."""
    status = get_herd_import_status()

    def iso(value: Any) -> str | None:
        if value is None:
            return None
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    running = bool(status.get("running"))
    error = status.get("error")
    summary = status.get("summary")
    return {
        "running": running,
        "summary": summary,
        "error": error,
        "started_at": iso(status.get("started_at")),
        "finished_at": iso(status.get("finished_at")),
        "complete": (not running) and bool(summary or error),
        "ok": (not running) and bool(summary) and not error,
    }


def consume_herd_import_result() -> dict[str, Any] | None:
    with _job_lock:
        if _job_state["running"] or not (_job_state["summary"] or _job_state["error"]):
            return None
        result = {
            "summary": _job_state["summary"],
            "error": _job_state["error"],
            "finished_at": _job_state["finished_at"],
        }
        _job_state["summary"] = None
        _job_state["error"] = None
        return result


def start_herd_import_job(*, force: bool = False) -> tuple[bool, str]:
    with _job_lock:
        if _job_state["running"]:
            return False, "A herd import is already running."
        _job_state.update(
            {
                "running": True,
                "started_at": datetime.now(timezone.utc),
                "finished_at": None,
                "summary": None,
                "error": None,
            }
        )

    def _runner() -> None:
        try:
            from services.database import get_session, init_db
            from services.herd_full_import import format_herd_import_summary, import_herd_exports

            init_db()
            with get_session() as session:
                result = import_herd_exports(session, force=force)
            summary = format_herd_import_summary(result)
            with _job_lock:
                _job_state["summary"] = summary
                _job_state["error"] = None
            logger.info("Herd import finished: %s", summary)
        except Exception as exc:
            logger.exception("Herd import failed")
            with _job_lock:
                _job_state["summary"] = None
                _job_state["error"] = str(exc)
        finally:
            with _job_lock:
                _job_state["running"] = False
                _job_state["finished_at"] = datetime.now(timezone.utc)

    threading.Thread(target=_runner, name="fetch-one-drive-data", daemon=True).start()
    return True, "Herd import started from OneDrive. Stay on this page — it will show Sync complete when finished."
