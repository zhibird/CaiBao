"""Persist bounded, model-reported task plan snapshots.

Revision ID: 20260603_00
Revises: 20260602_00
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260603_00"
down_revision: str | None = "20260602_00"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("agent_runs")}
    if "task_plan_json" not in columns:
        op.add_column("agent_runs", sa.Column("task_plan_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "task_plan_json")
