from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from app.services.mcp_manager import MCPManager

_logger = logging.getLogger(__name__)


class ProactiveGateway:
    """Pulls alert / content / context events from configured MCP sources."""

    def __init__(self, mcp_manager: MCPManager) -> None:
        self._mcp = mcp_manager

    def _ensure_connected(self, mcp_server: str) -> None:
        # Check if already connected — avoid calling connect_all which shutdowns all
        if mcp_server in self._mcp._servers:
            return
        configs = self._mcp.load_config()
        for cfg in configs:
            if cfg.name == mcp_server and cfg.enabled:
                # Use the internal _connect_one to avoid shutting down other servers
                self._mcp._connect_one(cfg)
                return

    def pull_events(self, source: dict[str, Any]) -> list[dict[str, Any]]:
        """Pull events from a single source config. Returns list of event dicts."""
        mcp_server = source.get("mcp_server", "")
        get_tool = source.get("get_tool", "")
        if not mcp_server or not get_tool:
            return []

        self._ensure_connected(mcp_server)
        tool_name = f"mcp__{mcp_server}__{get_tool}"
        try:
            result = self._mcp.call_tool(tool_name, {})
        except Exception:
            _logger.exception("Failed to pull from %s/%s", mcp_server, get_tool)
            return []

        content = result.get("content", [])
        events: list[dict[str, Any]] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, list):
                        events.extend(parsed)
                    elif isinstance(parsed, dict):
                        events.append(parsed)
                except (json.JSONDecodeError, TypeError):
                    events.append({"text": text, "raw": True})
            elif isinstance(item, dict):
                events.append(item)
        return events

    def ack_event(self, source: dict[str, Any], event_id: str) -> bool:
        """Acknowledge a consumed event."""
        ack_tool = source.get("ack_tool", "")
        mcp_server = source.get("mcp_server", "")
        if not ack_tool or not mcp_server:
            return False
        try:
            self._mcp.call_tool(f"mcp__{mcp_server}__{ack_tool}", {"event_id": event_id})
            return True
        except Exception:
            _logger.exception("Failed to ack event %s", event_id)
            return False

    @staticmethod
    def dedupe_hash(event: dict[str, Any]) -> str:
        """Stable hash for event deduplication."""
        raw = json.dumps(event, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()
