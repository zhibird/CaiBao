from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(slots=True)
class ToolCallContext:
    run_id: str | None
    team_id: str
    user_id: str
    tool_name: str
    arguments: dict[str, Any]
    source: Literal["builtin", "generic", "mcp"] = "builtin"
    dry_run: bool = False


@dataclass(slots=True)
class ToolPreflightResult:
    allowed: bool
    reason: str | None = None
    normalized_arguments: dict[str, Any] = field(default_factory=dict)
    requires_confirmation: bool = False
    dangerous: bool = False
