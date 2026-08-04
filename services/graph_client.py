"""Shared Microsoft Graph authentication helpers."""

import os

import msal
import requests

GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
EXCEL_EXTENSIONS = {".xlsx", ".xls", ".xlsm"}


def require_azure_config() -> list[str]:
    required = ["AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID"]
    return [key for key in required if not os.environ.get(key)]


def get_access_token() -> str:
    app = msal.ConfidentialClientApplication(
        os.environ["AZURE_CLIENT_ID"],
        authority=f"https://login.microsoftonline.com/{os.environ['AZURE_TENANT_ID']}",
        client_credential=os.environ["AZURE_CLIENT_SECRET"],
    )
    result = app.acquire_token_for_client(scopes=GRAPH_SCOPE)
    if "access_token" not in result:
        error = result.get("error_description") or result.get("error") or "Unknown auth error"
        raise RuntimeError(f"Microsoft Graph authentication failed: {error}")
    return result["access_token"]


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
