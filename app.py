"""Dairy dashboard web application."""

import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, url_for

from services.database import (
    get_dashboard_summary,
    get_recent_records,
    health_check,
    init_db,
    save_dataframe,
)
from services.excel_parser import parse_excel_file
from services.graph_email import GraphEmailService

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


@app.before_request
def ensure_database():
    _ensure_database()


@app.route("/")
def dashboard():
    summary = get_dashboard_summary()
    records = get_recent_records(limit=100)
    return render_template("dashboard.html", summary=summary, records=records)


@app.route("/sync", methods=["POST"])
def sync_from_outlook():
    required = ["AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID"]
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        flash(f"Missing Microsoft Graph configuration: {', '.join(missing)}", "error")
        return redirect(url_for("dashboard"))

    try:
        email_service = GraphEmailService()
        attachments = email_service.fetch_excel_attachments()

        if not attachments:
            flash("No matching Excel attachments found in Outlook.", "info")
            return redirect(url_for("dashboard"))

        imported = 0
        for item in attachments:
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

        flash(f"Imported {imported} Excel file(s) from Outlook.", "success")
    except Exception as exc:
        logger.exception("Outlook sync failed")
        flash(f"Sync failed: {exc}", "error")

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
