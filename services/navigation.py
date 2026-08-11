"""Sidebar navigation for Thomasson Farms Dashboard.

Add future submenus via the `children` list on any item.
Each item may include:
  permission: required page permission key (admins always pass)
  admin_only: only visible/accessible to admins
"""

NAV_ITEMS = [
    {
        "id": "home",
        "label": "Home",
        "endpoint": "home",
        "permission": "perm_home",
        "children": [],
    },
    {
        "id": "office",
        "label": "Office",
        "endpoint": "office",
        "permission": "perm_office",
        "children": [],
    },
    {
        "id": "parlours",
        "label": "Parlours",
        "endpoint": "parlours",
        "permission": "perm_parlours",
        "children": [
            {
                "id": "milking_efficiency",
                "label": "Milking Efficiency",
                "endpoint": "milking_efficiency",
                "permission": "perm_parlours",
            },
        ],
    },
    {
        "id": "stock_inventory",
        "label": "Stock Inventory",
        "endpoint": "stock_inventory",
        "permission": "perm_stock",
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


def filter_nav_items(user) -> list[dict]:
    """Return nav items the current user may see."""
    from services.auth import user_has_permission

    if user is None:
        return []

    filtered: list[dict] = []
    for item in NAV_ITEMS:
        if item.get("admin_only") and not user.is_admin:
            continue
        perm = item.get("permission")
        if perm and not user_has_permission(user, perm):
            continue

        children = []
        for child in item.get("children") or []:
            if child.get("admin_only") and not user.is_admin:
                continue
            child_perm = child.get("permission") or perm
            if child_perm and not user_has_permission(user, child_perm):
                continue
            children.append(child)

        filtered.append({**item, "children": children})
    return filtered
