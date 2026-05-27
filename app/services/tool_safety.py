from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from app.core.config import get_settings
from app.schemas.tooling import ToolCallContext, ToolPreflightResult
from app.services.tool_registry import ToolDefinition

_MAX_CALLS_PER_RUN = 20
_MAX_CALLS_PER_TOOL = 8
_MAX_CONSECUTIVE_DUPLICATE = 3


class ToolSafetyService:
    """Pre/post execution safety hooks for all tool calls."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._run_call_counts: dict[str, int] = defaultdict(int)
        self._run_tool_call_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._run_tool_history: dict[str, list[tuple[str, str]]] = defaultdict(list)

    def reset_run(self, run_id: str) -> None:
        self._run_call_counts.pop(run_id, None)
        self._run_tool_call_counts.pop(run_id, None)
        self._run_tool_history.pop(run_id, None)

    def preflight(self, ctx: ToolCallContext, definition: ToolDefinition) -> ToolPreflightResult:
        """Run all pre-execution checks. Returns a ToolPreflightResult."""
        # 1. Tool exists (caller should already have verified this)
        if definition is None:
            return ToolPreflightResult(allowed=False, reason="Unknown tool.")

        # 2. Tool is enabled
        if not definition.enabled:
            return ToolPreflightResult(allowed=False, reason=f"Tool '{ctx.tool_name}' is disabled.")

        # 3. Normalize and validate arguments against schema
        normalized = definition.normalize_arguments(ctx.arguments)
        schema_errors = definition.validate_arguments(normalized)
        if schema_errors:
            return ToolPreflightResult(
                allowed=False,
                reason=f"Schema validation failed: {'; '.join(schema_errors)}",
                normalized_arguments=normalized,
            )

        # 4. Loop guard — max calls per run
        if ctx.run_id:
            total = self._run_call_counts[ctx.run_id]
            if total >= _MAX_CALLS_PER_RUN:
                return ToolPreflightResult(
                    allowed=False,
                    reason=f"Max tool calls ({_MAX_CALLS_PER_RUN}) exceeded for this run.",
                    normalized_arguments=normalized,
                )
            per_tool = self._run_tool_call_counts[ctx.run_id].get(ctx.tool_name, 0)
            if per_tool >= _MAX_CALLS_PER_TOOL:
                return ToolPreflightResult(
                    allowed=False,
                    reason=f"Max calls for tool '{ctx.tool_name}' ({_MAX_CALLS_PER_TOOL}) exceeded.",
                    normalized_arguments=normalized,
                )

        # 5. Loop guard — consecutive duplicate detection
        if ctx.run_id:
            sig = self._call_signature(ctx.tool_name, normalized)
            history = self._run_tool_history[ctx.run_id]
            if len(history) >= _MAX_CONSECUTIVE_DUPLICATE:
                recent = history[-(_MAX_CONSECUTIVE_DUPLICATE):]
                if all(item == sig for item in recent):
                    return ToolPreflightResult(
                        allowed=False,
                        reason=f"Same tool+args called {_MAX_CONSECUTIVE_DUPLICATE}+ times consecutively. Loop blocked.",
                        normalized_arguments=normalized,
                    )

        # 6. Dangerous confirmation flag
        requires_confirmation = definition.dangerous and not ctx.dry_run

        return ToolPreflightResult(
            allowed=True,
            normalized_arguments=normalized,
            requires_confirmation=requires_confirmation,
            dangerous=definition.dangerous,
        )

    def record_call(self, ctx: ToolCallContext) -> None:
        """Record a successful tool call for loop guard tracking."""
        if ctx.run_id:
            self._run_call_counts[ctx.run_id] += 1
            self._run_tool_call_counts[ctx.run_id][ctx.tool_name] += 1
            sig = self._call_signature(ctx.tool_name, ctx.arguments)
            self._run_tool_history[ctx.run_id].append(sig)

    def postflight_output(
        self, result: dict[str, Any], *, source: str = "builtin"
    ) -> dict[str, Any]:
        """Post-execution: truncate large output fields."""
        max_bytes = self.settings.mcp_max_output_bytes if source == "mcp" else 200_000
        return self._truncate_output(result, max_bytes=max_bytes)

    @staticmethod
    def _call_signature(tool_name: str, args: dict[str, Any]) -> tuple[str, str]:
        sorted_args = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        return (tool_name.strip().lower(), sorted_args)

    @staticmethod
    def _truncate_output(result: dict[str, Any], *, max_bytes: int) -> dict[str, Any]:
        """Truncate 'content', 'text', 'report_markdown' fields that exceed max_bytes."""
        truncated = dict(result)
        for key in ("content", "text", "report_markdown", "result"):
            value = truncated.get(key)
            if isinstance(value, str) and len(value.encode("utf-8", errors="replace")) > max_bytes:
                encoded = value.encode("utf-8", errors="replace")
                tail_bytes = int(max_bytes * 0.2)
                truncated[key] = (
                    encoded[: max_bytes - tail_bytes].decode("utf-8", errors="replace")
                    + "\n\n[... truncated ...]\n\n"
                    + encoded[-tail_bytes:].decode("utf-8", errors="replace")
                )
                truncated["_truncated"] = True
        return truncated
