from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MemoryCard(Base):
    __tablename__ = "memory_cards"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'disabled', 'expired')",
            name="ck_memory_cards_status",
        ),
        CheckConstraint(
            "scope_level IN ('space', 'global')",
            name="ck_memory_cards_scope_level",
        ),
        Index(
            "ix_memory_cards_team_space_status_updated_at",
            "team_id",
            "space_id",
            "status",
            "updated_at",
        ),
        Index(
            "ix_memory_cards_team_user_status_created_at",
            "team_id",
            "user_id",
            "status",
            "created_at",
        ),
    )

    memory_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    team_id: Mapped[str] = mapped_column(
        ForeignKey("teams.team_id"),
        nullable=False,
        index=True,
    )
    space_id: Mapped[str | None] = mapped_column(
        ForeignKey("project_spaces.space_id"),
        nullable=True,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id"),
        nullable=False,
        index=True,
    )
    scope_level: Mapped[str] = mapped_column(String(16), nullable=False, default="space")
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="fact")
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=0.8)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.8)
    source_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_document_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.document_id"),
        nullable=True,
        index=True,
    )
    source_ref: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    memory_type: Mapped[str] = mapped_column(String(32), nullable=False, default="card", server_default="card")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
