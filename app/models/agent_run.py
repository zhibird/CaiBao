from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AgentRun(Base):
    __tablename__ = "agent_runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    team_id: Mapped[str] = mapped_column(
        ForeignKey("teams.team_id"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id"),
        nullable=False,
        index=True,
    )
    conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversations.conversation_id"),
        nullable=True,
        index=True,
    )
    space_id: Mapped[str | None] = mapped_column(
        ForeignKey("project_spaces.space_id"),
        nullable=True,
        index=True,
    )
    app_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_apps.app_id"),
        nullable=True,
        index=True,
    )
    app_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trigger_channel: Mapped[str] = mapped_column(String(32), nullable=False, default="agent")
    task: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running", index=True)
    final_answer: Mapped[str] = mapped_column(Text, nullable=False, default="")
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    max_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    required_confirmations_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
