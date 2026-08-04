"""Dairy dashboard web application."""

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
)
from services.excel_parser import parse_excel_file
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

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")


def _ensure_database():
    """Create tables once when DATABASE_URL is available."""
    if getattr(app, "_db_initialized", False):
        return
    if not os.environ.get("DATABASE_URL"):
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


@app.before_request
def ensure_database():
    _ensure_database()


@app.route("/")
def dashboard():
    summary = get_dashboard_summary()
    records = get_recent_records(limit=100)
    connected = None
    try:
        connected = get_connected_account()
    except Exception:
        logger.exception("Could not load Microsoft connection status")

    return render_template(
        "dashboard.html",
        summary=summary,
        records=records,
        connected=connected,
        auth_mode=auth_mode(),
        redirect_uri=get_redirect_uri(),
    )


@app.route("/auth/login")
def auth_login():
    missing = require_azure_config()
    if missing:
        flash(f"Missing Microsoft Graph configuration: {', '.join(missing)}", "error")
        return redirect(url_for("dashboard"))

    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    return redirect(build_auth_url(state))


@app.route("/auth/callback")
def auth_callback():
    error = request.args.get("error")
    if error:
        flash(f"Microsoft sign-in error: {error} — {request.args.get('error_description', '')}", "error")
        return redirect(url_for("dashboard"))

    state = request.args.get("state")
    if not state or state != session.get("oauth_state"):
        flash("Microsoft sign-in failed: invalid state. Try Connect Microsoft 365 again.", "error")
        return redirect(url_for("dashboard"))

    code = request.args.get("code")
    if not code:
        flash("Microsoft sign-in failed: no authorization code returned.", "error")
        return redirect(url_for("dashboard"))

    try:
        result = exchange_code_for_token(code)
        user_email = save_token_result(result)
        session.pop("oauth_state", None)
        flash(f"Connected as {user_email}. You can sync Outlook and OneDrive.", "success")
    except Exception as exc:
        logger.exception("OAuth callback failed")
        flash(f"Microsoft sign-in failed: {exc}", "error")

    return redirect(url_for("dashboard"))


@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    try:
        clear_saved_token()
        flash("Disconnected Microsoft 365.", "info")
    except Exception as exc:
        flash(f"Disconnect failed: {exc}", "error")
    return redirect(url_for("dashboard"))


@app.route("/sync", methods=["POST"])
def sync_from_outlook():
    missing = require_azure_config()
    if missing:
        flash(f"Missing Microsoft Graph configuration: {', '.join(missing)}", "error")
        return redirect(url_for("dashboard"))

    if auth_mode() == "delegated" and not get_connected_account():
        flash("Connect Microsoft 365 first, then sync Outlook.", "error")
        return redirect(url_for("dashboard"))

    try:
        attachments = GraphEmailService().fetch_excel_attachments()
        if not attachments:
            flash("No matching Excel attachments found in Outlook.", "info")
            return redirect(url_for("dashboard"))

        imported = _import_items(attachments)
        flash(f"Imported {imported} Excel file(s) from Outlook.", "success")
    except Exception as exc:
        logger.exception("Outlook sync failed")
        flash(f"Outlook sync failed: {exc}", "error")

    return redirect(url_for("dashboard"))


@app.route("/sync-onedrive", methods=["POST"])
def sync_from_onedrive():
    missing = require_azure_config()
    if missing:
        flash(f"Missing Microsoft Graph configuration: {', '.join(missing)}", "error")
        return redirect(url_for("dashboard"))

    if auth_mode() == "delegated" and not get_connected_account():
        flash("Connect Microsoft 365 first, then sync OneDrive.", "error")
        return redirect(url_for("dashboard"))

    try:
        files = GraphOneDriveService().fetch_excel_files()
        if not files:
            flash("No Excel files found in the configured OneDrive folder.", "info")
            return redirect(url_for("dashboard"))

        imported = _import_items(files)
        flash(f"Imported {imported} Excel file(s) from OneDrive.", "success")
    except Exception as exc:
        logger.exception("OneDrive sync failed")
        flash(f"OneDrive sync failed: {exc}", "error")

    return redirect(url_for("dashboard"))


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


# Warm the DB connection when the worker process starts (gunicorn).
with app.app_context():
    _ensure_database()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
