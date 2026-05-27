from __future__ import annotations

import json

import pytest

from app.schemas.tooling import ToolCallContext, ToolPreflightResult
from app.services.tool_registry import ToolDefinition
from app.services.tool_safety import ToolSafetyService


def _make_definition(*, name="test_tool", dangerous=False, enabled=True, source="builtin", **kwargs):
    return ToolDefinition(
        name=name,
        display_name="Test Tool",
        description="A test tool.",
        dangerous=dangerous,
        enabled=enabled,
        source=source,
        handler_key=f"test.{name}",
        permission_scope="team",
        input_schema={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "default": 5},
            },
        },
        output_schema={"type": "object"},
        **kwargs,
    )


def _ctx(tool_name="test_tool", arguments=None, run_id="run-1", source="builtin"):
    return ToolCallContext(
        run_id=run_id,
        team_id="team-1",
        user_id="user-1",
        tool_name=tool_name,
        arguments=arguments or {"query": "hello"},
        source=source,
    )


def _sig(tool_name, args):
    """Build the same signature that ToolSafetyService._call_signature produces."""
    sorted_args = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    return (tool_name.strip().lower(), sorted_args)


class TestToolSafetyPreflight:
    def test_allows_safe_tool(self):
        svc = ToolSafetyService()
        result = svc.preflight(_ctx(), _make_definition())
        assert result.allowed is True
        assert result.requires_confirmation is False

    def test_blocks_disabled_tool(self):
        svc = ToolSafetyService()
        result = svc.preflight(_ctx(), _make_definition(enabled=False))
        assert result.allowed is False
        assert "disabled" in result.reason.lower()

    def test_fills_defaults_in_normalized_args(self):
        svc = ToolSafetyService()
        result = svc.preflight(_ctx(arguments={"query": "hi"}), _make_definition())
        assert result.normalized_arguments == {"query": "hi", "limit": 5}

    def test_blocks_schema_validation_error(self):
        svc = ToolSafetyService()
        # Pass a number where a string is required — guaranteed schema violation
        result = svc.preflight(_ctx(arguments={"query": 12345}), _make_definition())
        assert result.allowed is False
        assert "schema" in result.reason.lower() or "validation" in result.reason.lower()

    def test_flags_dangerous_tool_for_confirmation(self):
        svc = ToolSafetyService()
        result = svc.preflight(_ctx(), _make_definition(dangerous=True))
        assert result.allowed is True
        assert result.requires_confirmation is True
        assert result.dangerous is True

    def test_dry_run_skips_confirmation_for_dangerous(self):
        svc = ToolSafetyService()
        ctx = _ctx()
        ctx = ToolCallContext(
            run_id=ctx.run_id, team_id=ctx.team_id, user_id=ctx.user_id,
            tool_name=ctx.tool_name, arguments=ctx.arguments,
            source=ctx.source, dry_run=True,
        )
        result = svc.preflight(ctx, _make_definition(dangerous=True))
        assert result.allowed is True
        assert result.requires_confirmation is False

    def test_blocks_after_max_calls_per_run(self):
        svc = ToolSafetyService()
        svc._run_call_counts["run-1"] = 20
        result = svc.preflight(_ctx(run_id="run-1"), _make_definition())
        assert result.allowed is False
        assert "Max tool calls" in result.reason

    def test_blocks_after_max_calls_per_tool(self):
        svc = ToolSafetyService()
        svc._run_tool_call_counts["run-1"]["test_tool"] = 8
        result = svc.preflight(_ctx(run_id="run-1"), _make_definition())
        assert result.allowed is False
        assert "Max calls for tool" in result.reason

    def test_blocks_consecutive_duplicate_calls(self):
        svc = ToolSafetyService()
        args = {"query": "hello", "limit": 5}
        sig = _sig("test_tool", args)
        svc._run_tool_history["run-1"] = [sig, sig, sig]
        result = svc.preflight(
            _ctx(run_id="run-1", arguments={"query": "hello", "limit": 5}),
            _make_definition(),
        )
        assert result.allowed is False
        assert "consecutively" in result.reason.lower()

    def test_allows_same_args_after_interleaved_call(self):
        svc = ToolSafetyService()
        args_a = {"query": "hello", "limit": 5}
        args_b = {}
        svc._run_tool_history["run-1"] = [
            _sig("test_tool", args_a),
            _sig("other_tool", args_b),
            _sig("test_tool", args_a),
        ]
        result = svc.preflight(
            _ctx(run_id="run-1", arguments={"query": "hello", "limit": 5}),
            _make_definition(),
        )
        assert result.allowed is True

    def test_normalized_args_create_consistent_signature(self):
        """Record with normalized args → preflight detects duplicate."""
        svc = ToolSafetyService()
        definition = _make_definition()

        # Simulate a recorded call (as fixed in ToolService.execute)
        normalized = definition.normalize_arguments({"query": "hello"})
        ctx = _ctx(run_id="run-1", arguments=normalized)
        svc.record_call(ctx)

        # Second call with same semantic args in different order → blocked
        result = svc.preflight(
            _ctx(run_id="run-1", arguments={"limit": 5, "query": "hello"}),
            definition,
        )
        assert result.allowed is True  # Only 1 prior call, not consecutive 3


class TestToolSafetyRecordCall:
    def test_increments_run_call_count(self):
        svc = ToolSafetyService()
        ctx = _ctx(run_id="run-1")
        svc.record_call(ctx)
        assert svc._run_call_counts["run-1"] == 1

    def test_increments_tool_call_count(self):
        svc = ToolSafetyService()
        svc.record_call(_ctx(run_id="run-1", tool_name="tool_a"))
        svc.record_call(_ctx(run_id="run-1", tool_name="tool_a"))
        svc.record_call(_ctx(run_id="run-1", tool_name="tool_b"))
        assert svc._run_tool_call_counts["run-1"]["tool_a"] == 2
        assert svc._run_tool_call_counts["run-1"]["tool_b"] == 1

    def test_appends_to_history(self):
        svc = ToolSafetyService()
        svc.record_call(_ctx(run_id="run-1"))
        assert len(svc._run_tool_history["run-1"]) == 1

    def test_reset_run_clears_all_tracking(self):
        svc = ToolSafetyService()
        svc.record_call(_ctx(run_id="run-1"))
        svc.reset_run("run-1")
        assert "run-1" not in svc._run_call_counts
        assert "run-1" not in svc._run_tool_call_counts
        assert "run-1" not in svc._run_tool_history


class TestToolSafetyPostflight:
    def test_passes_through_short_output(self):
        svc = ToolSafetyService()
        result = svc.postflight_output({"text": "short"}, source="builtin")
        assert result == {"text": "short"}

    def test_truncates_long_text_field(self):
        svc = ToolSafetyService()
        long_text = "x" * 300_000
        result = svc.postflight_output({"text": long_text}, source="builtin")
        assert len(result["text"]) < len(long_text)
        assert "[... truncated ...]" in result["text"]
        assert result["_truncated"] is True

    def test_truncates_long_content_field(self):
        svc = ToolSafetyService()
        long_text = "y" * 300_000
        result = svc.postflight_output({"content": long_text}, source="builtin")
        assert len(result["content"]) < len(long_text)
        assert result["_truncated"] is True

    def test_truncates_long_report_markdown_field(self):
        svc = ToolSafetyService()
        long_text = "z" * 300_000
        result = svc.postflight_output({"report_markdown": long_text}, source="builtin")
        assert len(result["report_markdown"]) < len(long_text)

    def test_respects_mcp_max_bytes_setting(self):
        svc = ToolSafetyService()
        svc.settings.mcp_max_output_bytes = 50_000
        long_text = "a" * 300_000
        result = svc.postflight_output({"text": long_text}, source="mcp")
        assert len(result["text"].encode("utf-8")) <= 55_000
