"""Run-local planning metadata and model request limits, never action receipts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PLAN_TOOL_NAME = "update_plan"
MAX_PLAN_ATTEMPTS = 8
MAX_MODEL_CALLS = 15

PLAN_GUIDANCE = """For a task with three or more substantive steps, multiple deliverables, or an
explicit request to track progress, you may use update_plan to maintain short,
public work steps. Skip it for greetings, a single lookup, a direct answer, or
a request only to draft a plan. Send the complete list on each change. This is
model-reported planning metadata, not proof that an action succeeded. Do not
include private reasoning, hidden thoughts, credentials or tool arguments.
Keep unfinished steps unfinished; never check them off merely to end the turn.
There are at most 8 plan update attempts per run, including invalid or repeated
updates. They do not spend the separate external action budget. This tool loop
has at most 15 model calls, shared across approval continuations, including its
final summary. Separate retrieval work and transport retries are not included."""


class PlanStepArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    step: str = Field(min_length=1, max_length=160)
    status: Literal["pending", "in_progress", "completed", "cancelled"]

    @field_validator("step")
    @classmethod
    def _clean_step(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A public step cannot be blank.")
        return value


class PlanUpdateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    plan: list[PlanStepArguments] = Field(max_length=10)
    explanation: str | None = Field(default=None, max_length=300)

    @field_validator("explanation")
    @classmethod
    def _clean_explanation(cls, value: str | None) -> str | None:
        return (value.strip() or None) if value is not None else None

    @model_validator(mode="after")
    def _one_active_step(self) -> "PlanUpdateArguments":
        if sum(item.status == "in_progress" for item in self.plan) > 1:
            raise ValueError("At most one public step may be in progress.")
        return self

    def public_steps(self) -> list[dict[str, str]]:
        # Snapshot-local labels, not claims of stable native task identity.
        return [{"id": f"s{index}", "content": item.step, "status": item.status}
                for index, item in enumerate(self.plan, 1)]


PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": PLAN_TOOL_NAME,
        "description": "Replace this run's short public work plan. Model-reported metadata only; no external action.",
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "plan": {"type": "array", "maxItems": 10, "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "step": {"type": "string", "minLength": 1, "maxLength": 160},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]},
                    }, "required": ["step", "status"],
                }},
                "explanation": {"type": "string", "maxLength": 300},
            }, "required": ["plan"],
        },
    },
}


@dataclass
class RunLoopControl:
    """One finite tool-loop budget, checkpointed before every approval boundary."""

    model_calls: int = 0
    plan_attempts: int = 0

    def __post_init__(self) -> None:
        for value, limit in ((self.model_calls, MAX_MODEL_CALLS), (self.plan_attempts, MAX_PLAN_ATTEMPTS)):
            if type(value) is not int or not 0 <= value <= limit:
                raise ValueError("Invalid run control counter.")

    def attempt_plan(self) -> bool:
        if self.plan_attempts >= MAX_PLAN_ATTEMPTS:
            return False
        self.plan_attempts += 1
        return True

    def to_state(self) -> dict[str, int]:
        self.__post_init__()
        return {"version": 1, "model_calls": self.model_calls, "plan_attempts": self.plan_attempts}

    @classmethod
    def from_state(cls, state: object) -> "RunLoopControl":
        if not isinstance(state, dict) or set(state) != {"version", "model_calls", "plan_attempts"}:
            raise ValueError("Invalid run control checkpoint.")
        if type(state["version"]) is not int or state["version"] != 1:
            raise ValueError("Unsupported run control checkpoint.")
        return cls(model_calls=state["model_calls"], plan_attempts=state["plan_attempts"])
