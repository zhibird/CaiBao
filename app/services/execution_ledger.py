"""Deterministic accounting for one API run, including approval continuations.

Signatures belong to a private checkpoint; public receipts expose only bounded
counts and controlled stop reasons, never arguments, tool output, or raw errors.
The caller persists a checkpoint before pausing or executing a reserved action.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, get_args


ExecutionStopReason = Literal[
    "in_progress", "model_finished", "tool_budget", "duplicate_guard",
    "iteration_limit", "waiting_confirmation", "cancelled", "interrupted",
    "execution_error", "invalid_checkpoint",
]
_REASONS = frozenset(get_args(ExecutionStopReason))
_INCOMPLETE_REASONS = _REASONS - {"model_finished", "tool_budget"}
_OUTCOMES = {"completed": "succeeded", "failed": "failed", "rejected": "rejected", "skipped": "skipped"}
_COUNTERS = ("used", "succeeded", "failed", "rejected", "skipped", "pending", "omitted")
_STATE_KEYS = frozenset({"version", "budget", "history", "stop_reason", *_COUNTERS})


def _integer(value: object, name: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"Invalid execution ledger {name}.")
    return value


@dataclass
class ToolLoopLedger:
    budget: int
    used: int = 0
    succeeded: int = 0
    failed: int = 0
    rejected: int = 0
    skipped: int = 0
    pending: int = 0
    omitted: int = 0
    history: list[tuple[str, str]] = field(default_factory=list)
    stop_reason: ExecutionStopReason = "model_finished"

    def __post_init__(self) -> None:
        self._validate()

    def reserve(self, signature: tuple[str, str]) -> bool:
        """Count one proposed attempt exactly once, before execution/approval.

False means no budget remains; the caller records the known number of omitted
calls separately. No counters or signatures change on a failed reservation.
        """
        self._validate()
        self._signature(signature)
        if self.used >= self.budget:
            return False
        self.used += 1
        self.pending += 1
        self.history.append(signature)
        return True

    def resolve(self, outcome: Literal["completed", "failed", "rejected", "skipped"]) -> None:
        """Resolve one reservation without charging its budget again."""
        self._validate()
        if not isinstance(outcome, str) or outcome not in _OUTCOMES:
            raise ValueError("Invalid execution ledger outcome.")
        if self.pending < 1:
            raise ValueError("Cannot resolve an unreserved tool call.")
        self.pending -= 1
        counter = _OUTCOMES[outcome]
        setattr(self, counter, getattr(self, counter) + 1)

    def reject(self, signature: tuple[str, str]) -> bool:
        """Reserve and reject an invalid/forbidden proposal, if budget allows."""
        if not self.reserve(signature):
            return False
        self.resolve("rejected")
        return True

    def record_omitted(self, count: int) -> None:
        self._validate()
        self.omitted += _integer(count, "omitted count")

    def to_state(self) -> dict[str, object]:
        self._validate()
        return {
            "version": 1, "budget": self.budget,
            **{key: getattr(self, key) for key in _COUNTERS},
            "history": [list(signature) for signature in self.history],
            "stop_reason": self.stop_reason,
        }

    @classmethod
    def from_state(cls, state: object, *, budget: int) -> "ToolLoopLedger":
        """Reject corrupt/stale checkpoints rather than granting fresh budget."""
        _integer(budget, "budget", minimum=1, maximum=10)
        if not isinstance(state, dict) or set(state) != _STATE_KEYS:
            raise ValueError("Invalid execution ledger checkpoint fields.")
        if type(state["version"]) is not int or state["version"] != 1:
            raise ValueError("Unsupported execution ledger checkpoint version.")
        if type(state["budget"]) is not int or state["budget"] != budget:
            raise ValueError("Execution ledger budget does not match the run.")
        raw_history = state["history"]
        if not isinstance(raw_history, list) or len(raw_history) > budget:
            raise ValueError("Invalid execution ledger history.")
        history: list[tuple[str, str]] = []
        for signature in raw_history:
            if not isinstance(signature, (tuple, list)) or len(signature) != 2:
                raise ValueError("Invalid execution ledger signature.")
            normalized = (signature[0], signature[1])
            cls._signature(normalized)
            history.append(normalized)
        return cls(
            budget=budget, **{key: state[key] for key in _COUNTERS},
            history=history, stop_reason=state["stop_reason"],
        )

    def receipt(self) -> dict[str, object]:
        self._validate()
        return {
            "version": 1, "source": "api_tool_loop", "tool_budget": self.budget,
            "tool_calls_used": self.used, "tool_calls_succeeded": self.succeeded,
            "tool_calls_failed": self.failed, "tool_calls_rejected": self.rejected,
            "tool_calls_skipped": self.skipped, "pending_tool_calls": self.pending,
            "omitted_tool_calls": self.omitted, "stop_reason": self.stop_reason,
            "incomplete": self._incomplete(),
        }

    def final_status(self) -> str:
        self._validate()
        if self.stop_reason == "waiting_confirmation":
            return "requires_confirmation"
        if self.stop_reason in {"cancelled", "interrupted"}:
            return self.stop_reason
        if self.stop_reason == "in_progress":
            return "running"
        if self._incomplete():
            return "partial_success" if self.succeeded or self.skipped else "failed"
        return "completed"

    def _incomplete(self) -> bool:
        return bool(self.failed or self.rejected or self.pending or self.omitted or self.stop_reason in _INCOMPLETE_REASONS)

    @staticmethod
    def _signature(signature: object) -> None:
        if not isinstance(signature, tuple) or len(signature) != 2 or any(not isinstance(value, str) for value in signature):
            raise ValueError("Invalid execution ledger signature.")

    def _validate(self) -> None:
        _integer(self.budget, "budget", minimum=1, maximum=10)
        for key in _COUNTERS:
            _integer(getattr(self, key), key)
        if self.used > self.budget or self.used != self.succeeded + self.failed + self.rejected + self.skipped + self.pending:
            raise ValueError("Execution ledger counts are inconsistent.")
        if not isinstance(self.history, list) or len(self.history) != self.used:
            raise ValueError("Execution ledger history does not match its budget usage.")
        for signature in self.history:
            self._signature(signature)
        if not isinstance(self.stop_reason, str) or self.stop_reason not in _REASONS:
            raise ValueError("Invalid execution ledger stop reason.")
