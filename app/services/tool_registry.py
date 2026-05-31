from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

try:
    import jsonschema as _jsonschema_lib
except ImportError:
    _jsonschema_lib = None


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    display_name: str
    description: str
    dangerous: bool
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    handler_key: str
    enabled: bool = True
    permission_scope: str = "team"
    source: str = "builtin"  # "builtin" | "generic" | "mcp"
    provider: str = ""       # "web_tools" | "file_tools" | "shell_tools" | mcp server name

    @property
    def parameters(self) -> dict[str, Any]:
        return self.input_schema

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "dangerous": self.dangerous,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "handler_key": self.handler_key,
            "enabled": self.enabled,
            "permission_scope": self.permission_scope,
            "source": self.source,
            "provider": self.provider,
            "parameters": self.input_schema,
        }

    def to_openai_tool(self) -> dict[str, Any]:
        """Map to OpenAI-compatible function calling tool definition."""
        schema = copy.deepcopy(self.input_schema)
        # OpenAI requires type: "object" at the top level
        if not isinstance(schema, dict) or schema.get("type") != "object":
            schema = {"type": "object", "properties": {}, **schema}
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": schema,
            },
        }

    def validate_arguments(self, raw_args: dict[str, Any]) -> list[str]:
        """Validate arguments against input_schema. Returns list of error strings."""
        if _jsonschema_lib is None:
            return []
        errors: list[str] = []
        try:
            validator_cls = _jsonschema_lib.validators.validator_for(self.input_schema)
            validator = validator_cls(self.input_schema)
            for err in validator.iter_errors(raw_args):
                path = ".".join(str(p) for p in err.absolute_path) if err.absolute_path else "<root>"
                errors.append(f"{path}: {err.message}")
        except Exception:
            pass
        return errors

    def normalize_arguments(self, raw_args: dict[str, Any]) -> dict[str, Any]:
        """Normalize and fill defaults according to the schema."""
        args = dict(raw_args)
        properties = self.input_schema.get("properties", {}) if isinstance(self.input_schema, dict) else {}
        for prop_name, prop_schema in properties.items():
            if not isinstance(prop_schema, dict):
                continue
            if "default" in prop_schema and prop_name not in args:
                args[prop_name] = prop_schema["default"]
        return args


AGENT_TOOL_DEFINITIONS = {
    "search_knowledge": ToolDefinition(
        name="search_knowledge",
        display_name="搜索知识库",
        description="Search workspace knowledge documents with a lightweight keyword fallback.",
        dangerous=False,
        handler_key="builtin.search_knowledge",
        permission_scope="space",
        input_schema={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 1000},
                "space_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "matches": {"type": "array"},
                "message": {"type": "string"},
            },
        },
    ),
    "list_recent_documents": ToolDefinition(
        name="list_recent_documents",
        display_name="最近文档",
        description="List recently imported documents in the current team/workspace.",
        dangerous=False,
        handler_key="builtin.list_recent_documents",
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                "space_id": {"type": "string"},
            },
        },
        output_schema={"type": "object", "properties": {"documents": {"type": "array"}}},
    ),
    "create_incident": ToolDefinition(
        name="create_incident",
        display_name="创建 Incident",
        description="Create an operational incident record. Requires explicit confirmation in agent mode.",
        dangerous=True,
        handler_key="builtin.create_incident",
        input_schema={
            "type": "object",
            "required": ["title"],
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 255},
                "severity": {"type": "string", "enum": ["P1", "P2", "P3"], "default": "P2"},
            },
        },
        output_schema={"type": "object", "properties": {"incident": {"type": "object"}}},
    ),
    "create_memory_card": ToolDefinition(
        name="create_memory_card",
        display_name="沉淀记忆卡",
        description="Create a long-term memory card in the active workspace.",
        dangerous=True,
        handler_key="builtin.create_memory_card",
        permission_scope="space",
        input_schema={
            "type": "object",
            "required": ["space_id", "title", "content"],
            "properties": {
                "space_id": {"type": "string"},
                "title": {"type": "string", "minLength": 1, "maxLength": 128},
                "content": {"type": "string", "minLength": 1, "maxLength": 4000},
                "category": {"type": "string", "default": "agent"},
            },
        },
        output_schema={"type": "object", "properties": {"memory": {"type": "object"}}},
    ),
    "promote_to_conclusion": ToolDefinition(
        name="promote_to_conclusion",
        display_name="沉淀结论",
        description="Create a structured conclusion record from the agent result.",
        dangerous=True,
        handler_key="builtin.promote_to_conclusion",
        permission_scope="space",
        input_schema={
            "type": "object",
            "required": ["space_id", "title", "content"],
            "properties": {
                "space_id": {"type": "string"},
                "title": {"type": "string", "minLength": 1, "maxLength": 128},
                "content": {"type": "string", "minLength": 1, "maxLength": 12000},
                "topic": {"type": "string", "default": "agent"},
                "status": {"type": "string", "enum": ["draft", "confirmed", "effective"], "default": "draft"},
            },
        },
        output_schema={"type": "object", "properties": {"conclusion": {"type": "object"}}},
    ),
    "forget_memory": ToolDefinition(
        name="forget_memory",
        display_name="删除记忆",
        description=(
            "Delete a memory card by its memory_id. "
            "Use this to remove outdated, incorrect, or irrelevant memories. "
            "The memory_id can be found from recall_memory results."
        ),
        dangerous=True,
        handler_key="builtin.forget_memory",
        permission_scope="space",
        input_schema={
            "type": "object",
            "required": ["memory_id"],
            "properties": {
                "memory_id": {"type": "string", "minLength": 1, "maxLength": 36},
            },
        },
        output_schema={"type": "object", "properties": {"message": {"type": "string"}}},
    ),
    "recall_memory": ToolDefinition(
        name="recall_memory",
        display_name="检索记忆",
        description=(
            "Semantically search the workspace memory cards by query text. "
            "Returns memory cards ranked by relevance. Useful for recalling "
            "past decisions, user preferences, or previously stored facts. "
            "Use this before answering questions that may have been addressed before."
        ),
        dangerous=False,
        handler_key="builtin.recall_memory",
        permission_scope="space",
        input_schema={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 1000},
                "space_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "memories": {"type": "array"},
                "query": {"type": "string"},
                "count": {"type": "integer"},
            },
        },
    ),
    "message_lookup": ToolDefinition(
        name="message_lookup",
        display_name="查看聊天记录",
        description=(
            "Look up recent chat messages in the current conversation. "
            "Use this when you need context from earlier in the conversation "
            "or when the user asks about something previously discussed."
        ),
        dangerous=False,
        handler_key="builtin.message_lookup",
        permission_scope="team",
        input_schema={
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "messages": {"type": "array"},
                "count": {"type": "integer"},
            },
        },
    ),
    "generate_incident_report": ToolDefinition(
        name="generate_incident_report",
        display_name="生成事件报告",
        description="Generate a Markdown incident report from task context and tool observations.",
        dangerous=False,
        handler_key="builtin.generate_incident_report",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "default": "Incident Report"},
                "incident_summary": {"type": "string"},
                "findings": {"type": "array", "items": {"type": "string"}},
                "recommendations": {"type": "array", "items": {"type": "string"}},
            },
        },
        output_schema={"type": "object", "properties": {"report_markdown": {"type": "string"}}},
    ),
}


def get_tool_definition(name: str) -> ToolDefinition | None:
    return AGENT_TOOL_DEFINITIONS.get(name.strip().lower())


def list_tool_definitions() -> list[ToolDefinition]:
    return list(AGENT_TOOL_DEFINITIONS.values())


def get_openai_tools(enabled_tools: list[str] | None = None) -> list[dict[str, Any]]:
    """Return OpenAI-compatible tool definitions for enabled tools."""
    definitions = list_tool_definitions()
    if enabled_tools:
        allowed = {t.strip().lower() for t in enabled_tools if t.strip()}
        definitions = [d for d in definitions if d.name.lower() in allowed]
    return [d.to_openai_tool() for d in definitions if d.enabled]
