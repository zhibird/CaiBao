"""add agent run traces

Revision ID: 20260524_00
Revises: 20260412_00
Create Date: 2026-05-24 10:00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "20260524_00"
down_revision: Union[str, None] = "20260412_00"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    table_names = set(inspector.get_table_names())

    if "agent_runs" not in table_names:
        op.create_table(
            "agent_runs",
            sa.Column("run_id", sa.String(length=36), nullable=False),
            sa.Column("team_id", sa.String(length=64), nullable=False),
            sa.Column("user_id", sa.String(length=64), nullable=False),
            sa.Column("conversation_id", sa.String(length=36), nullable=True),
            sa.Column("space_id", sa.String(length=36), nullable=True),
            sa.Column("task", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="running"),
            sa.Column("final_answer", sa.Text(), nullable=False, server_default=""),
            sa.Column("model", sa.String(length=128), nullable=True),
            sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("max_steps", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("required_confirmations_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("latency_ms", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["conversation_id"], ["conversations.conversation_id"]),
            sa.ForeignKeyConstraint(["space_id"], ["project_spaces.space_id"]),
            sa.ForeignKeyConstraint(["team_id"], ["teams.team_id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
            sa.PrimaryKeyConstraint("run_id"),
        )

    _ensure_index("agent_runs", "ix_agent_runs_team_id", ["team_id"])
    _ensure_index("agent_runs", "ix_agent_runs_user_id", ["user_id"])
    _ensure_index("agent_runs", "ix_agent_runs_conversation_id", ["conversation_id"])
    _ensure_index("agent_runs", "ix_agent_runs_space_id", ["space_id"])
    _ensure_index("agent_runs", "ix_agent_runs_status", ["status"])
    _ensure_index("agent_runs", "ix_agent_runs_dry_run", ["dry_run"])
    _ensure_index("agent_runs", "ix_agent_runs_team_user_created_at", ["team_id", "user_id", "created_at"])

    if "agent_steps" not in table_names:
        op.create_table(
            "agent_steps",
            sa.Column("step_id", sa.String(length=36), nullable=False),
            sa.Column("run_id", sa.String(length=36), nullable=False),
            sa.Column("team_id", sa.String(length=64), nullable=False),
            sa.Column("user_id", sa.String(length=64), nullable=False),
            sa.Column("step_index", sa.Integer(), nullable=False),
            sa.Column("step_type", sa.String(length=32), nullable=False),
            sa.Column("title", sa.String(length=128), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("tool_name", sa.String(length=64), nullable=True),
            sa.Column("input_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("output_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("latency_ms", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["run_id"], ["agent_runs.run_id"]),
            sa.ForeignKeyConstraint(["team_id"], ["teams.team_id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
            sa.PrimaryKeyConstraint("step_id"),
        )

    _ensure_index("agent_steps", "ix_agent_steps_run_id", ["run_id"])
    _ensure_index("agent_steps", "ix_agent_steps_team_id", ["team_id"])
    _ensure_index("agent_steps", "ix_agent_steps_user_id", ["user_id"])
    _ensure_index("agent_steps", "ix_agent_steps_step_type", ["step_type"])
    _ensure_index("agent_steps", "ix_agent_steps_status", ["status"])
    _ensure_index("agent_steps", "ix_agent_steps_tool_name", ["tool_name"])
    _ensure_index("agent_steps", "ix_agent_steps_run_index", ["run_id", "step_index"])


def _ensure_index(table_name: str, index_name: str, columns: list[str]) -> None:
    inspector = sa.inspect(op.get_bind())
    if table_name not in set(inspector.get_table_names()):
        return
    index_names = {item["name"] for item in inspector.get_indexes(table_name)}
    if index_name not in index_names:
        op.create_index(index_name, table_name, columns, unique=False)


def downgrade() -> None:
    op.drop_index("ix_agent_steps_run_index", table_name="agent_steps")
    op.drop_index("ix_agent_steps_tool_name", table_name="agent_steps")
    op.drop_index("ix_agent_steps_status", table_name="agent_steps")
    op.drop_index("ix_agent_steps_step_type", table_name="agent_steps")
    op.drop_index("ix_agent_steps_user_id", table_name="agent_steps")
    op.drop_index("ix_agent_steps_team_id", table_name="agent_steps")
    op.drop_index("ix_agent_steps_run_id", table_name="agent_steps")
    op.drop_table("agent_steps")

    op.drop_index("ix_agent_runs_team_user_created_at", table_name="agent_runs")
    op.drop_index("ix_agent_runs_dry_run", table_name="agent_runs")
    op.drop_index("ix_agent_runs_status", table_name="agent_runs")
    op.drop_index("ix_agent_runs_space_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_conversation_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_user_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_team_id", table_name="agent_runs")
    op.drop_table("agent_runs")
