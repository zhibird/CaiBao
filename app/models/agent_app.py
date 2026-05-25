from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AgentApp(Base):
    __tablename__ = "agent_apps"
    __table_args__ = (
        Index("ix_agent_apps_team_status_updated_at", "team_id", "status", "updated_at"),
        Index("ix_agent_apps_team_owner_created_at", "team_id", "created_by_user_id", "created_at"),
    )

    app_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.team_id"), nullable=False, index=True)
    created_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="agent_auto", index=True)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    space_id: Mapped[str | None] = mapped_column(ForeignKey("project_spaces.space_id"), nullable=True, index=True)
    retrieval_config_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    tool_config_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    workflow_config_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)
    app_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
