"""Import Excel files from OneDrive via Microsoft Graph."""

import base64
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from services.graph_client import EXCEL_EXTENSIONS, GRAPH_BASE, auth_mode, graph_get, graph_get_bytes


def encode_sharing_url(sharing_url: str) -> str:
    """Convert a OneDrive/SharePoint sharing link into a Graph share ID (u!...)."""
    encoded = base64.b64encode(sharing_url.encode("utf-8")).decode("utf-8")
    encoded = encoded.rstrip("=").replace("/", "_").replace("+", "-")
    return f"u!{encoded}"


class GraphOneDriveService:
    def __init__(self, download_dir: str | Path | None = None):
        self.user = os.environ.get("ONEDRIVE_USER", "").strip()
        self.folder_path = os.environ.get("ONEDRIVE_FOLDER_PATH", "").strip().strip("/")
        self.share_url = os.environ.get("ONEDRIVE_SHARE_URL", "").strip()
        self.download_dir = Path(download_dir or "data")
        self.download_dir.mkdir(parents=True, exist_ok=True)

        # Resolved when using a sharing link.
        self._drive_id: str | None = None
        self._folder_item_id: str | None = None
        self._folder_name: str | None = None

        if not self.share_url and auth_mode() == "application" and not self.user:
            raise RuntimeError(
                "Set ONEDRIVE_SHARE_URL (preferred) or ONEDRIVE_USER=parlours@alhfarm.com."
            )

    def _resolve_share_folder(self) -> None:
        if self._drive_id and self._folder_item_id:
            return

        share_id = encode_sharing_url(self.share_url)
        item = graph_get(
            f"{GRAPH_BASE}/shares/{share_id}/driveItem"
            f"?$select=id,name,folder,parentReference"
        )
        if not item.get("folder"):
            raise RuntimeError("ONEDRIVE_SHARE_URL must point to a folder, not a single file.")

        parent = item.get("parentReference") or {}
        drive_id = parent.get("driveId")
        item_id = item.get("id")
        if not drive_id or not item_id:
            raise RuntimeError("Could not resolve OneDrive folder from the sharing link.")

        self._drive_id = drive_id
        self._folder_item_id = item_id
        self._folder_name = item.get("name") or "shared-folder"

    def _children_url(self) -> str:
        if self.share_url:
            self._resolve_share_folder()
            return f"{GRAPH_BASE}/drives/{self._drive_id}/items/{self._folder_item_id}/children"

        if auth_mode() == "delegated" and not self.user:
            root = "me/drive"
        else:
            root = f"users/{quote(self.user)}/drive"

        if self.folder_path:
            encoded_path = quote(self.folder_path)
            return f"{GRAPH_BASE}/{root}/root:/{encoded_path}:/children"
        return f"{GRAPH_BASE}/{root}/root/children"

    def _list_excel_items(self) -> list[dict]:
        url = (
            f"{self._children_url()}"
            f"?$select=id,name,file,folder,lastModifiedDateTime,size,parentReference"
            f"&$orderby=lastModifiedDateTime desc"
            f"&$top=50"
        )
        items = graph_get(url).get("value", [])
        excel_files = []
        for item in items:
            if item.get("folder"):
                continue
            name = item.get("name", "")
            if Path(name).suffix.lower() not in EXCEL_EXTENSIONS:
                continue
            excel_files.append(item)
        return excel_files

    def _download_item(self, item: dict) -> Path:
        if self.share_url:
            self._resolve_share_folder()
            drive_id = (item.get("parentReference") or {}).get("driveId") or self._drive_id
            content_url = f"{GRAPH_BASE}/drives/{drive_id}/items/{item['id']}/content"
        elif auth_mode() == "delegated" and not self.user:
            content_url = f"{GRAPH_BASE}/me/drive/items/{item['id']}/content"
        else:
            content_url = f"{GRAPH_BASE}/users/{quote(self.user)}/drive/items/{item['id']}/content"

        content = graph_get_bytes(content_url)
        safe_name = Path(item.get("name", "onedrive.xlsx")).name
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = self.download_dir / f"{timestamp}_{safe_name}"
        destination.write_bytes(content)
        return destination

    def fetch_excel_files(self) -> list[dict]:
        downloads: list[dict] = []

        if self.share_url:
            self._resolve_share_folder()
            folder_label = f"OneDrive(share):/{self._folder_name}"
        else:
            owner = self.user or "me"
            folder_label = (
                f"OneDrive({owner}):/{self.folder_path}"
                if self.folder_path
                else f"OneDrive({owner}):/root"
            )

        for item in self._list_excel_items():
            file_path = self._download_item(item)

            modified_raw = item.get("lastModifiedDateTime")
            modified_at = None
            if modified_raw:
                modified_at = datetime.fromisoformat(modified_raw.replace("Z", "+00:00"))

            downloads.append(
                {
                    "file_path": file_path,
                    "filename": file_path.name,
                    "email_subject": f"{folder_label}/{item.get('name')}",
                    "email_received_at": modified_at,
                    "message_id": item["id"],
                }
            )

        return downloads
