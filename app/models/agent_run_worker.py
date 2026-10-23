"""A single-use worker claim; expired executions are never retried implicitly."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AgentRunWorker(Base):
    __tablename__ = "agent_run_workers"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.run_id", ondelete="CASCADE"), primary_key=True,
    )
    owner_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="active", index=True)
    slot_key: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
