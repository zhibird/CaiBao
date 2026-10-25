"""Persist a public, bounded execution receipt per API run.

Revision ID: 20260602_00
Revises: 20260601_00
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260602_00"
down_revision: str | None = "20260601_00"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The local SQLite bootstrap may already have added this nullable column.
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("agent_runs")}
    if "execution_receipt_json" not in columns:
        op.add_column("agent_runs", sa.Column("execution_receipt_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "execution_receipt_json")
