"""SenseHub pages and JSON APIs (ported from Cwrt Malle)."""

from __future__ import annotations

import threading

from flask import jsonify, render_template, request

from services.auth import (
    PermissionContext,
    current_user,
    permission_required,
    user_has_permission,
)
from services.database import SessionLocal, get_session
from services.sensehub_api import SenseHubError
from services.sensehub_import import (
    get_import_status,
    get_sensehub_report,
    is_import_running,
    mark_import_started,
    run_import_in_background,
)
from services.sensehub_youngstock import (
    DEFAULT_THRESHOLD,
    animal_events,
    cull_tags_to_remove,
    get_youngstock_job_status,
    import_youngstock_health,
    is_youngstock_job_running,
    list_low_health,
    list_tags_to_remove,
    list_unassigned_calves,
    refresh_tags_to_remove_data,
    run_backfill_in_background,
    save_scr_tag,
)


def _json_error(message: str, status: int = 400):
    return jsonify({"detail": message}), status


def _require_sensehub_page():
    user = current_user()
    if user is None:
        return jsonify({"error": "Authentication required."}), 401
    if not user_has_permission(user, "perm_sensehub"):
        return jsonify({"error": "Permission denied."}), 403
    return None


def _require_sensehub_action(permission: str):
    denied = _require_sensehub_page()
    if denied is not None:
        return denied
    if not user_has_permission(current_user(), permission):
        return jsonify({"error": "Permission denied."}), 403
    return None


def _page_extras(page_context_fn, *, active_nav: str, page_heading: str, **extra):
    user = current_user()
    extras = page_context_fn(active_nav=active_nav)
    extras["perms"] = PermissionContext(user)
    extras["page_heading"] = page_heading
    extras.update(extra)
    return extras


def register_sensehub_routes(app, page_context_fn):
    @app.route("/sensehub")
    @permission_required("perm_sensehub")
    def sensehub():
        return render_template(
            "sensehub/youngstock.html",
            **_page_extras(
                page_context_fn,
                active_nav="sensehub",
                page_heading="Youngstock Health Report",
                default_threshold=86,
                treated_within_days=None,
                print_title="Youngstock health",
                empty_message="No animals at or below that health index.",
            ),
        )

    @app.route("/sensehub/recently-treated")
    @permission_required("perm_sensehub")
    def sensehub_recently_treated():
        return render_template(
            "sensehub/youngstock.html",
            **_page_extras(
                page_context_fn,
                active_nav="sensehub-recently-treated",
                page_heading="Recently Treated Calves",
                default_threshold=100,
                treated_within_days=7,
                print_title="Recently treated calves",
                empty_message="No animals at or below that health index treated in the last 7 days.",
            ),
        )

    @app.route("/sensehub/tags-to-remove")
    @permission_required("perm_sensehub")
    def sensehub_tags_to_remove():
        return render_template(
            "sensehub/tags_to_remove.html",
            **_page_extras(
                page_context_fn,
                active_nav="sensehub-tags-to-remove",
                page_heading="Tags To Remove",
            ),
        )

    @app.route("/sensehub/unassigned")
    @permission_required("perm_sensehub")
    def sensehub_unassigned():
        return render_template(
            "sensehub/unassigned.html",
            **_page_extras(
                page_context_fn,
                active_nav="sensehub-unassigned",
                page_heading="Calves Not Assigned",
            ),
        )

    @app.route("/sensehub/reports")
    @permission_required("perm_sensehub")
    def sensehub_reports():
        return render_template(
            "sensehub/reports.html",
            **_page_extras(
                page_context_fn,
                active_nav="sensehub-reports",
                page_heading="SenseHub reports",
            ),
        )

    @app.route("/api/sensehub/tags-to-remove")
    def api_sensehub_tags_to_remove():
        denied = _require_sensehub_page()
        if denied:
            return denied
        try:
            with get_session() as db:
                return jsonify(list_tags_to_remove(db, auto_cull=False))
        except SenseHubError as exc:
            return _json_error(str(exc))

    @app.route("/api/sensehub/tags-to-remove/refresh", methods=["POST"])
    def api_sensehub_refresh_tags_to_remove():
        denied = _require_sensehub_action("perm_sync_sensehub")
        if denied:
            return denied
        try:
            with get_session() as db:
                return jsonify(refresh_tags_to_remove_data(db))
        except SenseHubError as exc:
            return _json_error(str(exc))

    @app.route("/api/sensehub/tags-to-remove/cull-all", methods=["POST"])
    def api_sensehub_cull_tags_to_remove():
        denied = _require_sensehub_action("perm_sync_sensehub_cull")
        if denied:
            return denied
        body = request.get_json(silent=True) or {}
        try:
            with get_session() as db:
                return jsonify(cull_tags_to_remove(db, animal_ids=body.get("animal_ids") or []))
        except SenseHubError as exc:
            return _json_error(str(exc))

    @app.route("/api/sensehub/tags-to-remove/cull", methods=["POST"])
    def api_sensehub_cull_one_tag_to_remove():
        denied = _require_sensehub_action("perm_sync_sensehub_cull")
        if denied:
            return denied
        body = request.get_json(silent=True) or {}
        try:
            with get_session() as db:
                return jsonify(cull_tags_to_remove(db, animal_id=body.get("animal_id")))
        except SenseHubError as exc:
            return _json_error(str(exc))

    @app.route("/api/sensehub/unassigned")
    def api_sensehub_unassigned():
        denied = _require_sensehub_page()
        if denied:
            return denied
        categories = request.args.getlist("category")
        with get_session() as db:
            return jsonify(list_unassigned_calves(db, categories=categories or None))

    @app.route("/api/sensehub/unassigned/scr-tag", methods=["POST"])
    def api_sensehub_save_scr_tag():
        denied = _require_sensehub_page()
        if denied:
            return denied
        body = request.get_json(silent=True) or {}
        try:
            with get_session() as db:
                return jsonify(
                    save_scr_tag(
                        db,
                        row_key=body.get("row_key") or "",
                        farm=body.get("farm"),
                        cow_id=body.get("cow_id"),
                        etag=body.get("etag"),
                        scr_tag=body.get("scr_tag"),
                    )
                )
        except (ValueError, SenseHubError) as exc:
            return _json_error(str(exc))

    @app.route("/api/sensehub/youngstock")
    def api_sensehub_youngstock():
        denied = _require_sensehub_page()
        if denied:
            return denied
        try:
            threshold = float(request.args.get("threshold") or DEFAULT_THRESHOLD)
        except (TypeError, ValueError):
            threshold = DEFAULT_THRESHOLD
        treated_raw = request.args.get("treated_within_days")
        treated_within_days = None
        if treated_raw not in (None, ""):
            try:
                treated_within_days = max(0, min(365, int(treated_raw)))
            except (TypeError, ValueError):
                treated_within_days = None
        with get_session() as db:
            return jsonify(
                list_low_health(
                    db, threshold=threshold, treated_within_days=treated_within_days
                )
            )

    @app.route("/api/sensehub/youngstock/job")
    def api_sensehub_youngstock_job():
        if current_user() is None:
            return jsonify({"error": "Authentication required."}), 401
        return jsonify(get_youngstock_job_status())

    @app.route("/api/sensehub/youngstock/backfill", methods=["POST"])
    def api_sensehub_youngstock_backfill():
        denied = _require_sensehub_action("perm_sync_sensehub")
        if denied:
            return denied
        if is_youngstock_job_running():
            return jsonify(
                {"status": "running", "message": "A SenseHub backfill is already running."}
            )
        days_raw = request.args.get("days")
        days = None
        if days_raw not in (None, ""):
            try:
                days = max(1, min(730, int(days_raw)))
            except (TypeError, ValueError):
                days = None
        thread = threading.Thread(
            target=run_backfill_in_background,
            args=(SessionLocal, days),
            kwargs={"force": True},
            daemon=True,
        )
        thread.start()
        if days is None:
            message = "Re-downloading all SenseHub youngstock history…"
        else:
            message = f"Re-downloading the last {days} days from SenseHub…"
        return jsonify({"status": "started", "message": message, "days": days})

    @app.route("/api/sensehub/youngstock/<animal_id>/events")
    def api_sensehub_youngstock_events(animal_id: str):
        denied = _require_sensehub_page()
        if denied:
            return denied
        with get_session() as db:
            return jsonify(animal_events(db, animal_id))

    @app.route("/api/sensehub/youngstock/import", methods=["POST"])
    def api_sensehub_youngstock_import():
        denied = _require_sensehub_action("perm_sync_sensehub")
        if denied:
            return denied
        try:
            with get_session() as db:
                return jsonify(import_youngstock_health(db))
        except SenseHubError as exc:
            return _json_error(str(exc))

    @app.route("/api/sensehub")
    def api_sensehub_report():
        denied = _require_sensehub_page()
        if denied:
            return denied
        name = request.args.get("name") or None
        with get_session() as db:
            return jsonify(get_sensehub_report(db, name=name))

    @app.route("/api/sensehub/import/status")
    def api_sensehub_import_status():
        if current_user() is None:
            return jsonify({"error": "Authentication required."}), 401
        return jsonify(get_import_status())

    @app.route("/api/sensehub/import", methods=["POST"])
    def api_sensehub_import():
        denied = _require_sensehub_action("perm_sync_sensehub")
        if denied:
            return denied
        if is_import_running():
            return jsonify(
                {"status": "running", "message": "SenseHub import already in progress."}
            )
        mark_import_started()
        thread = threading.Thread(
            target=run_import_in_background,
            args=(SessionLocal,),
            daemon=True,
        )
        thread.start()
        return jsonify({"status": "started", "message": "SenseHub import started."})

    @app.route("/api/sensehub/status")
    def api_sensehub_status():
        denied = _require_sensehub_page()
        if denied:
            return denied
        with get_session() as db:
            report = get_sensehub_report(db)
        return jsonify(
            {
                "configured": report["configured"],
                "latest_import": report["latest_import"],
                "farm_id": report["farm_id"],
                "farm_name": report["farm_name"],
                "report_count": len(report["reports"]),
                "import_status": get_import_status(),
            }
        )
