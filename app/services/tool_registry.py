from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
            "parameters": self.input_schema,
        }


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
