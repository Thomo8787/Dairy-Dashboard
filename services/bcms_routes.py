"""BCMS / CTS pages and JSON APIs (ported from Cwrt Malle Farm-Dashboard)."""

from __future__ import annotations

from flask import Response, jsonify, render_template, request

from services.auth import (
    PermissionContext,
    current_user,
    permission_required,
    user_has_permission,
)
from services.cts_client import CtsError, cts_status
from services.cts_movements import (
    list_archived_movements,
    list_awaiting_cts_movements,
    list_pending_movements,
)
from services.cts_reconcile import reconcile_farms, sync_farms
from services.cts_submit import CtsSubmitError, send_pending_movements
from services.cts_submit_xml import CtsSubmitXmlError, build_preview_xml
from services.database import get_session
from services.farms import HERD_FARM_OPTIONS
from services.herd_sync import start_herd_import_job


def _json_error(message: str, status: int = 400):
    return jsonify({"detail": message}), status


def _require_bcms_page():
    user = current_user()
    if user is None:
        return jsonify({"error": "Authentication required."}), 401
    if not user_has_permission(user, "perm_bcms"):
        return jsonify({"error": "Permission denied."}), 403
    return None


def _require_cts_sync():
    denied = _require_bcms_page()
    if denied is not None:
        return denied
    if not user_has_permission(current_user(), "perm_sync_cts"):
        return jsonify({"error": "Permission denied."}), 403
    return None


def _normalize_farms(raw: list[str]) -> list[str] | None:
    cleaned = [f.strip().upper() for f in raw if f and f.strip()]
    if not cleaned:
        return None
    invalid = [f for f in cleaned if f not in HERD_FARM_OPTIONS]
    if invalid:
        raise ValueError(
            f"Invalid farm(s): {', '.join(invalid)}. "
            f"Use {', '.join(HERD_FARM_OPTIONS)}."
        )
    return cleaned


def _page_extras(page_context_fn, *, active_nav: str, page_heading: str, **extra):
    user = current_user()
    extras = page_context_fn(active_nav=active_nav)
    extras["perms"] = PermissionContext(user)
    extras["page_heading"] = page_heading
    extras.update(extra)
    return extras


def _selected_pending_rows(session, *, farm_key: str, ids: list[str]) -> list[dict]:
    pending = list_pending_movements(session, farm_key)
    selected_ids = {item.strip() for item in ids if item and item.strip()}
    if selected_ids:
        rows = [row for row in pending["rows"] if row["id"] in selected_ids]
    else:
        rows = list(pending["rows"])
    holding = pending.get("holding") or ""
    for row in rows:
        row.setdefault("holding", holding)
    return rows


def register_bcms_routes(app, page_context_fn):
    @app.route("/bcms")
    @permission_required("perm_bcms")
    def bcms():
        return render_template(
            "bcms/cts_reconcile.html",
            **_page_extras(
                page_context_fn,
                active_nav="bcms_cts_reconcile",
                page_heading="CTS Reconcile",
                farm_options=list(HERD_FARM_OPTIONS),
                can_sync=user_has_permission(current_user(), "perm_sync_cts"),
                can_dc305_sync=user_has_permission(current_user(), "perm_sync_onedrive")
                or user_has_permission(current_user(), "perm_sync_cts"),
            ),
        )

    @app.route("/bcms/reconcile")
    @permission_required("perm_bcms")
    def bcms_cts_reconcile():
        return render_template(
            "bcms/cts_reconcile.html",
            **_page_extras(
                page_context_fn,
                active_nav="bcms_cts_reconcile",
                page_heading="CTS Reconcile",
                farm_options=list(HERD_FARM_OPTIONS),
                can_sync=user_has_permission(current_user(), "perm_sync_cts"),
                can_dc305_sync=user_has_permission(current_user(), "perm_sync_onedrive")
                or user_has_permission(current_user(), "perm_sync_cts"),
            ),
        )

    @app.route("/bcms/record-movements")
    @permission_required("perm_bcms")
    def bcms_record_movements():
        return render_template(
            "bcms/record_movements.html",
            **_page_extras(
                page_context_fn,
                active_nav="bcms_record_movements",
                page_heading="Record Movements",
                farm_options=list(HERD_FARM_OPTIONS),
                can_send=user_has_permission(current_user(), "perm_sync_cts"),
            ),
        )

    @app.route("/cts/reconcile")
    @permission_required("perm_bcms")
    def cts_reconcile_redirect():
        from flask import redirect, url_for

        return redirect(url_for("bcms_cts_reconcile"), code=307)

    @app.route("/api/cts/status")
    def api_cts_status():
        denied = _require_bcms_page()
        if denied is not None:
            return denied
        return jsonify(cts_status())

    @app.route("/api/cts/reconcile")
    def api_cts_reconcile():
        denied = _require_bcms_page()
        if denied is not None:
            return denied
        try:
            farms = _normalize_farms(request.args.getlist("farm"))
        except ValueError as exc:
            return _json_error(str(exc))
        with get_session() as session:
            return jsonify(reconcile_farms(session, farms=farms))

    @app.route("/api/cts/sync", methods=["POST"])
    def api_cts_sync():
        denied = _require_cts_sync()
        if denied is not None:
            return denied
        try:
            farms = _normalize_farms(request.args.getlist("farm"))
        except ValueError as exc:
            return _json_error(str(exc))
        try:
            with get_session() as session:
                return jsonify(sync_farms(session, farms=farms, source="manual"))
        except CtsError as exc:
            return _json_error(str(exc))

    @app.route("/api/cts/dc305-sync", methods=["POST"])
    def api_cts_dc305_sync():
        denied = _require_bcms_page()
        if denied is not None:
            return denied
        user = current_user()
        if not (
            user_has_permission(user, "perm_sync_onedrive")
            or user_has_permission(user, "perm_sync_cts")
        ):
            return jsonify({"error": "Permission denied."}), 403
        started, message = start_herd_import_job(force=False)
        status = 200 if started else 400
        return jsonify({"ok": started, "message": message, "detail": message}), status

    @app.route("/api/cts/movements/pending")
    def api_cts_movements_pending():
        denied = _require_bcms_page()
        if denied is not None:
            return denied
        farm_key = (request.args.get("farm") or "").strip().upper()
        if farm_key not in HERD_FARM_OPTIONS:
            return _json_error(
                f"Invalid farm. Use {', '.join(HERD_FARM_OPTIONS)}."
            )
        try:
            with get_session() as session:
                return jsonify(list_pending_movements(session, farm_key))
        except ValueError as exc:
            return _json_error(str(exc))

    @app.route("/api/cts/movements/awaiting-cts")
    def api_cts_movements_awaiting_cts():
        denied = _require_bcms_page()
        if denied is not None:
            return denied
        farm_key = (request.args.get("farm") or "").strip().upper()
        if farm_key not in HERD_FARM_OPTIONS:
            return _json_error(
                f"Invalid farm. Use {', '.join(HERD_FARM_OPTIONS)}."
            )
        try:
            with get_session() as session:
                return jsonify(list_awaiting_cts_movements(session, farm_key))
        except ValueError as exc:
            return _json_error(str(exc))

    @app.route("/api/cts/movements/archive")
    def api_cts_movements_archive():
        denied = _require_bcms_page()
        if denied is not None:
            return denied
        farm_key = (request.args.get("farm") or "").strip().upper()
        if farm_key not in HERD_FARM_OPTIONS:
            return _json_error(
                f"Invalid farm. Use {', '.join(HERD_FARM_OPTIONS)}."
            )
        try:
            with get_session() as session:
                return jsonify(list_archived_movements(session, farm_key))
        except ValueError as exc:
            return _json_error(str(exc))

    @app.route("/api/cts/movements/preview", methods=["POST"])
    def api_cts_movements_preview():
        denied = _require_bcms_page()
        if denied is not None:
            return denied
        body = request.get_json(silent=True) or {}
        farm_key = str(body.get("farm") or "").strip().upper()
        kind = str(body.get("kind") or "").strip().lower()
        ids = body.get("ids") or []
        if farm_key not in HERD_FARM_OPTIONS:
            return _json_error(
                f"Invalid farm. Use {', '.join(HERD_FARM_OPTIONS)}."
            )
        if kind not in {"births", "movements"}:
            return _json_error("kind must be 'births' or 'movements'.")
        try:
            with get_session() as session:
                rows = _selected_pending_rows(
                    session, farm_key=farm_key, ids=list(ids)
                )
                if not rows:
                    return _json_error("No pending movements selected.")
                filename, xml = build_preview_xml(
                    rows, farm=farm_key, kind=kind, redact_password=True
                )
        except (CtsSubmitXmlError, ValueError) as exc:
            return _json_error(str(exc))
        return Response(
            xml,
            mimetype="application/xml",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.route("/api/cts/movements/send", methods=["POST"])
    def api_cts_movements_send():
        denied = _require_cts_sync()
        if denied is not None:
            return denied
        body = request.get_json(silent=True) or {}
        farm_key = str(body.get("farm") or "").strip().upper()
        ids = body.get("ids") or []
        if farm_key not in HERD_FARM_OPTIONS:
            return _json_error(
                f"Invalid farm. Use {', '.join(HERD_FARM_OPTIONS)}."
            )
        try:
            with get_session() as session:
                rows = _selected_pending_rows(
                    session, farm_key=farm_key, ids=list(ids)
                )
                if not rows:
                    return _json_error("No pending movements selected.")
                return jsonify(send_pending_movements(session, farm=farm_key, rows=rows))
        except (CtsSubmitError, CtsError, CtsSubmitXmlError, ValueError) as exc:
            return _json_error(str(exc))
