"""Shared Microsoft Graph helpers.

Supports two modes (GRAPH_AUTH_MODE):
- application (default): client credentials — needs admin consent for Mail.Read + Files.Read.All
- delegated: user signs in once — for testing without admin consent
"""

import logging
import os
import time
from datetime import datetime, timedelta, timezone

import msal
import requests

from services.database import get_session

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
EXCEL_EXTENSIONS = {".xlsx", ".xls", ".xlsm"}
APP_SCOPE = ["https://graph.microsoft.com/.default"]

DELEGATED_SCOPES = [
    "User.Read",
    "Mail.Read",
    "Files.Read",
    "offline_access",
]


def auth_mode() -> str:
    mode = os.environ.get("GRAPH_AUTH_MODE", "application").strip().lower()
    return mode if mode in {"application", "delegated"} else "application"


def require_azure_config() -> list[str]:
    required = ["AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID"]
    return [key for key in required if not os.environ.get(key)]


def get_redirect_uri() -> str:
    explicit = os.environ.get("AZURE_REDIRECT_URI", "").strip()
    if explicit:
        return explicit
    base = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if base:
        return f"{base}/auth/callback"
    return "http://localhost:5000/auth/callback"


def _msal_app() -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        os.environ["AZURE_CLIENT_ID"],
        authority=f"https://login.microsoftonline.com/{os.environ['AZURE_TENANT_ID']}",
        client_credential=os.environ["AZURE_CLIENT_SECRET"],
    )


def build_auth_url(state: str) -> str:
    return _msal_app().get_authorization_request_url(
        scopes=DELEGATED_SCOPES,
        state=state,
        redirect_uri=get_redirect_uri(),
        prompt="select_account",
    )


def exchange_code_for_token(code: str) -> dict:
    result = _msal_app().acquire_token_by_authorization_code(
        code,
        scopes=DELEGATED_SCOPES,
        redirect_uri=get_redirect_uri(),
    )
    if "access_token" not in result:
        error = result.get("error_description") or result.get("error") or "Unknown auth error"
        raise RuntimeError(f"Microsoft sign-in failed: {error}")
    return result


def save_token_result(result: dict, account_hint: str | None = None) -> str:
    from services.database import GraphToken

    claims = result.get("id_token_claims") or {}
    user_email = (
        account_hint
        or claims.get("preferred_username")
        or claims.get("email")
        or claims.get("upn")
        or "unknown"
    )
    expires_in = int(result.get("expires_in") or 3600)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(expires_in - 60, 60))

    with get_session() as session:
        existing = session.query(GraphToken).filter_by(account_key="default").one_or_none()
        if existing is None:
            existing = GraphToken(account_key="default")
            session.add(existing)

        existing.user_email = user_email
        existing.access_token = result["access_token"]
        if result.get("refresh_token"):
            existing.refresh_token = result["refresh_token"]
        existing.expires_at = expires_at
        existing.scopes = " ".join(result.get("scope", "").split()) if result.get("scope") else " ".join(DELEGATED_SCOPES)

    return user_email


def clear_saved_token() -> None:
    from services.database import GraphToken

    with get_session() as session:
        existing = session.query(GraphToken).filter_by(account_key="default").one_or_none()
        if existing:
            session.delete(existing)


def get_connected_account() -> dict | None:
    if auth_mode() == "application":
        return {
            "user_email": os.environ.get("ONEDRIVE_USER") or os.environ.get("OUTLOOK_MAILBOX") or "app-only",
            "mode": "application",
            "expires_at": None,
            "has_refresh_token": False,
        }

    from services.database import GraphToken

    with get_session() as session:
        row = session.query(GraphToken).filter_by(account_key="default").one_or_none()
        if not row:
            return None
        return {
            "user_email": row.user_email,
            "mode": "delegated",
            "expires_at": row.expires_at,
            "has_refresh_token": bool(row.refresh_token),
        }


def _get_application_token() -> str:
    result = _msal_app().acquire_token_for_client(scopes=APP_SCOPE)
    if "access_token" not in result:
        error = result.get("error_description") or result.get("error") or "Unknown auth error"
        raise RuntimeError(f"Microsoft Graph app authentication failed: {error}")
    return result["access_token"]


def _get_delegated_token() -> str:
    from services.database import GraphToken

    with get_session() as session:
        row = session.query(GraphToken).filter_by(account_key="default").one_or_none()
        if not row or not row.access_token:
            raise RuntimeError(
                "Microsoft 365 is not connected. Click Connect Microsoft 365, or set GRAPH_AUTH_MODE=application after admin consent."
            )

        now = datetime.now(timezone.utc)
        expires_at = row.expires_at
        if expires_at and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        if expires_at and expires_at > now + timedelta(seconds=120):
            return row.access_token

        if not row.refresh_token:
            raise RuntimeError("Microsoft session expired. Click Connect Microsoft 365 again.")

        result = _msal_app().acquire_token_by_refresh_token(row.refresh_token, scopes=DELEGATED_SCOPES)
        if "access_token" not in result:
            error = result.get("error_description") or result.get("error") or "Refresh failed"
            raise RuntimeError(f"Microsoft token refresh failed: {error}")

        expires_in = int(result.get("expires_in") or 3600)
        row.access_token = result["access_token"]
        if result.get("refresh_token"):
            row.refresh_token = result["refresh_token"]
        row.expires_at = now + timedelta(seconds=max(expires_in - 60, 60))
        return row.access_token


def get_access_token() -> str:
    if auth_mode() == "application":
        return _get_application_token()
    return _get_delegated_token()


def graph_headers() -> dict:
    return {"Authorization": f"Bearer {get_access_token()}"}


def _request_with_retries(
    method: str,
    url: str,
    *,
    timeout: int,
    headers: dict | None = None,
    max_attempts: int = 6,
) -> requests.Response:
    """GET/POST with backoff on Graph throttling (429) and transient 5xx."""
    hdrs = headers or graph_headers()
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        response = requests.request(method, url, headers=hdrs, timeout=timeout)
        if response.status_code == 429 or response.status_code >= 500:
            retry_after = response.headers.get("Retry-After")
            try:
                wait_s = float(retry_after) if retry_after else min(2 ** attempt, 30)
            except ValueError:
                wait_s = min(2 ** attempt, 30)
            last_error = requests.HTTPError(
                f"{response.status_code} for url: {url}",
                response=response,
            )
            if attempt >= max_attempts:
                response.raise_for_status()
            # Slight jitter so parallel workers don't retry in lockstep.
            wait_s = wait_s + (attempt * 0.15)
            logger.warning(
                "Graph %s — retry %s/%s after %.1fs (%s)",
                response.status_code,
                attempt,
                max_attempts,
                wait_s,
                url.split("?")[0][-80:],
            )
            time.sleep(wait_s)
            # Refresh token header in case the wait crossed expiry.
            hdrs = headers or graph_headers()
            continue
        response.raise_for_status()
        return response

    assert last_error is not None
    raise last_error


def graph_get(url: str, timeout: int = 60) -> dict:
    response = _request_with_retries("GET", url, timeout=timeout)
    return response.json()


def graph_get_bytes(url: str, timeout: int = 120) -> bytes:
    response = _request_with_retries("GET", url, timeout=timeout)
    return response.content
