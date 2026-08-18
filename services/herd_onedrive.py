"""Discover and download DairyComp DCEXPORT files from Parlours OneDrive."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from services.farms import FARMS_BY_CODE
from services.graph_client import GRAPH_BASE, auth_mode, graph_get_bytes, require_azure_config
from services.graph_onedrive import GraphOneDriveService

DCEXPORT_FOLDER_RE = re.compile(r"^DCEXPORT([A-Z]{3})$", re.IGNORECASE)
CSV_SUFFIXES = {".csv"}

KIND_EVENTS = "events"
KIND_INVENTORY = "inventory"
KIND_BIRTHS = "births"


def local_herd_export_dir() -> Path | None:
    raw = os.environ.get("LOCAL_HERD_EXPORT_DIR", "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_dir() else None


def herd_import_configured() -> bool:
    if local_herd_export_dir() is not None:
        return True
    if os.environ.get("ONEDRIVE_SHARE_URL", "").strip() or os.environ.get("ONEDRIVE_USER", "").strip():
        return not require_azure_config()
    return False


def classify_csv_kind(name: str) -> str | None:
    upper = name.upper()
    if "EVENT" in upper:
        return KIND_EVENTS
    if "BORN" in upper or "BIRTH" in upper:
        return KIND_BIRTHS
    if "INV" in upper:
        return KIND_INVENTORY
    return None


def farm_code_from_dcexport_name(name: str) -> str | None:
    stem = Path(name).stem.upper()
    match = DCEXPORT_FOLDER_RE.match(stem)
    if not match:
        return None
    code = match.group(1)
    return code if code in FARMS_BY_CODE else None


def _local_mtime_iso(path: Path) -> str:
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return mtime.isoformat().replace("+00:00", "Z")


def _discover_local() -> list[dict[str, Any]]:
    root = local_herd_export_dir()
    if root is None:
        return []
    found: list[dict[str, Any]] = []
    for child in root.iterdir():
        farm = farm_code_from_dcexport_name(child.name)
        if farm is None:
            continue
        if child.is_file() and child.suffix.lower() in CSV_SUFFIXES:
            kind = classify_csv_kind(child.name) or KIND_EVENTS
            found.append(
                {
                    "farm": farm,
                    "kind": kind,
                    "name": child.name,
                    "relative_path": child.name,
                    "last_modified": _local_mtime_iso(child),
                    "local_path": child,
                }
            )
            continue
        if not child.is_dir():
            continue
        for file_path in child.iterdir():
            if not file_path.is_file() or file_path.suffix.lower() not in CSV_SUFFIXES:
                continue
            kind = classify_csv_kind(file_path.name)
            if kind is None:
                continue
            rel = f"{child.name}/{file_path.name}"
            found.append(
                {
                    "farm": farm,
                    "kind": kind,
                    "name": file_path.name,
                    "relative_path": rel,
                    "last_modified": _local_mtime_iso(file_path),
                    "local_path": file_path,
                }
            )
    return found


def _download_graph_item(service: GraphOneDriveService, item: dict) -> bytes:
    parent = item.get("parentReference") or {}
    drive_id = parent.get("driveId") or service._drive_id
    item_id = item.get("id")
    if drive_id and item_id:
        url = f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/content"
    elif auth_mode() == "delegated" and not service.user:
        url = f"{GRAPH_BASE}/me/drive/items/{item_id}/content"
    else:
        url = f"{GRAPH_BASE}/users/{quote(service.user)}/drive/items/{item_id}/content"
    return graph_get_bytes(url)


def _discover_onedrive() -> list[dict[str, Any]]:
    service = GraphOneDriveService()
    found: list[dict[str, Any]] = []
    for item in service._list_folder_page(service._root_children_url()):
        name = item.get("name") or ""
        farm = farm_code_from_dcexport_name(name)
        if farm is None:
            continue
        if item.get("file"):
            kind = classify_csv_kind(name) or KIND_EVENTS
            found.append(
                {
                    "farm": farm,
                    "kind": kind,
                    "name": name,
                    "relative_path": name,
                    "last_modified": item.get("lastModifiedDateTime") or "",
                    "graph_item": item,
                }
            )
            continue
        if not item.get("folder"):
            continue
        parent = item.get("parentReference") or {}
        drive_id = parent.get("driveId") or service._drive_id
        folder_id = item.get("id")
        if not drive_id or not folder_id:
            continue
        children_url = service._item_children_url(drive_id, folder_id)
        for child in service._list_folder_page(children_url):
            child_name = child.get("name") or ""
            if not child.get("file"):
                continue
            kind = classify_csv_kind(child_name)
            if kind is None:
                continue
            found.append(
                {
                    "farm": farm,
                    "kind": kind,
                    "name": child_name,
                    "relative_path": f"{name}/{child_name}",
                    "last_modified": child.get("lastModifiedDateTime") or "",
                    "graph_item": child,
                }
            )
    return found


def discover_dcexport_files() -> list[dict[str, Any]]:
    if local_herd_export_dir() is not None:
        return _discover_local()
    return _discover_onedrive()


def download_dcexport_file(entry: dict[str, Any]) -> bytes:
    local_path = entry.get("local_path")
    if local_path is not None:
        return Path(local_path).read_bytes()
    item = entry.get("graph_item")
    if not item:
        raise FileNotFoundError(f"No download source for {entry.get('relative_path')}")
    service = GraphOneDriveService()
    return _download_graph_item(service, item)


def files_for_kind(kind: str) -> list[dict[str, Any]]:
    return [entry for entry in discover_dcexport_files() if entry.get("kind") == kind]
