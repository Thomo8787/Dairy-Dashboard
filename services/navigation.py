"""Sidebar navigation for Thomasson Farms Dashboard.

Add future submenus via the `children` list on any item.
"""

NAV_ITEMS = [
    {
        "id": "home",
        "label": "Home",
        "endpoint": "home",
        "children": [],
    },
    {
        "id": "office",
        "label": "Office",
        "endpoint": "office",
        "children": [],
    },
    {
        "id": "parlours",
        "label": "Parlours",
        "endpoint": "parlours",
        "children": [
            {
                "id": "milking_efficiency",
                "label": "Milking Efficiency",
                "endpoint": "milking_efficiency",
            },
        ],
    },
    {
        "id": "stock_inventory",
        "label": "Stock Inventory",
        "endpoint": "stock_inventory",
        "children": [],
    },
]


def parent_nav_id(active_nav: str) -> str | None:
    """Return parent nav id when a submenu page is active."""
    for item in NAV_ITEMS:
        if item["id"] == active_nav:
            return None
        for child in item.get("children") or []:
            if child["id"] == active_nav:
                return item["id"]
    return None
