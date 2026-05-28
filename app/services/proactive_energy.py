from __future__ import annotations

import time
from typing import Any


class ProactiveEnergy:
    """Three-curve energy model: urgency + relevance - fatigue."""

    def __init__(
        self,
        urgency_weight: float = 0.4,
        relevance_weight: float = 0.35,
        fatigue_weight: float = 0.25,
        push_threshold: float = 0.5,
        base_interval: float = 60.0,
        max_interval: float = 480.0,
    ) -> None:
        self.urgency_weight = urgency_weight
        self.relevance_weight = relevance_weight
        self.fatigue_weight = fatigue_weight
        self.push_threshold = push_threshold
        self.base_interval = base_interval
        self.max_interval = max_interval
        self._push_history: list[float] = []

    def score(self, *, severity: str = "", relevance: float = 0.5) -> tuple[float, bool]:
        """Return (energy_score, should_push)."""
        urgency = self._urgency_from_severity(severity)
        fatigue = self._fatigue_score()
        score = (
            self.urgency_weight * urgency
            + self.relevance_weight * relevance
            - self.fatigue_weight * fatigue
        )
        return max(0.0, min(1.0, score)), score >= self.push_threshold

    def record_push(self) -> None:
        now = time.time()
        self._push_history.append(now)
        # Prune entries older than 2 hours to prevent unbounded growth
        cutoff = now - 7200
        self._push_history = [t for t in self._push_history if t > cutoff]

    def next_tick_seconds(self) -> float:
        fatigue = self._fatigue_score()
        interval = self.base_interval * (1.0 + 2.0 * fatigue)
        return min(interval, self.max_interval)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _urgency_from_severity(severity: str) -> float:
        s = severity.lower().strip()
        if s in ("critical", "p1", "high"):
            return 1.0
        if s in ("warning", "p2", "medium"):
            return 0.7
        if s in ("info", "p3", "low"):
            return 0.3
        return 0.5

    def _fatigue_score(self) -> float:
        now = time.time()
        recent = [t for t in self._push_history if now - t < 3600]
        # Exponential decay: each push in last hour contributes up to 0.25 fatigue
        return min(1.0, len(recent) * 0.25)
