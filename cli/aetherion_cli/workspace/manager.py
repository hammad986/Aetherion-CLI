"""
aetherion_cli/workspace/manager.py
Workspace discovery, selection, and local persistence.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..api.client import ApiClient
from ..api.contracts import WorkspaceInfo
from ..config.settings import get_active_workspace, set_active_workspace


class WorkspaceManager:
    """Manages workspace lifecycle for CLI sessions."""

    def __init__(self, client: Optional[ApiClient] = None):
        self.client = client or ApiClient()

    def list_workspaces(self) -> List[WorkspaceInfo]:
        """Fetch all workspaces for current account."""
        return self.client.list_workspaces()

    def get_current(self) -> Optional[str]:
        """Return ID of the currently selected workspace."""
        return get_active_workspace()

    def select(self, workspace_id: str) -> str:
        """Select active workspace."""
        set_active_workspace(workspace_id)
        return workspace_id

    def create(self, name: str) -> WorkspaceInfo:
        """Create a new workspace."""
        res = self.client.post("/api/workspaces", json_body={"name": name})
        info = WorkspaceInfo.from_dict(res)
        self.select(info.workspace_id)
        return info
