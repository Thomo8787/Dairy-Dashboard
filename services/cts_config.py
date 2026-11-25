"""BCMS CTS / DDTS credentials (env-driven, per farm).

ALH and BNK share one CTS holding / CTWS login, so BCMS pages expose a single
``ALH/BNK`` farm tab (value ``ALH``). Inventory and events for reconcile and
pending movements are unioned across both DairyComp herds.
"""

from __future__ import annotations

import os

from services.farms import HERD_FARM_OPTIONS

CTS_DDTS_USERNAME = os.getenv("CTS_DDTS_USERNAME", "").strip()
CTS_DDTS_PASSWORD = os.getenv("CTS_DDTS_PASSWORD", "").strip()

# One CTS holding covers both DairyComp herds (same pattern as SHARED_HERD_FARMS).
CTS_SHARED_HOLDING_FARMS: frozenset[str] = frozenset({"ALH", "BNK"})
CTS_SHARED_HOLDING_PRIMARY = "ALH"
CTS_SHARED_HOLDING_LABEL = "ALH/BNK"


def cts_ddts_is_configured() -> bool:
    return bool(CTS_DDTS_USERNAME and CTS_DDTS_PASSWORD)


def _farm_env(farm: str, suffix: str) -> str:
    key = (farm or "").strip().upper()
    return os.getenv(f"CTS_{suffix}_{key}", "").strip()


def cts_normalize_farm_key(farm: str) -> str:
    """Map a UI/API farm key onto the CTS logical farm (ALH for shared holding)."""
    key = (farm or "").strip().upper().replace(" ", "")
    key = key.replace("+", "/").replace("_", "/")
    if key in {"ALH/BNK", "ALHBNK"} or key in CTS_SHARED_HOLDING_FARMS:
        return CTS_SHARED_HOLDING_PRIMARY
    return key


def cts_herd_farms_for(farm: str) -> tuple[str, ...]:
    """DairyComp herd codes covered by a CTS farm selection."""
    key = cts_normalize_farm_key(farm)
    if key == CTS_SHARED_HOLDING_PRIMARY:
        return tuple(sorted(CTS_SHARED_HOLDING_FARMS))
    return (key,)


def cts_farm_ui_options() -> list[dict[str, str]]:
    """Ordered CTS farm slicer options (shared ALH/BNK once, then other herds)."""
    options: list[dict[str, str]] = []
    shared_added = False
    for code in HERD_FARM_OPTIONS:
        if code in CTS_SHARED_HOLDING_FARMS:
            if not shared_added:
                options.append(
                    {
                        "value": CTS_SHARED_HOLDING_PRIMARY,
                        "label": CTS_SHARED_HOLDING_LABEL,
                    }
                )
                shared_added = True
            continue
        options.append({"value": code, "label": code})
    return options


def cts_farm_option_values() -> tuple[str, ...]:
    return tuple(opt["value"] for opt in cts_farm_ui_options())


def cts_farm_labels() -> dict[str, str]:
    return {opt["value"]: opt["label"] for opt in cts_farm_ui_options()}


def cts_is_ui_farm(farm: str) -> bool:
    return cts_normalize_farm_key(farm) in set(cts_farm_option_values())


def cts_credential_lookup_farms(farm: str) -> tuple[str, ...]:
    """Env farm keys to try for CTWS credentials (primary first, then partners)."""
    key = cts_normalize_farm_key(farm)
    if key == CTS_SHARED_HOLDING_PRIMARY:
        partners = tuple(
            sorted(f for f in CTS_SHARED_HOLDING_FARMS if f != CTS_SHARED_HOLDING_PRIMARY)
        )
        return (CTS_SHARED_HOLDING_PRIMARY, *partners)
    return (key,)


def cts_farm_credential_parts(farm: str) -> dict[str, bool] | None:
    """Which CTWS/holding pieces are set for a farm (booleans only; no secrets)."""
    key = cts_normalize_farm_key(farm)
    if key not in HERD_FARM_OPTIONS and key not in set(cts_farm_option_values()):
        return None
    for lookup in cts_credential_lookup_farms(key):
        parts = {
            "username": bool(_farm_env(lookup, "CTWS_USERNAME")),
            "password": bool(_farm_env(lookup, "CTWS_PASSWORD")),
            "holding": bool(_farm_env(lookup, "HOLDING")),
        }
        if all(parts.values()):
            return parts
    # Return incomplete parts for the primary lookup so the UI can list missing keys.
    primary = cts_credential_lookup_farms(key)[0]
    return {
        "username": bool(_farm_env(primary, "CTWS_USERNAME")),
        "password": bool(_farm_env(primary, "CTWS_PASSWORD")),
        "holding": bool(_farm_env(primary, "HOLDING")),
    }


def cts_farm_credentials(farm: str) -> dict[str, str] | None:
    """Return CTWS + holding config for a farm, or None if incomplete."""
    key = cts_normalize_farm_key(farm)
    for lookup in cts_credential_lookup_farms(key):
        username = _farm_env(lookup, "CTWS_USERNAME")
        password = _farm_env(lookup, "CTWS_PASSWORD")
        holding = _farm_env(lookup, "HOLDING")
        if username and password and holding:
            return {
                "username": username,
                "password": password,
                "holding": holding,
            }
    return None


def cts_ready_farms() -> list[str]:
    """CTS UI farms with complete CTWS + holding config."""
    return [farm for farm in cts_farm_option_values() if cts_farm_credentials(farm) is not None]
