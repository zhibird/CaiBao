from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.tool_registry import (
    ToolDefinition,
    get_tool_definition,
    list_tool_definitions,
)


@dataclass
class ToolCatalogSnapshot:
    definitions: list[ToolDefinition] = field(default_factory=list)
    mcp_server_count: int = 0
    mcp_tool_count: int = 0


class ToolCatalogService:
    """Unified tool registry: builtin + generic + MCP tools."""

    def __init__(self) -> None:
        self._external_definitions: list[ToolDefinition] = []
        self._mcp_servers: dict[str, dict[str, Any]] = {}
        self._refresh_hook = None

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_generic(self, definitions: list[ToolDefinition]) -> None:
        """Register generic tools (web, file, shell). Called at startup."""
        # Deduplicate by name
        existing = {d.name for d in self.list_definitions()}
        for d in definitions:
            if d.name not in existing:
                self._external_definitions.append(d)  # temporary bucket for generic tools
                existing.add(d.name)

    def refresh_mcp(self, definitions: list[ToolDefinition]) -> ToolCatalogSnapshot:
        """Replace all MCP-origin tools with the given list."""
        generic_defs = [d for d in self._external_definitions if d.source == "generic"]
        builtin_defs = list_tool_definitions()
        self._external_definitions = generic_defs + definitions
        return ToolCatalogSnapshot(
            definitions=self.list_definitions(),
            mcp_server_count=len(self._mcp_servers),
            mcp_tool_count=len(definitions),
        )

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def list_definitions(
        self,
        enabled_tools: list[str] | None = None,
    ) -> list[ToolDefinition]:
        """Return all tools (builtin + generic + MCP)."""
        all_defs = list_tool_definitions() + self._external_definitions
        if enabled_tools is not None:
            allowed = {t.strip().lower() for t in enabled_tools if t.strip()}
            all_defs = [d for d in all_defs if d.name.lower() in allowed]
        return all_defs

    def get_definition(self, name: str) -> ToolDefinition | None:
        """Look up a tool by name across all sources."""
        # Check builtin first
        d = get_tool_definition(name)
        if d is not None:
            return d
        # Check generic + MCP
        lowered = name.strip().lower()
        for d in self._external_definitions:
            if d.name.lower() == lowered:
                return d
        return None

    def get_openai_tools(
        self,
        enabled_tools: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return OpenAI-compatible tool definitions."""
        definitions = self.list_definitions(enabled_tools=enabled_tools)
        return [d.to_openai_tool() for d in definitions if d.enabled]

    def mcp_server_status(self) -> list[dict[str, Any]]:
        """Return status of all configured MCP servers."""
        return [
            {
                "name": name,
                "enabled": info.get("enabled", False),
                "status": info.get("status", "unknown"),
                "tool_count": info.get("tool_count", 0),
                "last_error": info.get("last_error"),
            }
            for name, info in self._mcp_servers.items()
        ]
