"""Shared Microsoft Graph helpers — delegated (signed-in user) auth."""

import os
from datetime import datetime, timedelta, timezone

import msal
import requests

from services.database import get_session

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
EXCEL_EXTENSIONS = {".xlsx", ".xls", ".xlsm"}

# Delegated scopes — normally do NOT need admin consent for your own mailbox/OneDrive.
DELEGATED_SCOPES = [
    "User.Read",
    "Mail.Read",
    "Files.Read",
    "offline_access",
]


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
    """Persist tokens and return the signed-in user's email/UPN."""
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
    from services.database import GraphToken

    with get_session() as session:
        row = session.query(GraphToken).filter_by(account_key="default").one_or_none()
        if not row:
            return None
        return {
            "user_email": row.user_email,
            "expires_at": row.expires_at,
            "has_refresh_token": bool(row.refresh_token),
        }


def get_access_token() -> str:
    """Return a valid delegated access token, refreshing if needed."""
    from services.database import GraphToken

    with get_session() as session:
        row = session.query(GraphToken).filter_by(account_key="default").one_or_none()
        if not row or not row.access_token:
            raise RuntimeError(
                "Microsoft 365 is not connected. Click Connect Microsoft 365 and sign in as mark@alhfarm.co.uk."
            )

        now = datetime.now(timezone.utc)
        expires_at = row.expires_at
        if expires_at and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        if expires_at and expires_at > now + timedelta(seconds=120):
            return row.access_token

        if not row.refresh_token:
            raise RuntimeError(
                "Microsoft session expired. Click Connect Microsoft 365 and sign in again."
            )

        result = _msal_app().acquire_token_by_refresh_token(
            row.refresh_token,
            scopes=DELEGATED_SCOPES,
        )
        if "access_token" not in result:
            error = result.get("error_description") or result.get("error") or "Refresh failed"
            raise RuntimeError(f"Microsoft token refresh failed: {error}. Reconnect Microsoft 365.")

        expires_in = int(result.get("expires_in") or 3600)
        row.access_token = result["access_token"]
        if result.get("refresh_token"):
            row.refresh_token = result["refresh_token"]
        row.expires_at = now + timedelta(seconds=max(expires_in - 60, 60))
        return row.access_token


def graph_headers() -> dict:
    return {"Authorization": f"Bearer {get_access_token()}"}


def graph_get(url: str, timeout: int = 60) -> dict:
    response = requests.get(url, headers=graph_headers(), timeout=timeout)
    response.raise_for_status()
    return response.json()


def graph_get_bytes(url: str, timeout: int = 120) -> bytes:
    response = requests.get(url, headers=graph_headers(), timeout=timeout)
    response.raise_for_status()
    return response.content
