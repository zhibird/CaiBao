from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from app.core.config import PROJECT_ROOT, get_settings
from app.core.exceptions import DomainValidationError
from app.schemas.mcp import MCPServerConfig, MCPServersFile
from app.services.mcp_client import MCPServerError, MCPStdioClient
from app.services.tool_registry import ToolDefinition

logger = logging.getLogger(__name__)


class MCPManager:
    """Manages MCP server lifecycle, tool discovery, and namespace mapping."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._servers: dict[str, MCPStdioClient] = {}
        self._configs: dict[str, MCPServerConfig] = {}
        self._server_status: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    def load_config(self) -> list[MCPServerConfig]:
        """Load MCP server configs from the configured JSON file.

        Relative paths resolve against the project root, not the process cwd,
        so the app behaves the same under systemd/IDE/non-root launch dirs.
        """
        config_path = Path(self.settings.mcp_config_path)
        if not config_path.is_absolute():
            config_path = PROJECT_ROOT / config_path
        if not config_path.exists():
            logger.warning("MCP is enabled but config file %s does not exist; no servers loaded.", config_path)
            return []

        raw = config_path.read_text("utf-8")
        # Expand ${ENV_VAR} placeholders
        raw = self._expand_env_vars(raw)

        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise DomainValidationError(f"Invalid MCP config JSON: {exc}") from exc

        servers_file = MCPServersFile.model_validate(data)
        return servers_file.servers

    @staticmethod
    def _expand_env_vars(raw: str) -> str:
        import os

        def _replace(match):
            var_name = match.group(1)
            return os.environ.get(var_name, match.group(0))
        return re.sub(r"\$\{(\w+)\}", _replace, raw)

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    def connect_all(self, configs: list[MCPServerConfig]) -> dict[str, dict[str, Any]]:
        """Connect to all enabled servers, discover their tools."""
        self.shutdown_all()
        self._configs = {c.name: c for c in configs}
        status: dict[str, dict[str, Any]] = {}

        for config in configs:
            status[config.name] = self._connect_one(config)

        self._server_status = status
        return status

    def _connect_one(self, config: MCPServerConfig) -> dict[str, Any]:
        if not config.enabled:
            return {"name": config.name, "enabled": False, "status": "disabled", "tool_count": 0, "last_error": None}

        # Resolve relative cwd against the project root so relative commands
        # (e.g. ".venv/bin/python") work regardless of the launch directory.
        cwd = Path(config.cwd or ".")
        if not cwd.is_absolute():
            cwd = PROJECT_ROOT / cwd

        client = MCPStdioClient(
            command=config.command,
            args=config.args,
            cwd=str(cwd),
            env=config.env or {},
            timeout_seconds=config.timeout_seconds,
        )
        try:
            client.connect()
        except (MCPServerError, DomainValidationError, OSError) as exc:
            # Reap the child if it was spawned but the handshake failed —
            # otherwise each reload against a broken server leaks a process.
            client.disconnect()
            logger.warning("MCP server '%s' failed to connect: %s", config.name, exc)
            return {"name": config.name, "enabled": True, "status": "error", "tool_count": 0, "last_error": str(exc)}

        self._servers[config.name] = client
        tools = client.tools
        logger.info("MCP server '%s' connected with %d tool(s).", config.name, len(tools))
        return {
            "name": config.name,
            "enabled": True,
            "status": "connected",
            "tool_count": len(tools),
            "last_error": None,
        }

    def shutdown_all(self) -> None:
        for name, client in self._servers.items():
            try:
                client.disconnect()
            except Exception:
                pass
        self._servers.clear()
        self._configs.clear()

    # ------------------------------------------------------------------
    # Tool discovery
    # ------------------------------------------------------------------

    def discover_tools(self) -> list[ToolDefinition]:
        """Return ToolDefinition objects for all MCP-discovered tools."""
        definitions: list[ToolDefinition] = []
        for name, client in self._servers.items():
            config = self._configs.get(name)
            allowed = None
            if config is not None and config.allowed_tools:
                allowed = set(t.strip() for t in config.allowed_tools if t.strip())

            if not client.is_alive:
                self._server_status[name] = {
                    "name": name, "enabled": True, "status": "unavailable",
                    "tool_count": 0, "last_error": "Server process exited.",
                }
                continue

            try:
                mcp_tools = client.tools
            except Exception:
                mcp_tools = []

            _TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

            for mcp_tool in mcp_tools:
                tool_name = mcp_tool.get("name", "")
                if not tool_name:
                    continue
                if allowed is not None and tool_name not in allowed:
                    continue

                # Reject tool names that would break OpenAI function.name or
                # namespace parsing (spaces, path separators, __, excessive
                # length, etc.).
                if not _TOOL_NAME_RE.match(tool_name):
                    continue

                namespaced = f"mcp__{name}__{tool_name}"
                # OpenAI function.name ≤ 64 chars
                if len(namespaced) > 64:
                    continue
                input_schema = mcp_tool.get("inputSchema", {})
                if not isinstance(input_schema, dict):
                    input_schema = {"type": "object", "properties": {}}
                input_schema.setdefault("type", "object")

                annotations = mcp_tool.get("annotations", {})
                if not isinstance(annotations, dict):
                    annotations = {}
                dangerous = (
                    bool(annotations.get("destructiveHint"))
                    or bool(annotations.get("destructive"))
                )

                definitions.append(ToolDefinition(
                    name=namespaced,
                    display_name=f"[MCP:{name}] {tool_name}",
                    description=str(mcp_tool.get("description", f"MCP tool: {tool_name}")),
                    dangerous=dangerous,
                    input_schema=input_schema,
                    output_schema=mcp_tool.get("outputSchema", {}) or {},
                    handler_key=f"mcp.{namespaced}",
                    permission_scope="team",
                    source="mcp",
                    provider=name,
                ))

        return definitions

    # ------------------------------------------------------------------
    # Tool call
    # ------------------------------------------------------------------

    def call_tool(
        self,
        namespaced_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Call an MCP tool by its namespaced name. Returns wrapped result."""
        # Parse namespace: mcp__{server}__{tool}
        parts = namespaced_name.split("__", 2)
        if len(parts) != 3 or parts[0] != "mcp":
            raise DomainValidationError(f"Invalid MCP tool name: {namespaced_name}")

        server_name = parts[1]
        tool_name = parts[2]

        # Check allowed_tools whitelist
        config = self._configs.get(server_name)
        if config is not None and config.allowed_tools:
            allowed = set(t.strip() for t in config.allowed_tools if t.strip())
            if tool_name not in allowed:
                raise DomainValidationError(
                    f"MCP tool '{tool_name}' is not in the allowed_tools list for server '{server_name}'."
                )

        client = self._servers.get(server_name)
        if client is None:
            raise DomainValidationError(f"MCP server '{server_name}' is not connected.")

        if not client.is_alive:
            self._server_status[server_name] = {
                "name": server_name, "enabled": True, "status": "unavailable",
                "tool_count": 0, "last_error": "Server process exited.",
            }
            raise DomainValidationError(f"MCP server '{server_name}' is unavailable.")

        try:
            result = client.call_tool(tool_name, arguments)
        except MCPServerError as exc:
            self._server_status[server_name] = {
                "name": server_name, "enabled": True, "status": "error",
                "tool_count": len(client.tools), "last_error": str(exc),
            }
            raise

        content = result.get("content", [])
        is_error = result.get("isError", False)
        if isinstance(is_error, str):
            is_error = is_error.lower() in {"true", "yes", "1"}

        return {
            "server": server_name,
            "tool": tool_name,
            "is_error": bool(is_error),
            "content": content if isinstance(content, list) else [],
            "raw": result,
        }

    def status(self) -> list[dict[str, Any]]:
        return list(self._server_status.values())
