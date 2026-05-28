"""add source_ref and memory_type to memory_cards

Revision ID: 20260528_00
Revises: 20260526_00
Create Date: 2026-05-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260528_00"
down_revision: str | None = "20260526_00"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("memory_cards", sa.Column("source_ref", sa.String(128), nullable=True))
    op.add_column("memory_cards", sa.Column(
        "memory_type", sa.String(32), nullable=False, server_default="card",
    ))
    op.create_index("ix_memory_cards_source_ref", "memory_cards", ["source_ref"])
    op.create_index(
        "ix_memory_cards_team_user_space_ref_type",
        "memory_cards",
        ["team_id", "user_id", "space_id", "source_ref", "memory_type"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_memory_cards_team_user_space_ref_type", table_name="memory_cards")
    op.drop_index("ix_memory_cards_source_ref", table_name="memory_cards")
    op.drop_column("memory_cards", "memory_type")
    op.drop_column("memory_cards", "source_ref")
