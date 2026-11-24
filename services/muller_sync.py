"""Start a Müller portal download without blocking the website.

Locally this runs scripts/muller_download.py. On Render the website has no
Chromium, so the button starts the fetch-muller-collections cron instead.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = ROOT / "scripts" / "muller_download.py"
_TIMEOUT_SECONDS = 20 * 60
_RENDER_API = "https://api.render.com/v1"
_REMOTE_TRIGGER_COOLDOWN = timedelta(minutes=10)

_job_lock = threading.Lock()
_job_state: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "summary": None,
    "error": None,
}
_trigger_lock = threading.Lock()
_last_remote_trigger: datetime | None = None


def _on_render() -> bool:
    return bool((os.environ.get("RENDER") or "").strip())


def get_muller_portal_status() -> dict[str, Any]:
    with _job_lock:
        return dict(_job_state)


def start_muller_portal_import() -> tuple[bool, str, str]:
    """Return started, message, and status (started or scheduled)."""
    if _on_render():
        ok, message = _trigger_render_muller_cron()
        return ok, message, "scheduled"
    ok, message = _start_local_download()
    return ok, message, "started"


def _start_local_download() -> tuple[bool, str]:
    if not (os.environ.get("MULLER_USERNAME") or "").strip():
        return False, "Set MULLER_USERNAME to import from the Müller portal."
    if not (os.environ.get("MULLER_PASSWORD") or "").strip():
        return False, "Set MULLER_PASSWORD to import from the Müller portal."
    if not _SCRIPT.is_file():
        return False, "Müller download script is missing."
    with _job_lock:
        if _job_state["running"]:
            return False, "Müller portal import already in progress."
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
        env = os.environ.copy()
        env["MULLER_HEADLESS"] = "true"
        try:
            completed = subprocess.run(
                [sys.executable, str(_SCRIPT), "--headless"],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.exception("Müller portal import timed out")
            with _job_lock:
                _job_state["summary"] = None
                _job_state["error"] = "Müller portal import timed out."
                _job_state["running"] = False
                _job_state["finished_at"] = datetime.now(timezone.utc).isoformat()
            return
        except Exception as exc:
            logger.exception("Müller portal import failed")
            with _job_lock:
                _job_state["summary"] = None
                _job_state["error"] = str(exc)
                _job_state["running"] = False
                _job_state["finished_at"] = datetime.now(timezone.utc).isoformat()
            return

        output = "\n".join(
            part.strip()
            for part in (completed.stdout, completed.stderr)
            if part and part.strip()
        )
        tail = output[-4000:]
        with _job_lock:
            if completed.returncode == 0:
                _job_state["summary"] = {"output": tail, "returncode": 0}
                _job_state["error"] = None
            else:
                _job_state["summary"] = {"output": tail, "returncode": completed.returncode}
                _job_state["error"] = tail or f"Müller portal import failed ({completed.returncode})."
            _job_state["running"] = False
            _job_state["finished_at"] = datetime.now(timezone.utc).isoformat()

    threading.Thread(target=_run, daemon=True, name="muller-portal").start()
    return True, "Downloading this month's Müller collections…"


def _trigger_render_muller_cron() -> tuple[bool, str]:
    """Start fetch-muller-collections. A second click within 10 minutes is ignored."""
    global _last_remote_trigger
    api_key = (os.environ.get("RENDER_API_KEY") or "").strip()
    cron_id = (os.environ.get("RENDER_MULLER_CRON_ID") or "").strip()
    if not api_key or not cron_id:
        return (
            False,
            "Müller import is not set up yet. Add the Render API key and the fetch-muller-collections cron id to the website.",
        )
    with _trigger_lock:
        now = datetime.now(timezone.utc)
        if _last_remote_trigger and now - _last_remote_trigger < _REMOTE_TRIGGER_COOLDOWN:
            return (
                False,
                "A Müller import is already running. Refresh Collections in a few minutes.",
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
            logger.exception("Could not reach Render to start the Müller cron")
            return False, "Could not start the Müller import. Try again in a minute."
        if response.status_code != 200:
            logger.error(
                "Render Müller cron trigger failed: %s %s",
                response.status_code,
                response.text[:300],
            )
            return (
                False,
                "Could not start the Müller import. Try Trigger run on fetch-muller-collections in Render.",
            )
        _last_remote_trigger = now
    return True, "Müller import started. Refresh Collections in a few minutes."
