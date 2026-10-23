from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


CompanionEmotion = Literal["idle", "happy", "curious", "focused", "sleepy", "concerned"]
CompanionActivity = Literal["idle", "thinking", "waiting", "sleeping", "error"]
CompanionEventKind = Literal["pet", "wave", "feed", "rest", "wake"]
CompanionBackendId = Literal["api", "codex", "claude_code", "pi"]


class CompanionState(BaseModel):
    activity: CompanionActivity = "idle"
    emotion: CompanionEmotion = "idle"
    expression: str = "(=･ω･=)"
    updated_at: datetime | None = None
    last_run_id: str | None = None


class CompanionEmotionDefinition(BaseModel):
    id: CompanionEmotion
    label: str
    expression: str


class CompanionCapability(BaseModel):
    id: str
    label: str
    available: bool = True
    detail: str = ""


class CompanionRuntime(BaseModel):
    provider: str
    model: str
    demo_mode: bool


class CompanionBackend(BaseModel):
    id: CompanionBackendId
    label: str
    kind: Literal["api", "cli"]
    available: bool
    reason: str
    model: str | None = None
    demo_mode: bool = False


class CompanionProfile(BaseModel):
    name: str = "彩宝"
    species: str = "cat"
    color: str = "#8DDDC6"
    greeting: str = "嗯，我在。今天想一起做点什么？"
    state: CompanionState
    capabilities: list[CompanionCapability]
    emotions: list[CompanionEmotionDefinition]
    runtime: CompanionRuntime


class CompanionRunRequest(BaseModel):
    """Only companion-facing controls; identity and execution policy stay server-side."""

    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=4000)
    mode: Literal["chat", "agent"] = "agent"
    backend: CompanionBackendId = "api"
    conversation_id: str | None = Field(default=None, min_length=1, max_length=36)
    include_memory: bool = True
    model: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("task")
    @classmethod
    def _strip_task(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("task cannot be blank.")
        return value


class CompanionEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: CompanionEventKind


class CompanionEventResponse(BaseModel):
    event_id: str
    message: str
    state: CompanionState
