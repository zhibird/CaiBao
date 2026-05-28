"""add proactive, subagent, and peer_agent tables

Revision ID: 20260529_00
Revises: 20260528_00
Create Date: 2026-05-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260529_00"
down_revision: str | None = "20260528_00"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Proactive sources
    op.create_table(
        "proactive_sources",
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("team_id", sa.String(64), sa.ForeignKey("teams.team_id"), nullable=False),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("mcp_server", sa.String(64), nullable=False),
        sa.Column("get_tool", sa.String(128), nullable=False),
        sa.Column("ack_tool", sa.String(128), nullable=True),
        sa.Column("poll_tool", sa.String(128), nullable=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("config_json", sa.String(4000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("source_id"),
    )
    op.create_index("ix_proactive_sources_team_id", "proactive_sources", ["team_id"])
    op.create_index("ix_proactive_sources_user_id", "proactive_sources", ["user_id"])

    # Proactive deliveries
    op.create_table(
        "proactive_deliveries",
        sa.Column("delivery_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("team_id", sa.String(64), sa.ForeignKey("teams.team_id"), nullable=False),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("target", sa.String(256), nullable=True),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("evidence_json", sa.Text, nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("dedupe_hash", sa.String(64), nullable=False),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("delivery_id"),
    )
    op.create_index("ix_proactive_deliveries_team_id", "proactive_deliveries", ["team_id"])
    op.create_index("ix_proactive_deliveries_user_id", "proactive_deliveries", ["user_id"])
    op.create_index("ix_proactive_deliveries_dedupe_hash", "proactive_deliveries", ["dedupe_hash"])

    # Proactive event logs
    op.create_table(
        "proactive_event_logs",
        sa.Column("log_id", sa.String(36), nullable=False),
        sa.Column("team_id", sa.String(64), sa.ForeignKey("teams.team_id"), nullable=False),
        sa.Column("source_name", sa.String(128), nullable=False),
        sa.Column("event_id", sa.String(256), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("title", sa.String(512), nullable=True),
        sa.Column("content", sa.Text, nullable=True),
        sa.Column("url", sa.String(2048), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="new"),
        sa.Column("classification", sa.String(64), nullable=True),
        sa.Column("ack_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_json", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("log_id"),
    )
    op.create_index("ix_proactive_event_logs_team_id", "proactive_event_logs", ["team_id"])
    op.create_index("ix_proactive_event_logs_event_id", "proactive_event_logs", ["event_id"])

    # SubAgent jobs
    op.create_table(
        "subagent_jobs",
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("parent_run_id", sa.String(36), nullable=True),
        sa.Column("team_id", sa.String(64), sa.ForeignKey("teams.team_id"), nullable=False),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("profile", sa.String(32), nullable=False),
        sa.Column("task", sa.Text, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("result_json", sa.Text, nullable=True),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("max_steps", sa.Integer, nullable=False, server_default="10"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("job_id"),
    )
    op.create_index("ix_subagent_jobs_team_id", "subagent_jobs", ["team_id"])
    op.create_index("ix_subagent_jobs_user_id", "subagent_jobs", ["user_id"])

    # Peer agent configs
    op.create_table(
        "peer_agent_configs",
        sa.Column("peer_id", sa.String(36), nullable=False),
        sa.Column("team_id", sa.String(64), sa.ForeignKey("teams.team_id"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("base_url", sa.String(1024), nullable=False),
        sa.Column("api_key", sa.String(256), nullable=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("health_status", sa.String(32), nullable=True),
        sa.Column("last_health_check", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("peer_id"),
    )
    op.create_index("ix_peer_agent_configs_team_id", "peer_agent_configs", ["team_id"])


def downgrade() -> None:
    op.drop_table("peer_agent_configs")
    op.drop_table("subagent_jobs")
    op.drop_table("proactive_event_logs")
    op.drop_table("proactive_deliveries")
    op.drop_table("proactive_sources")
