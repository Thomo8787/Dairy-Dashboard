"""Background OneDrive DCEXPORT import for the Events pages."""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

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


def herd_import_allowed_on_web() -> bool:
    """OneDrive DCEXPORT import is too large for the Render starter web box."""
    return not bool(os.environ.get("RENDER", "").strip())


def start_herd_import_job(*, force: bool = False) -> tuple[bool, str]:
    if not herd_import_allowed_on_web():
        return (
            False,
            "Herd import runs on the daily OneDrive cron (05:00 UK). "
            "Using it here restarts the website.",
        )
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


_RENDER_API = "https://api.render.com/v1"
_trigger_lock = threading.Lock()
_last_remote_trigger: datetime | None = None
_REMOTE_TRIGGER_COOLDOWN = timedelta(minutes=10)


def trigger_herd_file_update() -> tuple[bool, str]:
    """Start the herd import.

    Locally this runs in the web process. On Render it starts the
    fetch-one-drive-data cron, because the events file is too large for the
    website. A second click within 10 minutes is ignored: Render cancels the
    run that is already going.
    """
    if herd_import_allowed_on_web():
        return start_herd_import_job(force=False)

    with _trigger_lock:
        global _last_remote_trigger
        now = datetime.now(timezone.utc)
        if _last_remote_trigger and now - _last_remote_trigger < _REMOTE_TRIGGER_COOLDOWN:
            return (
                False,
                "A herd file update is already running. Refresh Home in a few minutes.",
            )
        ok, message = _trigger_render_herd_cron()
        if ok:
            _last_remote_trigger = now
        return ok, message


def _trigger_render_herd_cron() -> tuple[bool, str]:
    api_key = (os.environ.get("RENDER_API_KEY") or "").strip()
    cron_id = (os.environ.get("RENDER_HERD_CRON_ID") or "").strip()
    if not api_key or not cron_id:
        return (
            False,
            "Herd file update is not set up yet. Add the Render API key and the fetch-one-drive-data cron id to the website.",
        )
    url = f"{_RENDER_API}/cron-jobs/{cron_id}/runs"
    try:
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            timeout=20,
        )
    except requests.RequestException:
        logger.exception("Could not reach Render to start the herd cron")
        return False, "Could not start the herd file update. Try again in a minute."
    if response.status_code != 200:
        logger.error(
            "Render herd cron trigger failed: %s %s",
            response.status_code,
            response.text[:300],
        )
        return False, "Could not start the herd file update. Try Trigger run on fetch-one-drive-data in Render."
    return True, "Herd file update started. Refresh Home in a few minutes."
