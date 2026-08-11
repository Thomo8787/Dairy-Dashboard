"""Thomasson Farms Dashboard web application."""

import logging
import os
import secrets
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for

from services.auth import (
    PERMISSION_KEYS,
    PERMISSION_LABELS,
    PUBLIC_ENDPOINTS,
    admin_required,
    authenticate,
    create_user,
    current_user,
    delete_user,
    first_allowed_endpoint,
    list_users,
    login_user,
    logout_user,
    permission_required,
    permissions_from_form,
    update_user,
    user_has_permission,
    user_to_template,
)
from services.database import (
    ensure_auth_ready,
    get_dashboard_summary,
    get_recent_records,
    health_check,
    save_dataframe,
)
from services.excel_parser import parse_excel_file
from services.farms import FARMS, active_farms
from services.graph_client import (
    auth_mode,
    build_auth_url,
    clear_saved_token,
    exchange_code_for_token,
    get_connected_account,
    get_redirect_uri,
    require_azure_config,
    save_token_result,
)
from services.milking_efficiency_summary import (
    METRIC_BY_KEY,
    SHIFT_OPTIONS,
    TREND_DAY_COUNT,
    build_metric_trend,
    build_pen_breakdown,
    build_seven_day_summary,
)
from services.navigation import filter_nav_items, parent_nav_id
from services.parlour_scheduler import start_parlour_hourly_sync
from services.parlour_sync import (
    IMPORT_DAY_OPTIONS,
    consume_manual_sync_result,
    get_manual_sync_status,
    start_manual_sync_job,
)

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")

# Local/dev: templates/static refresh on browser reload; Python auto-reloads via
# FLASK_USE_RELOADER (enabled by the desktop launcher).
_running_local = not bool(os.environ.get("RENDER"))
if _running_local or os.environ.get("FLASK_DEBUG", "").lower() == "true":
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
    app.jinja_env.auto_reload = True


def _use_reloader() -> bool:
    raw = os.environ.get("FLASK_USE_RELOADER")
    if raw is None:
        return _running_local
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _should_start_background_jobs() -> bool:
    """Avoid starting the hourly sync twice under Werkzeug's reloader parent."""
    if not _use_reloader():
        return True
    return os.environ.get("WERKZEUG_RUN_MAIN") == "true"


def _ensure_database():
    if getattr(app, "_db_initialized", False):
        return
    try:
        _, seed_error = ensure_auth_ready()
        app._db_initialized = True
        app._auth_seed_error = seed_error
        logger.info("Database tables ready")
        if seed_error:
            logger.error("Admin seed: %s", seed_error)
    except Exception:
        logger.exception("Database init failed; will retry on next request")


def _import_items(items: list[dict]) -> int:
    imported = 0
    for item in items:
        records = parse_excel_file(item["file_path"])
        if not records:
            continue
        save_dataframe(
            {
                "filename": item["filename"],
                "email_subject": item["email_subject"],
                "email_received_at": item["email_received_at"],
            },
            records,
        )
        imported += 1
    return imported


def _page_context(active_nav: str = "home", **extra):
    connected = None
    try:
        connected = get_connected_account()
    except Exception:
        logger.exception("Could not load Microsoft connection status")

    user = current_user()
    ctx = {
        "nav_items": filter_nav_items(user),
        "active_nav": active_nav,
        "active_parent_nav": parent_nav_id(active_nav),
        "farms": FARMS,
        "focus_farms": active_farms(),
        "connected": connected,
        "auth_mode": auth_mode(),
        "redirect_uri": get_redirect_uri(),
        "current_user": user_to_template(user),
        "can_sync_outlook": user_has_permission(user, "perm_sync_outlook"),
        "can_sync_onedrive": user_has_permission(user, "perm_sync_onedrive"),
        "can_sync_dataflow": user_has_permission(user, "perm_sync_dataflow"),
    }
    ctx.update(extra)
    return ctx


@app.before_request
def ensure_database():
    _ensure_database()


@app.before_request
def require_login():
    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
        return None
    if request.endpoint.startswith("static"):
        return None
    user = current_user()
    if user is None:
        if request.endpoint and request.endpoint.startswith("milking_efficiency"):
            return jsonify({"error": "Authentication required."}), 401
        return redirect(url_for("login", next=request.path))
    return None


@app.context_processor
def inject_globals():
    return {"app_name": "Thomasson Farms Dashboard"}


@app.route("/login", methods=["GET", "POST"])
def login():
    user = current_user()
    if user is not None:
        return redirect(url_for(first_allowed_endpoint(user)))

    seed_error = getattr(app, "_auth_seed_error", None)
    error = None
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        account = authenticate(email, password)
        if account is None:
            error = "Invalid email or password."
        else:
            login_user(account)
            flash(f"Signed in as {account.email}.", "success")
            next_url = request.args.get("next") or request.form.get("next") or ""
            if next_url.startswith("/") and not next_url.startswith("//"):
                return redirect(next_url)
            return redirect(url_for(first_allowed_endpoint(account)))

    return render_template(
        "login.html",
        error=error,
        seed_error=seed_error,
        next=request.args.get("next") or "",
    )


@app.route("/logout", methods=["POST"])
def logout():
    logout_user()
    flash("Signed out.", "info")
    return redirect(url_for("login"))


@app.route("/")
@permission_required("perm_home")
def home():
    summary = {"total_records": 0, "total_batches": 0, "latest_import": None, "recent_batches": [], "category_totals": []}
    records = []
    try:
        summary = get_dashboard_summary()
        records = get_recent_records(limit=50)
    except Exception:
        logger.exception("Home data load failed")

    return render_template(
        "home.html",
        summary=summary,
        records=records,
        **_page_context(active_nav="home"),
    )


@app.route("/office")
@permission_required("perm_office")
def office():
    return render_template(
        "office.html",
        **_page_context(active_nav="office"),
    )


@app.route("/users")
@admin_required
def users_admin():
    return render_template(
        "users.html",
        users=list_users(),
        permission_keys=PERMISSION_KEYS,
        permission_labels=PERMISSION_LABELS,
        **_page_context(active_nav="users"),
    )


@app.route("/users/create", methods=["POST"])
@admin_required
def users_create():
    try:
        create_user(
            email=request.form.get("email") or "",
            password=request.form.get("password") or "",
            is_admin=bool(request.form.get("is_admin")),
            is_active=True,
            permissions=permissions_from_form(request.form),
        )
        flash("User created.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    except Exception as exc:
        logger.exception("Create user failed")
        flash(f"Could not create user: {exc}", "error")
    return redirect(url_for("users_admin"))


@app.route("/users/<int:user_id>/update", methods=["POST"])
@admin_required
def users_update(user_id: int):
    password = (request.form.get("password") or "").strip() or None
    try:
        updated = update_user(
            user_id,
            email=request.form.get("email") or "",
            is_admin=bool(request.form.get("is_admin")),
            is_active=bool(request.form.get("is_active")),
            permissions=permissions_from_form(request.form),
            password=password,
        )
        actor = current_user()
        if actor and actor.id == updated.id:
            session["user_email"] = updated.email
        flash("User updated.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    except Exception as exc:
        logger.exception("Update user failed")
        flash(f"Could not update user: {exc}", "error")
    return redirect(url_for("users_admin"))


@app.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def users_delete(user_id: int):
    actor = current_user()
    try:
        delete_user(user_id, acting_user_id=actor.id if actor else None)
        flash("User deleted.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    except Exception as exc:
        logger.exception("Delete user failed")
        flash(f"Could not delete user: {exc}", "error")
    return redirect(url_for("users_admin"))


@app.route("/parlours")
@permission_required("perm_parlours")
def parlours():
    return render_template(
        "parlours.html",
        **_page_context(active_nav="parlours"),
    )


@app.route("/parlours/milking-efficiency")
@permission_required("perm_parlours")
def milking_efficiency():
    farm_code = (request.args.get("farm") or "ALH").upper()
    if farm_code not in {farm.code for farm in FARMS}:
        farm_code = "ALH"
    shift_id = request.args.get("shift") or "Morning"
    if shift_id not in {item["id"] for item in SHIFT_OPTIONS}:
        shift_id = "Morning"

    summary = build_seven_day_summary(farm_code=farm_code, shift_id=shift_id)
    sync_status = get_manual_sync_status()
    if not sync_status.get("running"):
        finished = consume_manual_sync_result()
        if finished and finished.get("error"):
            flash(f"Last parlour import failed: {finished['error']}", "error")
        elif finished and finished.get("summary"):
            flash(finished["summary"], "success")

    return render_template(
        "milking_efficiency.html",
        summary=summary,
        shift_options=SHIFT_OPTIONS,
        import_day_options=IMPORT_DAY_OPTIONS,
        selected_farm=farm_code,
        selected_shift=shift_id,
        manual_sync_status=sync_status,
        **_page_context(active_nav="milking_efficiency"),
    )


@app.route("/parlours/milking-efficiency/pens")
def milking_efficiency_pens():
    user = current_user()
    if user is None:
        return jsonify({"error": "Authentication required."}), 401
    if not user_has_permission(user, "perm_parlours"):
        return jsonify({"error": "Permission denied."}), 403

    farm_code = (request.args.get("farm") or "ALH").upper()
    if farm_code not in {farm.code for farm in FARMS}:
        farm_code = "ALH"
    shift_id = request.args.get("shift") or "Morning"
    if shift_id not in {item["id"] for item in SHIFT_OPTIONS}:
        shift_id = "Morning"

    date_raw = (request.args.get("date") or "").strip()
    try:
        milking_date = datetime.strptime(date_raw, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "Invalid date. Use YYYY-MM-DD."}), 400

    return jsonify(
        build_pen_breakdown(
            farm_code=farm_code,
            shift_id=shift_id,
            milking_date=milking_date,
        )
    )


@app.route("/parlours/milking-efficiency/trend")
def milking_efficiency_trend():
    user = current_user()
    if user is None:
        return jsonify({"error": "Authentication required."}), 401
    if not user_has_permission(user, "perm_parlours"):
        return jsonify({"error": "Permission denied."}), 403

    farm_code = (request.args.get("farm") or "ALH").upper()
    if farm_code not in {farm.code for farm in FARMS}:
        farm_code = "ALH"

    metric_key = (request.args.get("metric") or "").strip()
    if metric_key not in METRIC_BY_KEY:
        return jsonify({"error": "Unknown metric."}), 400

    try:
        days = int(request.args.get("days") or TREND_DAY_COUNT)
    except ValueError:
        days = TREND_DAY_COUNT

    pen = (request.args.get("pen") or "").strip() or None

    return jsonify(
        build_metric_trend(
            farm_code=farm_code,
            metric_key=metric_key,
            days=days,
            pen=pen,
        )
    )


@app.route("/stock-inventory")
@permission_required("perm_stock")
def stock_inventory():
    return render_template(
        "stock_inventory.html",
        **_page_context(active_nav="stock_inventory"),
    )


# Keep old URL working
@app.route("/dashboard")
@permission_required("perm_home")
def dashboard():
    return redirect(url_for("home"))


@app.route("/auth/login")
@permission_required("perm_office")
def auth_login():
    missing = require_azure_config()
    if missing:
        flash(f"Missing Microsoft Graph configuration: {', '.join(missing)}", "error")
        return redirect(url_for("office"))

    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    return redirect(build_auth_url(state))


@app.route("/auth/callback")
@permission_required("perm_office")
def auth_callback():
    error = request.args.get("error")
    if error:
        flash(f"Microsoft sign-in error: {error} — {request.args.get('error_description', '')}", "error")
        return redirect(url_for("office"))

    state = request.args.get("state")
    if not state or state != session.get("oauth_state"):
        flash("Microsoft sign-in failed: invalid state. Try Connect Microsoft 365 again.", "error")
        return redirect(url_for("office"))

    code = request.args.get("code")
    if not code:
        flash("Microsoft sign-in failed: no authorization code returned.", "error")
        return redirect(url_for("office"))

    try:
        result = exchange_code_for_token(code)
        user_email = save_token_result(result)
        session.pop("oauth_state", None)
        flash(f"Connected as {user_email}. You can sync Outlook and OneDrive.", "success")
    except Exception as exc:
        logger.exception("OAuth callback failed")
        flash(f"Microsoft sign-in failed: {exc}", "error")

    return redirect(url_for("office"))


@app.route("/auth/logout", methods=["POST"])
@permission_required("perm_office")
def auth_logout():
    try:
        clear_saved_token()
        flash("Disconnected Microsoft 365.", "info")
    except Exception as exc:
        flash(f"Disconnect failed: {exc}", "error")
    return redirect(url_for("office"))


@app.route("/sync", methods=["POST"])
@permission_required("perm_sync_outlook")
def sync_from_outlook():
    missing = require_azure_config()
    if missing:
        flash(f"Missing Microsoft Graph configuration: {', '.join(missing)}", "error")
        return redirect(url_for("office"))

    if auth_mode() == "delegated" and not get_connected_account():
        flash("Connect Microsoft 365 first, then sync Outlook.", "error")
        return redirect(url_for("office"))

    try:
        attachments = GraphEmailService().fetch_excel_attachments()
        if not attachments:
            flash("No matching Excel attachments found in Outlook.", "info")
            return redirect(url_for("office"))

        imported = _import_items(attachments)
        flash(f"Imported {imported} Excel file(s) from Outlook.", "success")
    except Exception as exc:
        logger.exception("Outlook sync failed")
        flash(f"Outlook sync failed: {exc}", "error")

    return redirect(url_for("office"))


@app.route("/sync-onedrive", methods=["POST"])
@permission_required("perm_sync_onedrive")
def sync_from_onedrive():
    missing = require_azure_config()
    if missing:
        flash(f"Missing Microsoft Graph configuration: {', '.join(missing)}", "error")
        return redirect(url_for("office"))

    if auth_mode() == "delegated" and not get_connected_account():
        flash("Connect Microsoft 365 first, then sync OneDrive.", "error")
        return redirect(url_for("office"))

    try:
        files = GraphOneDriveService().fetch_excel_files()
        if not files:
            flash("No Excel files found in the configured OneDrive folder.", "info")
            return redirect(url_for("office"))

        imported = _import_items(files)
        flash(f"Imported {imported} Excel file(s) from OneDrive.", "success")
    except Exception as exc:
        logger.exception("OneDrive sync failed")
        flash(f"OneDrive sync failed: {exc}", "error")

    return redirect(url_for("office"))


@app.route("/sync-milk-flow", methods=["POST"])
@permission_required("perm_sync_dataflow")
def sync_milk_flow():
    """Manual import: look back N days and overwrite parlour data in that window."""
    farm_code = (request.form.get("farm") or request.args.get("farm") or "ALH").upper()
    shift_id = request.form.get("shift") or request.args.get("shift") or "Morning"

    try:
        days_back = int(request.form.get("days_back") or 7)
    except ValueError:
        days_back = 7
    if days_back not in IMPORT_DAY_OPTIONS:
        days_back = 7

    missing = require_azure_config()
    if missing:
        flash(f"Missing Microsoft Graph configuration: {', '.join(missing)}", "error")
        return redirect(url_for("milking_efficiency", farm=farm_code, shift=shift_id))

    # Run off the request thread so the single Gunicorn worker can still answer
    # /health while Graph download + multi-farm insert runs (minutes, not seconds).
    started, message = start_manual_sync_job(
        days_back=days_back,
        farm_code=None,
        overwrite=True,
    )
    flash(message, "info" if started else "error")

    return redirect(url_for("milking_efficiency", farm=farm_code, shift=shift_id))


@app.route("/health")
def health():
    try:
        _ensure_database()
        health_check()
        return {
            "status": "ok",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, 200
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}, 503


with app.app_context():
    _ensure_database()
    if _should_start_background_jobs():
        start_parlour_hourly_sync(app)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    reloader = _use_reloader()
    if reloader:
        logger.info("Auto-reloader enabled — Python changes apply without a manual restart")
    app.run(
        host="0.0.0.0",
        port=port,
        debug=debug,
        use_reloader=reloader,
        use_debugger=debug,
        exclude_patterns=[
            "*/data/*",
            "*/.venv/*",
            "*/__pycache__/*",
            "*/.git/*",
        ],
    )
