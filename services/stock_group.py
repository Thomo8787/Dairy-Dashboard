"""Shared stock-group classification (aligned with Stock Accruals event filters)."""

from __future__ import annotations

from services.database import (
    STOCK_GROUP_BEEF,
    STOCK_GROUP_COWS,
    STOCK_GROUP_OPTIONS,
    STOCK_GROUP_YOUNGSTOCK,
)
from services.herd_import_utils import BEEF_CBREED_MIN, CATEGORY_BEEF
from services.inventory_valuation import inventory_sbrd_is_beef

VALID_STOCK_GROUPS = set(STOCK_GROUP_OPTIONS)


def normalize_stock_group(value: str | None) -> str:
    normalized = (value or STOCK_GROUP_COWS).strip().lower()
    if normalized not in VALID_STOCK_GROUPS:
        return STOCK_GROUP_COWS
    return normalized


def _normalize_lact(lact: int | float | None) -> int:
    if lact is None:
        return 0
    try:
        return int(lact)
    except (TypeError, ValueError):
        return 0


def _normalize_cbrd(cbrd: int | float | None) -> int | None:
    if cbrd is None:
        return None
    try:
        return int(cbrd)
    except (TypeError, ValueError):
        return None


def stock_group_from_event_fields(
    lact: int | float | None,
    cbrd: int | float | None,
    gndr: str | None,
) -> str:
    """Mirror ``_apply_cow_event_stock_group`` in stock_accruals."""
    lact_n = _normalize_lact(lact)
    if lact_n > 0:
        return STOCK_GROUP_COWS

    gender = (gndr or "").strip().upper()
    cbrd_code = _normalize_cbrd(cbrd)
    if gender == "F" and cbrd_code is not None and cbrd_code < BEEF_CBREED_MIN:
        return STOCK_GROUP_YOUNGSTOCK
    return STOCK_GROUP_BEEF


def stock_group_from_inventory(lact: int | float | None, sbrd: str | None) -> str:
    lact_n = _normalize_lact(lact)
    if lact_n > 0:
        return STOCK_GROUP_COWS
    if inventory_sbrd_is_beef(sbrd):
        return STOCK_GROUP_BEEF
    return STOCK_GROUP_YOUNGSTOCK


def stock_group_from_birth(
    birth_category: str | None,
    cbrd: int | float | None,
    gndr: str | None,
) -> str:
    if (birth_category or "").strip() == CATEGORY_BEEF:
        return STOCK_GROUP_BEEF
    return stock_group_from_event_fields(0, cbrd, gndr)
