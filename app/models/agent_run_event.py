"""Durable, append-only transport events for reconnectable agent runs."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AgentRunEvent(Base):
    __tablename__ = "agent_run_events"
    # A database cursor, deliberately independent of the source generator's seq.
    # Gaps across runs are expected; ordering within one run remains strict.
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.run_id", ondelete="CASCADE"), nullable=False, index=True,
    )
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    step_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
