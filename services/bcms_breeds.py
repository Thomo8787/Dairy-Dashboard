"""Map DairyComp CBRD (numeric cattle breed) to BCMS / CTS breed codes.

DairyComp CBRD values are farm-configured integers. This table was inferred from
animals present in both herd_inventory and the CTS on-holding snapshot for CM/GAD
(majority CTS breed per CBRD), and can be extended as new codes appear.
"""

from __future__ import annotations

from typing import Any

# DairyComp CBRD -> official BCMS breed code (GOV.UK CTS list).
CBRD_TO_BCMS_BREED: dict[int, str] = {
    1: "HF",  # Holstein Friesian
    4: "JE",  # Jersey
    19: "HE",  # Hereford
    21: "AA",  # Aberdeen Angus
    101: "HF",  # Holstein Friesian
    119: "HEX",  # Hereford Cross
    121: "AAX",  # Aberdeen Angus Cross
    254: "WAX",  # Wagyu Cross
}


def normalize_cbrd(cbrd: Any) -> int | None:
    if cbrd is None:
        return None
    try:
        return int(cbrd)
    except (TypeError, ValueError):
        return None


def bcms_breed_from_cbrd(cbrd: Any) -> str:
    """Return the BCMS breed code for a DairyComp CBRD, or '' if unknown."""
    code = normalize_cbrd(cbrd)
    if code is None:
        return ""
    return CBRD_TO_BCMS_BREED.get(code, "")
