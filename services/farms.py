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


FARMS: tuple[Farm, ...] = (
    Farm(
        code="ALH",
        name="Aston Lower Hall",
        short_name="ALH",
        dairy_data_group="alh_bnk",
        has_dairycomp=True,
        active_for_imports=True,
    ),
    Farm(
        code="BNK",
        name="Bank Farm",
        short_name="BNK",
        dairy_data_group="alh_bnk",
        has_dairycomp=True,
        active_for_imports=True,
    ),
    Farm(
        code="SFR",
        name="Park Hall Farm",
        short_name="SFR",
        dairy_data_group="sfr",
        has_dairycomp=False,
        active_for_imports=False,
    ),
    Farm(
        code="PRK",
        name="The Parkes",
        short_name="PRK",
        dairy_data_group="prk",
        has_dairycomp=False,
        active_for_imports=False,
    ),
    Farm(
        code="COF",
        name="Cherry Orchard Farm",
        short_name="COF",
        dairy_data_group="cof",
        has_dairycomp=False,
        active_for_imports=False,
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
