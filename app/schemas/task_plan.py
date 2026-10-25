"""Bounded model-reported task metadata, separate from execution evidence."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


TaskPlanSource = Literal["api.update_plan", "codex.todo_list", "claude.TodoWrite", "claude.tasks"]
TaskPlanStepStatus = Literal["pending", "in_progress", "completed", "cancelled"]


class AgentTaskPlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=160)
    status: TaskPlanStepStatus

    @field_validator("id", "content", mode="before")
    @classmethod
    def _strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class AgentTaskPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1]
    revision: int = Field(ge=1, le=65)
    source: TaskPlanSource
    model_reported: Literal[True]
    steps: list[AgentTaskPlanStep] = Field(max_length=10)
    explanation: str | None = Field(default=None, max_length=300)
    limited: bool = False

    @field_validator("version", mode="before")
    @classmethod
    def _strict_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Task plan version must be an integer.")
        return value

    @field_validator("model_reported", mode="before")
    @classmethod
    def _strict_model_reported(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Task plans must retain model-reported provenance.")
        return value

    @field_validator("explanation", mode="before")
    @classmethod
    def _strip_explanation(cls, value: object) -> object:
        return (value.strip() or None) if isinstance(value, str) else value

    @model_validator(mode="after")
    def _validate_steps(self) -> "AgentTaskPlan":
        if len({step.id for step in self.steps}) != len(self.steps):
            raise ValueError("Task plan step IDs must be unique.")
        if sum(step.status == "in_progress" for step in self.steps) > 1:
            raise ValueError("A task plan may have at most one step in progress.")
        if self.limited != (self.revision == 65):
            raise ValueError("A limited task plan must be its final bounded revision.")
        return self

    @classmethod
    def from_snapshot(cls, raw: str | None) -> "AgentTaskPlan | None":
        # Even escaped Unicode at all schema bounds fits comfortably below this
        # ceiling. Reject oversized/corrupt DB values before decoding them.
        if not isinstance(raw, str) or not raw or len(raw) > 64_000:
            return None
        try:
            return cls.model_validate(json.loads(raw))
        except (ValueError, TypeError, RecursionError):
            return None
