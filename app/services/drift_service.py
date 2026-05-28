from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.core.config import get_settings
from app.services.drift_state_store import DriftStateStore
from app.services.drift_tools import (
    drift_edit_work_file,
    drift_finish_drift,
    drift_list_work_files,
    drift_message_push,
    drift_read_work_file,
    drift_write_work_file,
    get_drift_state,
    new_drift_run,
)
from uuid import uuid4 as _uuid4

_logger = logging.getLogger(__name__)

_MAX_DRIFT_STEPS = 8


class DriftService:
    """Runs a single drift skill execution against the LLM."""

    def __init__(
        self,
        store: DriftStateStore,
        llm_service=None,      # LLMService
        tool_service_factory=None,  # callable(team_id, user_id) -> ToolService
    ) -> None:
        self.store = store
        self._llm = llm_service
        self._tool_factory = tool_service_factory

    def run_drift(
        self,
        *,
        skill_name: str,
        team_id: str,
        user_id: str,
        space_id: str | None = None,
        model_name: str = "",
        base_url: str = "",
        api_key: str = "",
    ) -> dict[str, Any]:
        skill = self.store.get_skill(team_id, user_id, skill_name)
        if skill is None:
            return {"status": "error", "error": f"Skill '{skill_name}' not found"}

        max_steps = int(skill.get("max_steps", _MAX_DRIFT_STEPS))
        description = skill.get("description", skill_name)
        skill_body = self._read_skill_body(team_id, user_id, skill_name)

        run_token = new_drift_run(str(_uuid4())[:12])
        st = get_drift_state()

        # Build tool list
        tool_names = skill.get("allowed_tools", [])
        if isinstance(tool_names, str):
            tool_names = [t.strip() for t in tool_names.split(",")]
        if not tool_names:
            tool_names = ["read_work_file", "write_work_file", "list_work_files", "finish_drift"]

        system = (
            f"You are a background drift task executor. Your task: {description}\n\n"
            "Rules:\n"
            "- You MUST call finish_drift exactly once at the end of your work.\n"
            "- message_push may be called at most once per run.\n"
            "- After message_push, you may only write/edit work files and call finish_drift.\n"
            "- Work files are in a shared workspace. Read before making changes.\n"
            "- If you cannot complete within the step limit, call finish_drift(decision='incomplete').\n\n"
            f"Skill instructions:\n{skill_body[:4000]}"
        )

        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Execute drift skill: {skill_name}"},
        ]

        drift_tools = self._build_drift_tools(tool_names, team_id, user_id, skill_name)

        current_model = model_name or get_settings().llm_model or ""
        current_base = base_url or get_settings().llm_base_url or ""
        current_key = api_key or get_settings().llm_api_key or ""

        for step in range(max_steps):
            st["step_count"] = step + 1

            try:
                result = self._llm.complete_chat(
                    messages=messages,
                    model=current_model,
                    base_url=current_base,
                    api_key=current_key,
                    tools=drift_tools,
                )
            except Exception as exc:
                _logger.exception("Drift LLM call failed at step %d", step + 1)
                return {"status": "error", "error": str(exc), "steps": step + 1}

            if result.tool_calls:
                for tc in result.tool_calls:
                    fn = tc.get("function", {})
                    t_name = fn.get("name", "")
                    args = fn.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    if not isinstance(args, dict):
                        args = {}

                    tool_result = self._dispatch_drift_tool(t_name, args, team_id, user_id, skill_name)
                    messages.append({"role": "assistant", "content": None, "tool_calls": [tc]})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": str(tc.get("id", "")),
                        "content": json.dumps(tool_result, ensure_ascii=False, default=str),
                    })

                    if st.get("finished"):
                        break
            else:
                # No tool calls — LLM done
                if not st.get("finished"):
                    st["finished"] = True
                    st["decision"] = "complete"
                    st["message_result"] = "no push"
                break

            if st.get("finished"):
                break

        # Final state handling
        if not st.get("finished"):
            st["decision"] = "incomplete"
            st["message_result"] = "no push (max steps reached)"

        # Persist state
        self.store.update_state(team_id, user_id, skill_name, {
            "last_run": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "decision": st.get("decision", "unknown"),
            "message_pushed": st.get("message_pushed", False),
            "message_result": st.get("message_result", ""),
            "draft_message": st.get("draft_message", ""),
            "steps": st.get("step_count", 0),
        })

        return {
            "status": "completed",
            "skill_name": skill_name,
            "decision": st.get("decision"),
            "message_pushed": st.get("message_pushed", False),
            "steps": st.get("step_count", 0),
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _read_skill_body(self, team_id: str, user_id: str, skill_name: str) -> str:
        import re
        skill_file = self.store._skills_dir(team_id, user_id) / skill_name / "SKILL.md"
        if not skill_file.exists():
            return ""
        text = skill_file.read_text("utf-8")
        # Strip YAML frontmatter
        text = re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, flags=re.DOTALL)
        return text.strip()

    def _build_drift_tools(self, tool_names: list[str], team_id: str, user_id: str, skill_name: str) -> list[dict]:
        from app.services.tool_registry import ToolDefinition

        registry = {
            "read_work_file": ToolDefinition(
                name="read_work_file", display_name="Read work file",
                description="Read a file from the drift workspace.",
                dangerous=False, source="builtin",
                handler_key="drift.read_work_file",
                permission_scope="team",
                input_schema={"type": "object", "required": ["filename"], "properties": {"filename": {"type": "string"}}},
                output_schema={"type": "object"},
            ),
            "write_work_file": ToolDefinition(
                name="write_work_file", display_name="Write work file",
                description="Write (overwrite) a file in the drift workspace.",
                dangerous=True, source="builtin",
                handler_key="drift.write_work_file",
                permission_scope="team",
                input_schema={"type": "object", "required": ["filename", "content"], "properties": {"filename": {"type": "string"}, "content": {"type": "string"}}},
                output_schema={"type": "object"},
            ),
            "edit_work_file": ToolDefinition(
                name="edit_work_file", display_name="Edit work file",
                description="Replace text in a work file.",
                dangerous=True, source="builtin",
                handler_key="drift.edit_work_file",
                permission_scope="team",
                input_schema={"type": "object", "required": ["filename", "old_text", "new_text"], "properties": {"filename": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}, "replace_all": {"type": "boolean", "default": False}}},
                output_schema={"type": "object"},
            ),
            "list_work_files": ToolDefinition(
                name="list_work_files", display_name="List work files",
                description="List files in the drift workspace.",
                dangerous=False, source="builtin",
                handler_key="drift.list_work_files",
                permission_scope="team",
                input_schema={"type": "object", "properties": {}},
                output_schema={"type": "object"},
            ),
            "message_push": ToolDefinition(
                name="message_push", display_name="Push message",
                description="Stage a draft message for user notification. Max once per drift run.",
                dangerous=True, source="builtin",
                handler_key="drift.message_push",
                permission_scope="team",
                input_schema={"type": "object", "required": ["message"], "properties": {"message": {"type": "string"}}},
                output_schema={"type": "object"},
            ),
            "finish_drift": ToolDefinition(
                name="finish_drift", display_name="Finish drift",
                description="End the drift run. Must be called exactly once per run.",
                dangerous=False, source="builtin",
                handler_key="drift.finish_drift",
                permission_scope="team",
                input_schema={"type": "object", "required": ["decision"], "properties": {"decision": {"type": "string", "enum": ["complete", "incomplete", "reply"]}, "message_result": {"type": "string", "default": ""}}},
                output_schema={"type": "object"},
            ),
        }

        tools = []
        for name in tool_names:
            name = name.strip()
            if name in registry:
                tools.append(registry[name].to_openai_tool())
        return tools

    def _dispatch_drift_tool(self, tool_name: str, args: dict, team_id: str, user_id: str, skill_name: str) -> dict:
        store = self.store
        dispatch = {
            "read_work_file": lambda: drift_read_work_file(team_id=team_id, user_id=user_id, arguments=args, store=store, skill_name=skill_name),
            "write_work_file": lambda: drift_write_work_file(team_id=team_id, user_id=user_id, arguments=args, store=store, skill_name=skill_name),
            "edit_work_file": lambda: drift_edit_work_file(team_id=team_id, user_id=user_id, arguments=args, store=store, skill_name=skill_name),
            "list_work_files": lambda: drift_list_work_files(team_id=team_id, user_id=user_id, arguments=args, store=store, skill_name=skill_name),
            "message_push": lambda: drift_message_push(team_id=team_id, user_id=user_id, arguments=args),
            "finish_drift": lambda: drift_finish_drift(team_id=team_id, user_id=user_id, arguments=args),
        }
        handler = dispatch.get(tool_name)
        if handler is None:
            return {"error": f"Unknown drift tool: {tool_name}"}
        try:
            return handler()
        except Exception as exc:
            return {"error": str(exc)}
