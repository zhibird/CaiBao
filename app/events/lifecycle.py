from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TurnStarted:
    run_id: str
    team_id: str
    user_id: str
    conversation_id: str | None
    space_id: str | None
    task: str
    channel: str = "agent"
    timestamp: str = field(default_factory=_now)


@dataclass
class RetrievalCompleted:
    run_id: str
    query: str
    rewritten_query: str | None = None
    hyde_query: str | None = None
    hit_count: int = 0
    injected_count: int = 0
    trace: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    timestamp: str = field(default_factory=_now)


@dataclass
class ToolCalled:
    run_id: str
    step_index: int
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=_now)


@dataclass
class ToolResulted:
    run_id: str
    step_index: int
    tool_name: str
    status: str = "completed"  # "completed" | "failed" | "skipped"
    result_preview: str | None = None
    error: str | None = None
    timestamp: str = field(default_factory=_now)


@dataclass
class StepCompleted:
    run_id: str
    step_index: int
    step_type: str  # "plan" | "retrieval" | "tool_call" | "final"
    status: str = "completed"
    latency_ms: int | None = None
    timestamp: str = field(default_factory=_now)


@dataclass
class TurnCommitted:
    run_id: str
    team_id: str
    user_id: str
    conversation_id: str | None
    space_id: str | None
    input_message: str
    assistant_response: str
    tools_used: list[str] = field(default_factory=list)
    timestamp: str = field(default_factory=_now)


@dataclass
class MemoryUpdated:
    team_id: str
    user_id: str
    space_id: str | None
    source_ref: str
    files: list[str] = field(default_factory=list)
    card_ids: list[str] = field(default_factory=list)
    summary: str = ""
    timestamp: str = field(default_factory=_now)


@dataclass
class ConsolidationCommitted:
    team_id: str
    user_id: str
    space_id: str | None
    source_ref: str
    history_entries: list[dict[str, Any]] = field(default_factory=list)
    pending_items: list[dict[str, Any]] = field(default_factory=list)
    timestamp: str = field(default_factory=_now)
