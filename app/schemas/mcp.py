from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator


# Server names must be safe for OpenAI function.name: no dots, no __.
_MCP_SERVER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


class MCPServerConfig(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    command: str = Field(min_length=1, max_length=1024)
    args: list[str] = Field(default_factory=list)
    cwd: str = "."
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=20, ge=1, le=120)
    allowed_tools: list[str] | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if "__" in v:
            raise ValueError("MCP server name must not contain '__' (reserved namespace separator)")
        if not _MCP_SERVER_NAME_RE.match(v):
            raise ValueError(
                "MCP server name must start with a letter/digit and contain only "
                "alphanumeric characters, dashes, and underscores (max 64)."
            )
        return v


class MCPServersFile(BaseModel):
    servers: list[MCPServerConfig]


class MCPServerStatus(BaseModel):
    name: str
    enabled: bool
    status: str  # "connected" | "unavailable" | "disabled" | "error"
    tool_count: int = 0
    last_error: str | None = None


class MCPToolCallRequest(BaseModel):
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int | None = None


class MCPToolCallResponse(BaseModel):
    server: str
    tool: str
    is_error: bool = False
    content: list[dict[str, Any]] = Field(default_factory=list)
    raw: dict[str, Any] | None = None
