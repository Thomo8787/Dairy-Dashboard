"""Import Excel files from OneDrive via Microsoft Graph."""

import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from services.graph_client import EXCEL_EXTENSIONS, GRAPH_BASE, graph_get, graph_get_bytes


class GraphOneDriveService:
    def __init__(self, download_dir: str | Path | None = None):
        self.user = os.environ.get("ONEDRIVE_USER", "").strip()
        self.folder_path = os.environ.get("ONEDRIVE_FOLDER_PATH", "").strip().strip("/")
        self.download_dir = Path(download_dir or "data")
        self.download_dir.mkdir(parents=True, exist_ok=True)

        if not self.user:
            raise RuntimeError("ONEDRIVE_USER is not set (email of the OneDrive owner)")

    def _children_url(self) -> str:
        user = quote(self.user)
        if self.folder_path:
            encoded_path = quote(self.folder_path)
            return f"{GRAPH_BASE}/users/{user}/drive/root:/{encoded_path}:/children"
        return f"{GRAPH_BASE}/users/{user}/drive/root/children"

    def _list_excel_items(self) -> list[dict]:
        url = (
            f"{self._children_url()}"
            f"?$select=id,name,file,folder,lastModifiedDateTime,size"
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
        user = quote(self.user)
        item_id = item["id"]
        content = graph_get_bytes(f"{GRAPH_BASE}/users/{user}/drive/items/{item_id}/content")

        safe_name = Path(item.get("name", "onedrive.xlsx")).name
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = self.download_dir / f"{timestamp}_{safe_name}"
        destination.write_bytes(content)
        return destination

    def fetch_excel_files(self) -> list[dict]:
        """
        Download Excel files from the configured OneDrive folder.

        Returns dicts shaped like Outlook imports so the same save path works:
        {
            "file_path": Path,
            "filename": str,
            "email_subject": str,       # OneDrive path label
            "email_received_at": datetime,
            "message_id": str,          # drive item id
        }
        """
        downloads: list[dict] = []
        folder_label = f"OneDrive:/{self.folder_path}" if self.folder_path else "OneDrive:/root"

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
