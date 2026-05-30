"""normalize legacy agent_runs schema

Revision ID: 20260530_00
Revises: 20260529_00
Create Date: 2026-05-30
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260530_00"
down_revision: str | None = "20260529_00"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CURRENT_AGENT_RUN_COLUMNS: tuple[sa.Column, ...] = (
    sa.Column("app_id", sa.String(length=36), nullable=True),
    sa.Column("app_version", sa.Integer(), nullable=True),
    sa.Column("trigger_channel", sa.String(length=32), nullable=False, server_default="agent"),
    sa.Column("task", sa.Text(), nullable=False, server_default=""),
    sa.Column("final_answer", sa.Text(), nullable=False, server_default=""),
    sa.Column("model", sa.String(length=128), nullable=True),
    sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("max_steps", sa.Integer(), nullable=False, server_default="5"),
    sa.Column("required_confirmations_json", sa.Text(), nullable=False, server_default="[]"),
    sa.Column("request_payload_json", sa.Text(), nullable=False, server_default="{}"),
    sa.Column("loop_state_json", sa.Text(), nullable=True),
    sa.Column("model_routes_json", sa.Text(), nullable=True),
    sa.Column("latency_ms", sa.Integer(), nullable=True),
    sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
)
_STALE_AGENT_RUN_COLUMNS = ("current_step", "result_summary", "error_message", "updated_at", "goal")


def upgrade() -> None:
    columns = _column_names("agent_runs")
    if not columns:
        return

    with op.batch_alter_table("agent_runs") as batch_op:
        if "goal" in columns and "task" not in columns:
            batch_op.alter_column("goal", new_column_name="task", existing_type=sa.Text())
            columns.remove("goal")
            columns.add("task")

        for column in _CURRENT_AGENT_RUN_COLUMNS:
            if column.name not in columns:
                batch_op.add_column(column)

        stale_columns = [column for column in _STALE_AGENT_RUN_COLUMNS if column in columns]
        for column in stale_columns:
            batch_op.drop_column(column)


def downgrade() -> None:
    columns = _column_names("agent_runs")
    if not columns:
        return

    with op.batch_alter_table("agent_runs") as batch_op:
        if "current_step" not in columns:
            batch_op.add_column(sa.Column("current_step", sa.Integer(), nullable=False, server_default="0"))
        if "result_summary" not in columns:
            batch_op.add_column(sa.Column("result_summary", sa.Text(), nullable=True))
        if "error_message" not in columns:
            batch_op.add_column(sa.Column("error_message", sa.Text(), nullable=True))
        if "updated_at" not in columns:
            batch_op.add_column(
                sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
            )


def _column_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in set(inspector.get_table_names()):
        return set()
    return {item["name"] for item in inspector.get_columns(table_name)}
