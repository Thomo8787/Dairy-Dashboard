"""SenseHub (SCR Allflex) credentials — same login as https://st.scrdairy.com."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


SENSEHUB_USERNAME = _env("SENSEHUB_USERNAME").strip()
SENSEHUB_PASSWORD = _env("SENSEHUB_PASSWORD")
SENSEHUB_FARM_ID = _env("SENSEHUB_FARM_ID").strip()
SENSEHUB_REGION = _env("SENSEHUB_REGION", "IL01").strip() or "IL01"
SENSEHUB_LOGIN_BASE = _env("SENSEHUB_LOGIN_BASE", "https://st.scrdairy.com").strip().rstrip("/")
SENSEHUB_PROXY_BASE = _env(
    "SENSEHUB_PROXY_BASE", "https://rp.scrdairy.com/ReverseProxy"
).strip().rstrip("/")
SENSEHUB_DOMAIN_RESOLVER_URL = _env(
    "SENSEHUB_DOMAIN_RESOLVER_URL",
    "https://shc-common-services-api.scrdairy.com/ShcCommonServices/api/v1/domain/demo",
).strip()


def sensehub_is_configured() -> bool:
    return bool(SENSEHUB_USERNAME and SENSEHUB_PASSWORD and SENSEHUB_FARM_ID)
