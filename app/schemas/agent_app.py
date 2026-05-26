from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.agent import AgentRunResponse


def _sanitize_llm_routing(value: Any) -> dict[str, Any] | None:
    """Strip raw api_key/base_url from llm_routing; only model-name refs are accepted."""
    if value is None:
        return None
    if not isinstance(value, dict):
        return None
    sanitized: dict[str, Any] = {}
    for role in ("planner", "fast", "vision"):
        entry = value.get(role)
        if entry is None:
            sanitized[role] = None
        elif isinstance(entry, str):
            sanitized[role] = entry.strip() if entry.strip() else None
        elif isinstance(entry, dict):
            model_ref = str(entry.get("model_name", "")).strip()
            sanitized[role] = model_ref if model_ref else None
        else:
            sanitized[role] = None
    return sanitized


class AgentAppCreate(BaseModel):
    team_id: str | None = Field(default=None, min_length=1, max_length=64)
    user_id: str | None = Field(default=None, min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=4000)
    mode: str = Field(default="agent_auto", min_length=1, max_length=32)
    system_prompt: str = Field(default="", max_length=12000)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    embedding_model: str | None = Field(default=None, min_length=1, max_length=128)
    space_id: str | None = Field(default=None, min_length=1, max_length=36)
    retrieval_config: dict[str, Any] = Field(default_factory=dict)
    tool_config: dict[str, Any] = Field(default_factory=dict)
    workflow_config: dict[str, Any] = Field(default_factory=dict)
    llm_routing: dict[str, Any] = Field(default_factory=dict)
    status: str = Field(default="draft", min_length=1, max_length=32)

    @field_validator("llm_routing", mode="before")
    @classmethod
    def _sanitize_llm_routing(cls, value: Any) -> dict[str, Any] | None:
        return _sanitize_llm_routing(value)


class AgentAppUpdate(BaseModel):
    team_id: str | None = Field(default=None, min_length=1, max_length=64)
    user_id: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=4000)
    mode: str | None = Field(default=None, min_length=1, max_length=32)
    system_prompt: str | None = Field(default=None, max_length=12000)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    embedding_model: str | None = Field(default=None, min_length=1, max_length=128)
    space_id: str | None = Field(default=None, min_length=1, max_length=36)
    retrieval_config: dict[str, Any] | None = None
    tool_config: dict[str, Any] | None = None
    workflow_config: dict[str, Any] | None = None
    llm_routing: dict[str, Any] | None = None
    status: str | None = Field(default=None, min_length=1, max_length=32)

    @field_validator("llm_routing", mode="before")
    @classmethod
    def _sanitize_llm_routing(cls, value: Any) -> dict[str, Any] | None:
        return _sanitize_llm_routing(value)


class AgentAppPublishRequest(BaseModel):
    team_id: str | None = Field(default=None, min_length=1, max_length=64)
    user_id: str | None = Field(default=None, min_length=1, max_length=64)
    notes: str = Field(default="", max_length=4000)


class AgentAppInvokeRequest(BaseModel):
    team_id: str | None = Field(default=None, min_length=1, max_length=64)
    user_id: str | None = Field(default=None, min_length=1, max_length=64)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=36)
    space_id: str | None = Field(default=None, min_length=1, max_length=36)
    task: str = Field(min_length=1, max_length=4000)
    top_k: int | None = Field(default=None, ge=1, le=20)
    selected_document_ids: list[str] | None = None
    include_memory: bool | None = None
    include_library: bool | None = None
    include_conclusions: bool | None = None
    dry_run: bool = False
    confirm_dangerous_actions: bool = False
    max_steps: int | None = Field(default=None, ge=3, le=10)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    embedding_model: str | None = Field(default=None, min_length=1, max_length=128)
    inputs: dict[str, Any] = Field(default_factory=dict)


class AgentAppVersionResponse(BaseModel):
    version_id: str
    app_id: str
    team_id: str
    version_number: int
    snapshot: dict[str, Any]
    notes: str
    created_by_user_id: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_record(cls, record: Any) -> "AgentAppVersionResponse":
        return cls(
            version_id=record.version_id,
            app_id=record.app_id,
            team_id=record.team_id,
            version_number=record.version_number,
            snapshot=_safe_json_loads(record.snapshot_json),
            notes=record.notes,
            created_by_user_id=record.created_by_user_id,
            created_at=record.created_at,
        )


class AgentAppResponse(BaseModel):
    app_id: str
    team_id: str
    created_by_user_id: str
    name: str
    description: str
    mode: str
    system_prompt: str
    model: str | None
    embedding_model: str | None
    space_id: str | None
    retrieval_config: dict[str, Any]
    tool_config: dict[str, Any]
    workflow_config: dict[str, Any]
    llm_routing: dict[str, Any] = Field(default_factory=dict)
    status: str
    app_version: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_record(cls, record: Any) -> "AgentAppResponse":
        return cls(
            app_id=record.app_id,
            team_id=record.team_id,
            created_by_user_id=record.created_by_user_id,
            name=record.name,
            description=record.description,
            mode=record.mode,
            system_prompt=record.system_prompt,
            model=record.model,
            embedding_model=record.embedding_model,
            space_id=record.space_id,
            retrieval_config=_safe_json_loads(record.retrieval_config_json),
            tool_config=_safe_json_loads(record.tool_config_json),
            workflow_config=_safe_json_loads(record.workflow_config_json),
            llm_routing=_sanitize_llm_routing(_safe_json_loads(record.llm_routing_json)) or {},
            status=record.status,
            app_version=record.app_version,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class AgentAppListResponse(BaseModel):
    items: list[AgentAppResponse]


class AgentAppInvokeResponse(AgentRunResponse):
    app_id: str | None = None
    app_version: int | None = None


def _safe_json_loads(raw: str) -> dict[str, Any]:
    try:
        decoded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}
