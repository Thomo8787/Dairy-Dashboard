"""Thomasson Farms Dashboard web application."""

import logging
import os
import secrets
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, session, url_for

from services.database import (
    get_dashboard_summary,
    get_recent_records,
    health_check,
    init_db,
    save_dataframe,
    save_milk_flow_records,
    save_rotary_entry_id_records,
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
from services.graph_email import GraphEmailService
from services.graph_onedrive import GraphOneDriveService
from services.milking_efficiency_summary import SHIFT_OPTIONS, build_seven_day_summary
from services.milk_flow_email import MilkFlowEmailService
from services.milk_flow_parser import parse_milk_flow_csv
from services.navigation import NAV_ITEMS, parent_nav_id
from services.rotary_entry_email import RotaryEntryEmailService
from services.rotary_entry_parser import parse_rotary_entry_id_csv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")

# Local/dev: pick up HTML/CSS changes on browser refresh without restarting.
# Python (.py) changes still need a restart (or debug reloader).
_running_local = not bool(os.environ.get("RENDER"))
if _running_local or os.environ.get("FLASK_DEBUG", "").lower() == "true":
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
    app.jinja_env.auto_reload = True


def _ensure_database():
    if getattr(app, "_db_initialized", False):
        return
    try:
        init_db()
        app._db_initialized = True
        logger.info("Database tables ready")
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

    ctx = {
        "nav_items": NAV_ITEMS,
        "active_nav": active_nav,
        "active_parent_nav": parent_nav_id(active_nav),
        "farms": FARMS,
        "focus_farms": active_farms(),
        "connected": connected,
        "auth_mode": auth_mode(),
        "redirect_uri": get_redirect_uri(),
    }
    ctx.update(extra)
    return ctx


@app.before_request
def ensure_database():
    _ensure_database()


@app.context_processor
def inject_globals():
    return {"app_name": "Thomasson Farms Dashboard"}


@app.route("/")
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
def office():
    return render_template(
        "office.html",
        **_page_context(active_nav="office"),
    )


@app.route("/parlours")
def parlours():
    return render_template(
        "parlours.html",
        **_page_context(active_nav="parlours"),
    )


@app.route("/parlours/milking-efficiency")
def milking_efficiency():
    farm_code = (request.args.get("farm") or "ALH").upper()
    if farm_code not in {farm.code for farm in FARMS}:
        farm_code = "ALH"
    shift_id = request.args.get("shift") or "Morning"
    if shift_id not in {item["id"] for item in SHIFT_OPTIONS}:
        shift_id = "Morning"

    summary = build_seven_day_summary(farm_code=farm_code, shift_id=shift_id)

    return render_template(
        "milking_efficiency.html",
        summary=summary,
        shift_options=SHIFT_OPTIONS,
        selected_farm=farm_code,
        selected_shift=shift_id,
        **_page_context(active_nav="milking_efficiency"),
    )


@app.route("/stock-inventory")
def stock_inventory():
    return render_template(
        "stock_inventory.html",
        **_page_context(active_nav="stock_inventory"),
    )


# Keep old URL working
@app.route("/dashboard")
def dashboard():
    return redirect(url_for("home"))


@app.route("/auth/login")
def auth_login():
    missing = require_azure_config()
    if missing:
        flash(f"Missing Microsoft Graph configuration: {', '.join(missing)}", "error")
        return redirect(url_for("office"))

    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    return redirect(build_auth_url(state))


@app.route("/auth/callback")
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
def auth_logout():
    try:
        clear_saved_token()
        flash("Disconnected Microsoft 365.", "info")
    except Exception as exc:
        flash(f"Disconnect failed: {exc}", "error")
    return redirect(url_for("office"))


@app.route("/sync", methods=["POST"])
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
def sync_milk_flow():
    """Import Milk Flow + Rotary Entry ID CSVs from @dataflow2.com and stamp as ALH."""
    farm_code = (request.form.get("farm") or request.args.get("farm") or "ALH").upper()
    shift_id = request.form.get("shift") or request.args.get("shift") or "Morning"

    missing = require_azure_config()
    if missing:
        flash(f"Missing Microsoft Graph configuration: {', '.join(missing)}", "error")
        return redirect(url_for("milking_efficiency", farm=farm_code, shift=shift_id))

    try:
        milk_downloads = MilkFlowEmailService().fetch_milk_flow_csvs(top=40)
        entry_downloads = RotaryEntryEmailService().fetch_rotary_entry_csvs(top=40)

        if not milk_downloads and not entry_downloads:
            flash("No Milk Flow or Rotary Entry ID CSVs found from @dataflow2.com.", "info")
            return redirect(url_for("milking_efficiency", farm=farm_code, shift=shift_id))

        milk_files = 0
        milk_rows = 0
        for item in milk_downloads:
            records = parse_milk_flow_csv(item["file_path"], farm_code="ALH")
            if not records:
                continue
            _, inserted = save_milk_flow_records(
                {
                    "farm_code": "ALH",
                    "report_type": "milk_flow",
                    "filename": item["filename"],
                    "email_subject": item["email_subject"],
                    "email_from": item["email_from"],
                    "email_received_at": item["email_received_at"],
                    "message_id": item["message_id"],
                },
                records,
            )
            milk_files += 1
            milk_rows += inserted

        entry_files = 0
        entry_rows = 0
        for item in entry_downloads:
            records = parse_rotary_entry_id_csv(item["file_path"], farm_code="ALH")
            if not records:
                continue
            _, inserted = save_rotary_entry_id_records(
                {
                    "farm_code": "ALH",
                    "report_type": "rotary_entry_id",
                    "filename": item["filename"],
                    "email_subject": item["email_subject"],
                    "email_from": item["email_from"],
                    "email_received_at": item["email_received_at"],
                    "message_id": item["message_id"],
                },
                records,
            )
            entry_files += 1
            entry_rows += inserted

        flash(
            f"ALH import — Milk Flow: {milk_files} file(s), {milk_rows} new rows; "
            f"Rotary Entry ID: {entry_files} file(s), {entry_rows} new rows.",
            "success",
        )
    except Exception as exc:
        logger.exception("Parlour report sync failed")
        flash(f"Parlour report sync failed: {exc}", "error")

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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
