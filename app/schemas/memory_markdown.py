from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class PendingItem(BaseModel):
    tag: str = Field(min_length=1, max_length=32)
    content: str = Field(min_length=1, max_length=2000)


class HistoryEntry(BaseModel):
    summary: str = Field(min_length=1, max_length=2000)
    emotional_weight: int = Field(default=0, ge=-3, le=3)


class RecentContextResult(BaseModel):
    compression: list[str] = Field(default_factory=list)
    ongoing_threads: list[str] = Field(default_factory=list)


class ConsolidationResult(BaseModel):
    history_entries: list[dict[str, Any]] = Field(default_factory=list)
    pending_items: list[dict[str, Any]] = Field(default_factory=list)
    recent_context: dict[str, Any] = Field(default_factory=dict)
