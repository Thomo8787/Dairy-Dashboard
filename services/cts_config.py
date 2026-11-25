"""BCMS CTS / DDTS credentials (env-driven, per farm)."""

from __future__ import annotations

import os

from services.farms import HERD_FARM_OPTIONS

CTS_DDTS_USERNAME = os.getenv("CTS_DDTS_USERNAME", "").strip()
CTS_DDTS_PASSWORD = os.getenv("CTS_DDTS_PASSWORD", "").strip()


def cts_ddts_is_configured() -> bool:
    return bool(CTS_DDTS_USERNAME and CTS_DDTS_PASSWORD)


def _farm_env(farm: str, suffix: str) -> str:
    key = (farm or "").strip().upper()
    return os.getenv(f"CTS_{suffix}_{key}", "").strip()


def cts_farm_credential_parts(farm: str) -> dict[str, bool] | None:
    """Which CTWS/holding pieces are set for a farm (booleans only; no secrets)."""
    key = (farm or "").strip().upper()
    if key not in HERD_FARM_OPTIONS:
        return None
    return {
        "username": bool(_farm_env(key, "CTWS_USERNAME")),
        "password": bool(_farm_env(key, "CTWS_PASSWORD")),
        "holding": bool(_farm_env(key, "HOLDING")),
    }


def cts_farm_credentials(farm: str) -> dict[str, str] | None:
    """Return CTWS + holding config for a farm, or None if incomplete."""
    key = (farm or "").strip().upper()
    parts = cts_farm_credential_parts(key)
    if parts is None or not all(parts.values()):
        return None
    return {
        "username": _farm_env(key, "CTWS_USERNAME"),
        "password": _farm_env(key, "CTWS_PASSWORD"),
        "holding": _farm_env(key, "HOLDING"),
    }


def cts_ready_farms() -> list[str]:
    """Farms with complete CTWS + holding config."""
    return [farm for farm in HERD_FARM_OPTIONS if cts_farm_credentials(farm) is not None]
