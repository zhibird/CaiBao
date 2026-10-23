"""Add durable agent event cursors and single-use worker leases.

Revision ID: 20260531_00
Revises: 20260530_00
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260531_00"
down_revision: str | None = "20260530_00"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite's development bootstrap may already have created these tables.
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "agent_runs" in existing:
        columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("agent_runs")}
        if "client_request_id" not in columns:
            op.add_column("agent_runs", sa.Column("client_request_id", sa.String(64), nullable=True))
        indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("agent_runs")}
        if "uq_agent_runs_request" not in indexes:
            op.create_index("uq_agent_runs_request", "agent_runs", ["team_id", "user_id", "client_request_id"], unique=True)
    if "agent_run_events" not in existing:
        op.create_table(
            "agent_run_events",
            sa.Column("seq", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("agent_runs.run_id", ondelete="CASCADE"), nullable=False),
            sa.Column("event", sa.String(64), nullable=False),
            sa.Column("step_index", sa.Integer(), nullable=True),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_agent_run_events_run_id", "agent_run_events", ["run_id"])
    if "agent_run_workers" not in existing:
        op.create_table(
            "agent_run_workers",
            sa.Column("run_id", sa.String(36), sa.ForeignKey("agent_runs.run_id", ondelete="CASCADE"), primary_key=True),
            sa.Column("owner_id", sa.String(36), nullable=False),
            sa.Column("state", sa.String(24), nullable=False),
            sa.Column("slot_key", sa.String(64), nullable=True, unique=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        )
        for column in ("owner_id", "state", "lease_expires_at"):
            op.create_index(f"ix_agent_run_workers_{column}", "agent_run_workers", [column])


def downgrade() -> None:
    op.drop_table("agent_run_workers")
    op.drop_table("agent_run_events")
    op.drop_index("uq_agent_runs_request", table_name="agent_runs")
    op.drop_column("agent_runs", "client_request_id")
