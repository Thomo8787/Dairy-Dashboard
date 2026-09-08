"""Import SenseHub reports into the local database."""

from __future__ import annotations

import datetime as dt
import threading
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from services.sensehub_config import SENSEHUB_FARM_ID, sensehub_is_configured
from services.database import SenseHubReportSnapshot
from services.sensehub_api import (
    DEFAULT_REPORT,
    PRIORITY_REPORTS,
    SenseHubAuthError,
    SenseHubConfigError,
    SenseHubError,
    fetch_all_reports,
)
from services.sensehub_youngstock import (
    auto_cull_exited_sensehub_animals,
    refresh_sensehub_list_snapshots,
    save_from_reports,
)

_lock = threading.Lock()
_import_status: dict[str, Any] = {
    "status": "idle",
    "message": "",
    "latest_import": None,
    "reports_imported": 0,
    "rows_imported": 0,
    "needs_auth": False,
    "configured": False,
}


def get_import_status() -> dict[str, Any]:
    with _lock:
        status = dict(_import_status)
    status["configured"] = sensehub_is_configured()
    return status


def _set_status(**kwargs: Any) -> None:
    with _lock:
        _import_status.update(kwargs)


def is_import_running() -> bool:
    with _lock:
        return _import_status.get("status") == "running"


def mark_import_started() -> None:
    _set_status(
        status="running",
        message="Starting SenseHub import…",
        reports_imported=0,
        rows_imported=0,
        needs_auth=False,
    )


def import_sensehub(db: Session) -> dict[str, Any]:
    _set_status(
        status="running",
        message="Logging in to SenseHub…",
        reports_imported=0,
        rows_imported=0,
        needs_auth=False,
    )
    try:
        payload = fetch_all_reports()
        reports = payload.get("reports") or []
        if not reports:
            raise SenseHubError("SenseHub returned no reports.")

        fetched_at = dt.datetime.now()
        db.execute(delete(SenseHubReportSnapshot))
        db.flush()

        reports_by_key: dict[int, dict[str, Any]] = {}
        for report in reports:
            reports_by_key[int(report["report_key"])] = report
        reports = list(reports_by_key.values())

        rows_imported = 0
        for report in reports:
            rows = report.get("rows") or []
            snapshot = SenseHubReportSnapshot(
                report_key=int(report["report_key"]),
                report_name=str(report["report_name"]),
                category=report.get("category"),
                title=str(report.get("title") or report["report_name"]),
                row_count=int(report.get("row_count") or len(rows)),
                payload={
                    "columns": report.get("columns") or [],
                    "rows": rows,
                    "report_time": report.get("report_time"),
                    "farm_id": payload.get("farm_id"),
                    "farm_name": payload.get("farm_name"),
                    "software_version": payload.get("software_version"),
                },
                fetched_at=fetched_at,
            )
            db.add(snapshot)
            rows_imported += len(rows)

        db.flush()
        save_from_reports(db, reports)
        try:
            refresh_sensehub_list_snapshots(
                db,
                fetched_at=fetched_at,
                farm_id=payload.get("farm_id"),
                farm_name=payload.get("farm_name"),
                software_version=payload.get("software_version"),
            )
        except SenseHubError:
            pass
        try:
            auto_cull_exited_sensehub_animals(db)
        except Exception:
            pass
        db.commit()
        latest = fetched_at.isoformat()
        result = {
            "reports_imported": len(reports),
            "rows_imported": rows_imported,
            "latest_import": latest,
            "farm_id": payload.get("farm_id") or SENSEHUB_FARM_ID,
            "farm_name": payload.get("farm_name"),
        }
        _set_status(
            status="complete",
            message=f"Imported {len(reports)} SenseHub reports ({rows_imported} rows).",
            latest_import=latest,
            reports_imported=len(reports),
            rows_imported=rows_imported,
            needs_auth=False,
        )
        return result
    except SenseHubConfigError as exc:
        db.rollback()
        _set_status(status="error", message=str(exc), needs_auth=True)
        raise
    except SenseHubAuthError as exc:
        db.rollback()
        _set_status(status="error", message=str(exc), needs_auth=True)
        raise
    except Exception as exc:
        db.rollback()
        _set_status(status="error", message=str(exc), needs_auth=False)
        raise


def run_import_in_background(db_factory) -> None:
    db = db_factory()
    try:
        import_sensehub(db)
    except Exception:
        pass
    finally:
        db.close()


def get_sensehub_report(db: Session, *, name: str | None = None) -> dict[str, Any]:
    snapshots = list(
        db.scalars(
            select(SenseHubReportSnapshot).order_by(
                SenseHubReportSnapshot.category,
                SenseHubReportSnapshot.title,
            )
        ).all()
    )
    latest = db.scalar(select(func.max(SenseHubReportSnapshot.fetched_at)))
    summaries = [
        {
            "report_key": item.report_key,
            "report_name": item.report_name,
            "title": item.title,
            "category": item.category,
            "row_count": item.row_count,
            "priority": item.report_name in PRIORITY_REPORTS,
        }
        for item in snapshots
    ]
    selected = None
    if name:
        selected = next((item for item in snapshots if item.report_name == name), None)
    elif snapshots:
        selected = next(
            (item for item in snapshots if item.report_name == DEFAULT_REPORT),
            None,
        )
        if selected is None:
            selected = next(
                (
                    item
                    for item in snapshots
                    if item.report_name in PRIORITY_REPORTS and item.row_count
                ),
                snapshots[0],
            )

    payload = (selected.payload if selected else {}) or {}
    return {
        "configured": sensehub_is_configured(),
        "farm_id": (payload.get("farm_id") if selected else None) or SENSEHUB_FARM_ID,
        "farm_name": payload.get("farm_name") if selected else None,
        "software_version": payload.get("software_version") if selected else None,
        "latest_import": latest.isoformat() if latest else None,
        "reports": summaries,
        "selected": selected.report_name if selected else None,
        "columns": payload.get("columns") or [],
        "rows": payload.get("rows") or [],
        "import_status": get_import_status(),
    }
