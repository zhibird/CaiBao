from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.chat import ChatSource
from app.services.execution_ledger import ExecutionStopReason


class AgentRunRequest(BaseModel):
    client_request_id: str | None = Field(default=None, min_length=1, max_length=64)
    user_id: str | None = Field(default=None, min_length=1, max_length=64)
    team_id: str | None = Field(default=None, min_length=1, max_length=64)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=36)
    space_id: str | None = Field(default=None, min_length=1, max_length=36)
    app_id: str | None = Field(default=None, min_length=1, max_length=36)
    app_version: int | None = Field(default=None, ge=0)
    trigger_channel: str = Field(default="agent", min_length=1, max_length=32)
    run_mode: str = Field(default="agent_auto", min_length=1, max_length=32)
    system_prompt: str | None = Field(default=None, max_length=12000)
    task: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=20)
    selected_document_ids: list[str] | None = None
    include_memory: bool = True
    include_library: bool = True
    include_conclusions: bool = False
    dry_run: bool = False
    confirm_dangerous_actions: bool = False
    max_steps: int = Field(default=5, ge=3, le=10)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    embedding_model: str | None = Field(default=None, min_length=1, max_length=128)
    enabled_tools: list[str] | None = None
    workflow_config: dict[str, Any] = Field(default_factory=dict)
    model_routes: dict[str, Any] | None = None

    @field_validator("model_routes", mode="before")
    @classmethod
    def _sanitize_model_routes(cls, value: Any) -> dict[str, Any] | None:
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
                # Normalize {"model_name": "foo"} to plain string "foo" so
                # LLMRouter._parse_route_value resolves user-configured models.
                model_ref = str(entry.get("model_name", "")).strip()
                sanitized[role] = model_ref if model_ref else None
            else:
                sanitized[role] = None
        return sanitized


class AgentConfirmRequest(BaseModel):
    approval_token: str | None = Field(default=None, min_length=1, max_length=64)
    user_id: str | None = Field(default=None, min_length=1, max_length=64)
    team_id: str | None = Field(default=None, min_length=1, max_length=64)


class AgentToolDefinition(BaseModel):
    name: str
    display_name: str | None = None
    description: str
    dangerous: bool
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    handler_key: str | None = None
    enabled: bool = True
    permission_scope: str = "team"
    source: str = "builtin"
    provider: str = ""
    requires_confirmation_by_default: bool = False
    parameters: dict[str, Any]


class AgentToolCall(BaseModel):
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    dangerous: bool = False
    requires_confirmation: bool = False


class AgentStepResponse(BaseModel):
    step_id: str
    run_id: str
    step_index: int
    step_type: str
    title: str
    status: str
    tool_name: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None
    latency_ms: int | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_record(cls, record: Any) -> "AgentStepResponse":
        return cls(
            step_id=record.step_id,
            run_id=record.run_id,
            step_index=record.step_index,
            step_type=record.step_type,
            title=record.title,
            status=record.status,
            tool_name=record.tool_name,
            input=_safe_json_loads(record.input_json),
            output=_safe_json_loads(record.output_json),
            error_message=record.error_message,
            latency_ms=record.latency_ms,
            created_at=record.created_at,
        )


class AgentExecutionReceipt(BaseModel):
    """Public execution facts only; checkpoints and raw errors are private."""

    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1]
    source: Literal["api_tool_loop"]
    tool_budget: int = Field(ge=1, le=10)
    tool_calls_used: int = Field(ge=0, le=10)
    tool_calls_succeeded: int = Field(ge=0, le=10)
    tool_calls_failed: int = Field(ge=0, le=10)
    tool_calls_rejected: int = Field(ge=0, le=10)
    tool_calls_skipped: int = Field(ge=0, le=10)
    pending_tool_calls: int = Field(ge=0, le=10)
    omitted_tool_calls: int = Field(ge=0)
    stop_reason: ExecutionStopReason
    incomplete: bool

    @classmethod
    def from_snapshot(cls, raw: str | None, *, run_status: str) -> "AgentExecutionReceipt | None":
        """Read a public receipt safely and overlay authoritative terminal state.

        A late worker must not turn cancellation/interruption into success. A
        worker failure outside the tool loop may leave an in-progress receipt;
        retain its counters but make that incomplete outcome explicit.
        """
        if not isinstance(raw, str) or not raw:
            return None
        try:
            receipt = cls.model_validate(json.loads(raw))
        except (ValueError, TypeError, RecursionError):
            return None
        if run_status in {"cancelled", "interrupted"}:
            return receipt.model_copy(update={"stop_reason": run_status, "incomplete": True})
        if run_status == "failed" and (
            receipt.stop_reason in {"in_progress", "waiting_confirmation"} or not receipt.incomplete
        ):
            return receipt.model_copy(update={"stop_reason": "execution_error", "incomplete": True})
        return receipt

    @field_validator("version", mode="before")
    @classmethod
    def _strict_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Receipt version must be an integer.")
        return value

    @model_validator(mode="after")
    def _validate_accounting(self) -> "AgentExecutionReceipt":
        resolved = self.tool_calls_succeeded + self.tool_calls_failed + self.tool_calls_rejected + self.tool_calls_skipped
        if self.tool_calls_used > self.tool_budget or self.tool_calls_used != resolved + self.pending_tool_calls:
            raise ValueError("Execution receipt counts are inconsistent.")
        incomplete = bool(
            self.tool_calls_failed or self.tool_calls_rejected or self.pending_tool_calls or self.omitted_tool_calls
            or self.stop_reason not in {"model_finished", "tool_budget"}
        )
        if self.incomplete != incomplete:
            raise ValueError("Execution receipt incomplete flag is inconsistent.")
        return self


class AgentRunResponse(BaseModel):
    run_id: str
    client_request_id: str | None = None
    approval_token: str | None = None
    team_id: str
    user_id: str
    conversation_id: str | None
    space_id: str | None
    app_id: str | None = None
    app_version: int | None = None
    trigger_channel: str = "agent"
    task: str
    status: str
    answer: str
    model: str | None = None
    dry_run: bool
    max_steps: int
    required_confirmations: list[AgentToolCall] = Field(default_factory=list)
    steps: list[AgentStepResponse] = Field(default_factory=list)
    tool_calls: list[AgentToolCall] = Field(default_factory=list)
    sources: list[ChatSource] = Field(default_factory=list)
    latency_ms: int | None = None
    created_at: datetime
    completed_at: datetime | None = None
    model_routes: dict[str, Any] | None = None
    execution_identity: dict[str, Any] | None = None
    context_snapshot: dict[str, Any] | None = None
    execution_receipt: AgentExecutionReceipt | None = None


class AgentRunListResponse(BaseModel):
    items: list[AgentRunResponse]


class AgentRunEventsResponse(BaseModel):
    items: list["AgentStreamEvent"]
    next_seq: int = 0
    status: str
    has_more: bool = False


class AgentRunStartResponse(BaseModel):
    run_id: str
    stream_url: str
    status: str


class AgentStreamEvent(BaseModel):
    event: str
    run_id: str
    step_index: int | None = None
    seq: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)


class LLMCompletionResult(BaseModel):
    assistant_text: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    finish_reason: str | None = None
    raw_message: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    model: str | None = None


class LLMStreamChunk(BaseModel):
    delta_text: str | None = None
    tool_call_deltas: list[dict[str, Any]] = Field(default_factory=list)
    finish_reason: str | None = None


class ModelRoute(BaseModel):
    model_name: str
    base_url: str
    api_key: str | None = Field(default=None, exclude=True)
    source: str = "default"
    capabilities: dict[str, Any] = Field(default_factory=dict)


class LLMRoutesConfig(BaseModel):
    planner: ModelRoute
    fast: ModelRoute | None = None
    vision: ModelRoute | None = None


def _safe_json_loads(raw: str) -> dict[str, Any]:
    try:
        decoded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}
