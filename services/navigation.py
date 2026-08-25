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
            {
                "id": "stall_issues",
                "label": "Stall Issues",
                "endpoint": "stall_issues",
                "permission": "perm_parlours",
            },
            {
                "id": "scatter_graphs",
                "label": "Scatter Graphs",
                "endpoint": "scatter_graphs",
                "permission": "perm_parlours",
            },
            {
                "id": "parlour_efficiency",
                "label": "Efficiency",
                "endpoint": "parlour_efficiency",
                "permission": "perm_parlours",
            },
        ],
    },
    {
        "id": "events",
        "label": "Events",
        "endpoint": "events",
        "permission": "perm_events",
        "children": [
            {"id": "events_calvings", "label": "Calvings", "endpoint": "events_calvings", "permission": "perm_events"},
            {"id": "events_births", "label": "Births", "endpoint": "events_births", "permission": "perm_events"},
            {"id": "events_sales", "label": "Sales", "endpoint": "events_sales", "permission": "perm_events"},
            {"id": "events_deaths", "label": "Deaths", "endpoint": "events_deaths", "permission": "perm_events"},
            {"id": "events_disease", "label": "Disease", "endpoint": "events_disease", "permission": "perm_events"},
            {"id": "events_hooftrimming", "label": "Hoof Trimming", "endpoint": "events_hooftrimming", "permission": "perm_events"},
            {"id": "events_breedings", "label": "Breedings", "endpoint": "events_breedings", "permission": "perm_events"},
            {"id": "events_total_protein", "label": "Total Protein", "endpoint": "events_total_protein", "permission": "perm_events"},
        ],
    },
    {
        "id": "stock_inventory",
        "label": "Stock Inventory",
        "endpoint": "stock_inventory",
        "permission": "perm_stock",
        "children": [
            {"id": "stock_heifer_inventory", "label": "Heifer Inventory", "endpoint": "stock_heifer_inventory", "permission": "perm_stock"},
            {"id": "stock_beef_inventory", "label": "Beef Inventory", "endpoint": "stock_beef_inventory", "permission": "perm_stock"},
            {"id": "stock_calves_due", "label": "Calves Due", "endpoint": "stock_calves_due", "permission": "perm_stock"},
            {"id": "stock_heifers_due", "label": "Heifers Due", "endpoint": "stock_heifers_due", "permission": "perm_stock"},
        ],
    },
    {
        "id": "genetics",
        "label": "Genetics",
        "endpoint": "genetics",
        "permission": "perm_genetics",
        "children": [
            {
                "id": "genetics_genomic_progress",
                "label": "Genomic Progress",
                "endpoint": "genetics_genomic_progress",
                "permission": "perm_genetics",
            },
        ],
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
