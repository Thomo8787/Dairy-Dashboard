"""Shared inventory valuation rules (Power Query / DC305 methodology)."""

from __future__ import annotations

from services.herd_import_utils import CATEGORY_BEEF, category_from_birth

CATEGORIES: tuple[str, ...] = ("Beef", "Dairy", "Youngstock")
VALUE_CAP = 1800.0

# DairyComp SBRD codes treated as dairy (legacy blank/"H" plus Holstein variants).
_DAIRY_SBRD_CODES = frozenset({"", "H", "HF", "HO", "HOLSTEIN"})


def normalize_inventory_sbrd(sbrd: str | None) -> str:
    """Uppercase raw SBRD code from the inventory file (e.g. HF, AAX, HEX)."""
    if sbrd is None:
        return ""
    text = str(sbrd).strip().upper()
    if text in {"", "NAN", "NONE", "-"}:
        return ""
    return text


def inventory_sbrd_is_beef(sbrd: str | None) -> bool:
    """True when SBRD is a beef code (AA/AAX/HE/HEX/…) or legacy 'Beef' label."""
    return normalize_inventory_sbrd(sbrd) not in _DAIRY_SBRD_CODES


def _normalize_lact(lact: int | float | None) -> int:
    if lact is None:
        return 0
    try:
        return int(lact)
    except (TypeError, ValueError):
        return 0


def category_from_inventory(lact: int | float | None, sbrd: str | None) -> str:
    lact_n = _normalize_lact(lact)
    if lact_n > 0:
        return "Dairy"
    if inventory_sbrd_is_beef(sbrd):
        return "Beef"
    if lact_n == 0:
        return "Youngstock"
    return "Dairy"


def category_from_event_proxy(
    lact: int | float | None,
    cbrd: int | float | None,
    gndr: str | None,
) -> str:
    lact_n = _normalize_lact(lact)
    if lact_n > 0:
        return "Dairy"
    if category_from_birth(cbrd, gndr) == CATEGORY_BEEF:
        return "Beef"
    return "Youngstock"


def compute_value(
    lact: int | float | None,
    category: str,
    aged_days: int | float | None,
) -> float:
    lact_n = _normalize_lact(lact)
    if lact_n == 1:
        return 2500.0
    if lact_n == 2:
        return 2200.0
    if lact_n > 2:
        return 1800.0

    aged_val = 0
    if aged_days is not None:
        try:
            aged_val = max(0, int(aged_days))
        except (TypeError, ValueError):
            aged_val = 0

    if category == "Beef":
        base_value = 100 + (1.90 * aged_val)
    elif category == "Youngstock":
        base_value = 100 + (2.5 * aged_val)
    else:
        base_value = 100.0
    return round(min(base_value, VALUE_CAP), 0)
