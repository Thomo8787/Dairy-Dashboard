"""Thomasson Farms Dashboard web application."""

import logging
import os
import secrets
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, Response, flash, jsonify, redirect, render_template, request, session, url_for

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
from services.events_common import build_dairy_semen_30d, build_events_page_report
from services.events_pages import EVENT_PAGES, _parse_date_arg, _parse_int_arg, events_template_extras
from services.births_report import build_births_report
from services.stp_report import build_stp_report
from services.breeding_sires import (
    delete_sire_classification,
    list_all_sires,
    set_sire_classification,
)
from services.database import (
    ensure_auth_ready,
    get_session,
    health_check,
    save_dataframe,
)
from services.excel_parser import parse_excel_file
from services.graph_onedrive import GraphOneDriveService
from services.herd_sync import (
    consume_herd_import_result,
    get_herd_import_status,
    herd_import_status_payload,
    start_herd_import_job,
)
from services.stock_pages import STOCK_PAGES, stock_template_extras
from services.heifer_inventory import (
    build_heifer_inventory_csv,
    build_heifer_inventory_pdf,
    get_heifer_inventory_report,
)
from services.beef_inventory import (
    build_beef_inventory_csv,
    build_beef_inventory_pdf,
    get_beef_inventory_report,
)
from services.calves_due import get_calves_due_report
from services.heifers_due import get_heifers_due_report
from services.stock_inventory_export import PDF_CONTENT_TYPE
from services.genomic_progress import (
    build_genomic_progress,
    build_genomic_scatter,
    genetics_template_extras,
    list_traits,
)
from services.farms import FARMS, HERD_FARM_OPTIONS, active_farms
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
    build_farm_summaries,
    build_metric_trend,
    build_pen_breakdown,
)
from services.stall_issues import (
    latest_milking_date,
    list_stall_issues,
    list_stall_metric_history,
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
        "herd_import_status": get_herd_import_status(),
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
        if request.endpoint and (
            request.endpoint.startswith("milking_efficiency")
            or request.endpoint.startswith("events_api")
            or request.endpoint.startswith("parlour_api")
        ):
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
    return render_template(
        "home.html",
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

    sync_status = get_manual_sync_status()
    if not sync_status.get("running"):
        finished = consume_manual_sync_result()
        if finished and finished.get("error"):
            flash(f"Last parlour import failed: {finished['error']}", "error")
        elif finished and finished.get("summary"):
            flash(finished["summary"], "success")

    return render_template(
        "milking_efficiency.html",
        shift_options=SHIFT_OPTIONS,
        import_day_options=IMPORT_DAY_OPTIONS,
        selected_farm=farm_code,
        selected_shift=shift_id,
        manual_sync_status=sync_status,
        **_page_context(active_nav="milking_efficiency"),
    )


@app.route("/parlours/milking-efficiency/summary")
def milking_efficiency_summary():
    user = current_user()
    if user is None:
        return jsonify({"error": "Authentication required."}), 401
    if not user_has_permission(user, "perm_parlours"):
        return jsonify({"error": "Permission denied."}), 403

    farm_code = (request.args.get("farm") or "ALH").upper()
    if farm_code not in {farm.code for farm in FARMS}:
        farm_code = "ALH"
    return jsonify(build_farm_summaries(farm_code=farm_code))


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


def _parlour_json_user():
    user = current_user()
    if user is None:
        return None, (jsonify({"error": "Authentication required."}), 401)
    if not user_has_permission(user, "perm_parlours"):
        return None, (jsonify({"error": "Permission denied."}), 403)
    return user, None


def _parse_iso_date_arg(name: str):
    raw = (request.args.get(name) or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return False


@app.route("/parlours/stall-issues")
@permission_required("perm_parlours")
def stall_issues():
    return render_template(
        "parlour/stall_issues.html",
        **_page_context(active_nav="stall_issues"),
    )


@app.route("/parlours/api/status")
def parlour_api_status():
    user, error = _parlour_json_user()
    if error:
        return error
    farm_code = (request.args.get("farm") or "").upper() or None
    if farm_code and farm_code not in {farm.code for farm in FARMS}:
        farm_code = None
    latest = latest_milking_date(farm_code)
    return jsonify({"latest_milking_date": latest.isoformat() if latest else None})


@app.route("/parlours/api/stall-issues")
def parlour_api_stall_issues():
    user, error = _parlour_json_user()
    if error:
        return error
    farm_code = (request.args.get("farm") or "ALH").upper()
    date_from = _parse_iso_date_arg("date_from")
    date_to = _parse_iso_date_arg("date_to")
    if date_from is False or date_to is False:
        return jsonify({"error": "Invalid date. Use YYYY-MM-DD."}), 400
    try:
        return jsonify(list_stall_issues(farm=farm_code, date_from=date_from, date_to=date_to))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/parlours/api/stall-issues/detail")
def parlour_api_stall_issues_detail():
    user, error = _parlour_json_user()
    if error:
        return error
    farm_code = (request.args.get("farm") or "ALH").upper()
    raw_point = (request.args.get("milking_point") or "").strip()
    if not raw_point:
        return jsonify({"error": "milking_point is required."}), 400
    try:
        milking_point = int(raw_point)
    except ValueError:
        milking_point = raw_point
    date_from = _parse_iso_date_arg("date_from")
    date_to = _parse_iso_date_arg("date_to")
    if date_from is False or date_to is False:
        return jsonify({"error": "Invalid date. Use YYYY-MM-DD."}), 400
    try:
        return jsonify(
            list_stall_metric_history(
                farm=farm_code,
                milking_point=milking_point,
                date_from=date_from,
                date_to=date_to,
            )
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/stock-inventory")
@permission_required("perm_stock")
def stock_inventory():
    return redirect(url_for("stock_heifer_inventory"))


def _stock_json_user():
    user = current_user()
    if user is None:
        return None, (jsonify({"error": "Authentication required."}), 401)
    if not user_has_permission(user, "perm_stock"):
        return None, (jsonify({"error": "Permission denied."}), 403)
    return user, None


def _render_stock_page(slug: str):
    status = get_herd_import_status()
    if not status.get("running"):
        finished = consume_herd_import_result()
        if finished and finished.get("error"):
            flash(f"Last herd import failed: {finished['error']}", "error")
        elif finished and finished.get("summary"):
            flash(finished["summary"], "success")

    page = STOCK_PAGES[slug]
    return render_template(
        page["template"],
        **_page_context(active_nav=page["nav"]),
        **stock_template_extras(),
    )


@app.route("/stock-inventory/heifer-inventory")
@permission_required("perm_stock")
def stock_heifer_inventory():
    return _render_stock_page("heifer-inventory")


@app.route("/stock-inventory/beef-inventory")
@permission_required("perm_stock")
def stock_beef_inventory():
    return _render_stock_page("beef-inventory")


@app.route("/stock-inventory/calves-due")
@permission_required("perm_stock")
def stock_calves_due():
    return _render_stock_page("calves-due")


@app.route("/stock-inventory/heifers-due")
@permission_required("perm_stock")
def stock_heifers_due():
    return _render_stock_page("heifers-due")


def _genetics_json_user():
    user = current_user()
    if user is None:
        return None, (jsonify({"error": "Authentication required."}), 401)
    if not user_has_permission(user, "perm_genetics"):
        return None, (jsonify({"error": "Permission denied."}), 403)
    return user, None


@app.route("/genetics")
@permission_required("perm_genetics")
def genetics():
    return redirect(url_for("genetics_genomic_progress"))


@app.route("/genetics/genomic-progress")
@permission_required("perm_genetics")
def genetics_genomic_progress():
    status = get_herd_import_status()
    if not status.get("running"):
        finished = consume_herd_import_result()
        if finished and finished.get("error"):
            flash(f"Last herd import failed: {finished['error']}", "error")
        elif finished and finished.get("summary"):
            flash(finished["summary"], "success")

    return render_template(
        "genetics/genomic_progress.html",
        **_page_context(active_nav="genetics_genomic_progress"),
        **genetics_template_extras(),
    )


@app.route("/genetics/api/genomic-progress/traits")
def genetics_api_traits():
    user, error = _genetics_json_user()
    if error:
        return error
    return jsonify({"traits": list_traits()})


@app.route("/genetics/api/genomic-progress")
def genetics_api_progress():
    user, error = _genetics_json_user()
    if error:
        return error
    trait = (request.args.get("trait") or "pli").strip()
    farms = request.args.getlist("farm") or None
    try:
        with get_session() as session:
            return jsonify(build_genomic_progress(session, trait=trait, farms=farms))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/genetics/api/genomic-scatter")
def genetics_api_scatter():
    user, error = _genetics_json_user()
    if error:
        return error
    x_trait = (request.args.get("x_trait") or "milk_kg").strip()
    y_trait = (request.args.get("y_trait") or "pli").strip()
    farms = request.args.getlist("farm") or None
    try:
        with get_session() as session:
            return jsonify(
                build_genomic_scatter(
                    session,
                    x_trait=x_trait,
                    y_trait=y_trait,
                    farms=farms,
                )
            )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


def _selected_stock_farms() -> list[str]:
    farms = request.args.getlist("farm")
    return [f for f in farms if f in HERD_FARM_OPTIONS]


@app.route("/stock-inventory/api/heifer-inventory")
def stock_api_heifer_inventory():
    user, error = _stock_json_user()
    if error:
        return error
    with get_session() as session:
        return jsonify(
            get_heifer_inventory_report(
                session,
                farms=_selected_stock_farms() or None,
                min_age=_parse_int_arg("min_age"),
                max_age=_parse_int_arg("max_age"),
            )
        )


@app.route("/stock-inventory/api/heifer-inventory/export.csv")
def stock_api_heifer_inventory_csv():
    user, error = _stock_json_user()
    if error:
        return error
    farms = _selected_stock_farms()
    with get_session() as session:
        report = get_heifer_inventory_report(
            session,
            farms=farms or None,
            min_age=_parse_int_arg("min_age"),
            max_age=_parse_int_arg("max_age"),
        )
    content = build_heifer_inventory_csv(report, farms or list(HERD_FARM_OPTIONS))
    return Response(
        content,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="heifer_inventory.csv"'},
    )


@app.route("/stock-inventory/api/heifer-inventory/export.pdf")
def stock_api_heifer_inventory_pdf():
    user, error = _stock_json_user()
    if error:
        return error
    farms = _selected_stock_farms()
    with get_session() as session:
        report = get_heifer_inventory_report(
            session,
            farms=farms or None,
            min_age=_parse_int_arg("min_age"),
            max_age=_parse_int_arg("max_age"),
        )
    content = build_heifer_inventory_pdf(report, farms or list(HERD_FARM_OPTIONS))
    return Response(
        content,
        mimetype=PDF_CONTENT_TYPE,
        headers={"Content-Disposition": 'attachment; filename="heifer_inventory.pdf"'},
    )


@app.route("/stock-inventory/api/beef-inventory")
def stock_api_beef_inventory():
    user, error = _stock_json_user()
    if error:
        return error
    try:
        with get_session() as session:
            return jsonify(
                get_beef_inventory_report(
                    session,
                    farms=_selected_stock_farms() or None,
                    min_age=_parse_int_arg("min_age"),
                    max_age=_parse_int_arg("max_age"),
                    jv_mode=request.args.get("jv_mode") or "all",
                )
            )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/stock-inventory/api/beef-inventory/export.csv")
def stock_api_beef_inventory_csv():
    user, error = _stock_json_user()
    if error:
        return error
    farms = _selected_stock_farms()
    try:
        with get_session() as session:
            report = get_beef_inventory_report(
                session,
                farms=farms or None,
                min_age=_parse_int_arg("min_age"),
                max_age=_parse_int_arg("max_age"),
                jv_mode=request.args.get("jv_mode") or "all",
            )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    content = build_beef_inventory_csv(report, farms or list(HERD_FARM_OPTIONS))
    return Response(
        content,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="beef_inventory.csv"'},
    )


@app.route("/stock-inventory/api/beef-inventory/export.pdf")
def stock_api_beef_inventory_pdf():
    user, error = _stock_json_user()
    if error:
        return error
    farms = _selected_stock_farms()
    try:
        with get_session() as session:
            report = get_beef_inventory_report(
                session,
                farms=farms or None,
                min_age=_parse_int_arg("min_age"),
                max_age=_parse_int_arg("max_age"),
                jv_mode=request.args.get("jv_mode") or "all",
            )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    content = build_beef_inventory_pdf(report, farms or list(HERD_FARM_OPTIONS))
    return Response(
        content,
        mimetype=PDF_CONTENT_TYPE,
        headers={"Content-Disposition": 'attachment; filename="beef_inventory.pdf"'},
    )


@app.route("/stock-inventory/api/calves-due")
def stock_api_calves_due():
    user, error = _stock_json_user()
    if error:
        return error
    with get_session() as session:
        return jsonify(
            get_calves_due_report(
                session,
                farms=_selected_stock_farms() or None,
                breeds=request.args.getlist("breed") or None,
                due_from=_parse_date_arg("due_from"),
                due_to=_parse_date_arg("due_to"),
            )
        )


@app.route("/stock-inventory/api/heifers-due")
def stock_api_heifers_due():
    user, error = _stock_json_user()
    if error:
        return error
    with get_session() as session:
        return jsonify(
            get_heifers_due_report(
                session,
                farms=_selected_stock_farms() or None,
                due_from=_parse_date_arg("due_from"),
                due_to=_parse_date_arg("due_to"),
            )
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
    """Manual import: same incremental sync as the cron, with a chosen lookback."""
    farm_code = (request.form.get("farm") or request.args.get("farm") or "ALH").upper()
    shift_id = request.form.get("shift") or request.args.get("shift") or "Morning"

    try:
        days_back = int(request.form.get("days_back") or 2)
    except ValueError:
        days_back = 2
    if days_back not in IMPORT_DAY_OPTIONS:
        days_back = 2

    missing = require_azure_config()
    if missing:
        flash(f"Missing Microsoft Graph configuration: {', '.join(missing)}", "error")
        return redirect(url_for("milking_efficiency", farm=farm_code, shift=shift_id))

    # Same mode as the hourly cron (overwrite=False); only days_back differs.
    started, message = start_manual_sync_job(
        days_back=days_back,
        farm_code=None,
        overwrite=False,
    )
    flash(message, "info" if started else "error")

    return redirect(url_for("milking_efficiency", farm=farm_code, shift=shift_id))


def _events_json_user():
    user = current_user()
    if user is None:
        return None, (jsonify({"error": "Authentication required."}), 401)
    if not user_has_permission(user, "perm_events"):
        return None, (jsonify({"error": "Permission denied."}), 403)
    return user, None


def _render_events_page(slug: str):
    user = current_user()
    status = get_herd_import_status()
    if not status.get("running"):
        finished = consume_herd_import_result()
        if finished and finished.get("error"):
            flash(f"Last herd import failed: {finished['error']}", "error")
        elif finished and finished.get("summary"):
            flash(finished["summary"], "success")

    page = EVENT_PAGES[slug]
    extras = events_template_extras(slug, is_admin=bool(user and user.is_admin))
    return render_template(
        page["template"],
        **_page_context(active_nav=page["nav"]),
        **extras,
    )


@app.route("/events")
@permission_required("perm_events")
def events():
    return redirect(url_for("events_calvings"))


@app.route("/events/calvings")
@permission_required("perm_events")
def events_calvings():
    return _render_events_page("calvings")


@app.route("/events/births")
@permission_required("perm_events")
def events_births():
    return _render_events_page("births")


@app.route("/events/sales")
@permission_required("perm_events")
def events_sales():
    return _render_events_page("sales")


@app.route("/events/deaths")
@permission_required("perm_events")
def events_deaths():
    return _render_events_page("deaths")


@app.route("/events/disease")
@permission_required("perm_events")
def events_disease():
    return _render_events_page("disease")


@app.route("/events/hooftrimming")
@permission_required("perm_events")
def events_hooftrimming():
    return _render_events_page("hooftrimming")


@app.route("/events/breedings")
@permission_required("perm_events")
def events_breedings():
    return _render_events_page("breedings")


@app.route("/events/total-protein")
@permission_required("perm_events")
def events_total_protein():
    return _render_events_page("total-protein")


@app.route("/api/events/dairy-semen-30d")
def events_api_dairy_semen_30d():
    user, error = _events_json_user()
    if error:
        return error
    with get_session() as session:
        return jsonify(build_dairy_semen_30d(session))


@app.route("/events/api/<slug>")
def events_api_report(slug: str):
    user, error = _events_json_user()
    if error:
        return error
    if slug not in EVENT_PAGES:
        return jsonify({"error": "Unknown events page."}), 404

    farms = request.args.getlist("farm") or None
    lact = request.args.getlist("lact") or None
    parity = request.args.getlist("parity") or None
    semen = request.args.getlist("semen") or None
    protocol = request.args.getlist("protocol") or None
    category = request.args.getlist("category") or None
    breed = request.args.getlist("breed") or None
    fiscal_year = _parse_int_arg("fiscal_year")
    event_from = _parse_date_arg("event_from")
    event_to = _parse_date_arg("event_to")
    disease = request.args.get("disease") or None
    y_min = _parse_int_arg("y_min")
    y_max = _parse_int_arg("y_max")

    with get_session() as session:
        if slug == "births":
            payload = build_births_report(
                session,
                farms=farms,
                categories=category,
                event_from=event_from,
                event_to=event_to,
                fiscal_year=fiscal_year,
            )
        elif slug == "total-protein":
            payload = build_stp_report(
                session,
                farms=farms,
                breed_types=breed,
                birth_from=_parse_date_arg("birth_from"),
                birth_to=_parse_date_arg("birth_to"),
            )
        else:
            payload = build_events_page_report(
                session,
                page_slug=slug,
                farms=farms,
                event_from=event_from,
                event_to=event_to,
                lact_groups=lact,
                parity_groups=parity,
                fiscal_year=fiscal_year,
                disease=disease,
                semen_types=semen,
                lame_protocols=protocol,
                y_min=y_min,
                y_max=y_max,
            )
    return jsonify(payload)


@app.route("/events/api/breedings/sires")
def events_api_breedings_sires():
    user, error = _events_json_user()
    if error:
        return error
    with get_session() as session:
        return jsonify(list_all_sires(session))


@app.route("/events/api/breedings/sires/<sire_code>", methods=["PUT"])
def events_api_set_sire(sire_code: str):
    user, error = _events_json_user()
    if error:
        return error
    if not user.is_admin:
        return jsonify({"error": "Admin access required."}), 403
    body = request.get_json(silent=True) or {}
    semen_type = str(body.get("semen_type") or "")
    try:
        with get_session() as session:
            row = set_sire_classification(session, sire_code, semen_type)
            return jsonify(row.to_dict())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/events/api/breedings/sires/<sire_code>", methods=["DELETE"])
def events_api_delete_sire(sire_code: str):
    user, error = _events_json_user()
    if error:
        return error
    if not user.is_admin:
        return jsonify({"error": "Admin access required."}), 403
    with get_session() as session:
        if not delete_sire_classification(session, sire_code):
            return jsonify({"error": "Sire classification not found."}), 404
    return jsonify({"ok": True})


@app.route("/events/api/herd-import-status")
def events_herd_import_status():
    user = current_user()
    if user is None:
        return jsonify({"error": "Authentication required."}), 401
    if not (
        user_has_permission(user, "perm_events")
        or user_has_permission(user, "perm_stock")
        or user_has_permission(user, "perm_genetics")
    ):
        return jsonify({"error": "Permission denied."}), 403
    return jsonify(herd_import_status_payload())


@app.route("/sync-herd-exports", methods=["POST"])
@permission_required("perm_sync_onedrive")
def sync_herd_exports():
    next_path = request.form.get("next") or url_for("events_calvings")
    if not (
        next_path.startswith("/events")
        or next_path.startswith("/stock-inventory")
        or next_path.startswith("/genetics")
    ):
        next_path = url_for("events_calvings")
    started, message = start_herd_import_job(force=False)
    flash(message, "info" if started else "error")
    return redirect(next_path)


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
