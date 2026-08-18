"""Farm registry for Thomasson Farms Dashboard.

ALH and BNK share a DairyComp data source. The other farms are standalone.
Only ALH/BNK are active for Outlook/OneDrive imports for now.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Farm:
    code: str
    name: str
    short_name: str
    dairy_data_group: str
    """Logical dairy data group id, e.g. 'alh_bnk' for shared DairyComp."""
    has_dairycomp: bool = False
    active_for_imports: bool = False
    # DataFlow night/evening milkings keep one milking_date across midnight.
    night_shift_crosses_midnight: bool = True
    # Rotary stall count. Used for parlour efficiency (potential cows/hour).
    stall_count: int | None = None


FARMS: tuple[Farm, ...] = (
    Farm(
        code="ALH",
        name="Aston Lower Hall",
        short_name="ALH",
        dairy_data_group="alh_bnk",
        has_dairycomp=True,
        active_for_imports=True,
        night_shift_crosses_midnight=True,
        stall_count=60,
    ),
    Farm(
        code="BNK",
        name="Bank Farm",
        short_name="BNK",
        dairy_data_group="alh_bnk",
        has_dairycomp=True,
        active_for_imports=True,
        night_shift_crosses_midnight=True,
    ),
    Farm(
        code="SFR",
        name="Park Hall Farm",
        short_name="SFR",
        dairy_data_group="sfr",
        has_dairycomp=False,
        active_for_imports=False,
        night_shift_crosses_midnight=True,
    ),
    Farm(
        code="PRK",
        name="The Parkes",
        short_name="PRK",
        dairy_data_group="prk",
        has_dairycomp=False,
        active_for_imports=False,
        night_shift_crosses_midnight=True,
    ),
    Farm(
        code="COF",
        name="Cherry Orchard Farm",
        short_name="COF",
        dairy_data_group="cof",
        has_dairycomp=False,
        active_for_imports=False,
        night_shift_crosses_midnight=True,
    ),
)

FARMS_BY_CODE = {farm.code: farm for farm in FARMS}


def active_farms() -> list[Farm]:
    return [farm for farm in FARMS if farm.active_for_imports]


def farms_in_group(group_id: str) -> list[Farm]:
    return [farm for farm in FARMS if farm.dairy_data_group == group_id]


def dairy_data_groups() -> dict[str, list[Farm]]:
    groups: dict[str, list[Farm]] = {}
    for farm in FARMS:
        groups.setdefault(farm.dairy_data_group, []).append(farm)
    return groups
