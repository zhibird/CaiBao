"""add agent app builder tables

Revision ID: 20260525_00
Revises: 20260524_00
Create Date: 2026-05-25 14:00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "20260525_00"
down_revision: Union[str, None] = "20260524_00"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    table_names = set(inspector.get_table_names())

    if "agent_apps" not in table_names:
        op.create_table(
            "agent_apps",
            sa.Column("app_id", sa.String(length=36), nullable=False),
            sa.Column("team_id", sa.String(length=64), nullable=False),
            sa.Column("created_by_user_id", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("mode", sa.String(length=32), nullable=False, server_default="agent_auto"),
            sa.Column("system_prompt", sa.Text(), nullable=False, server_default=""),
            sa.Column("model", sa.String(length=128), nullable=True),
            sa.Column("embedding_model", sa.String(length=128), nullable=True),
            sa.Column("space_id", sa.String(length=36), nullable=True),
            sa.Column("retrieval_config_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("tool_config_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("workflow_config_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="draft"),
            sa.Column("app_version", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["created_by_user_id"], ["users.user_id"]),
            sa.ForeignKeyConstraint(["space_id"], ["project_spaces.space_id"]),
            sa.ForeignKeyConstraint(["team_id"], ["teams.team_id"]),
            sa.PrimaryKeyConstraint("app_id"),
        )

    _ensure_index("agent_apps", "ix_agent_apps_team_id", ["team_id"])
    _ensure_index("agent_apps", "ix_agent_apps_created_by_user_id", ["created_by_user_id"])
    _ensure_index("agent_apps", "ix_agent_apps_space_id", ["space_id"])
    _ensure_index("agent_apps", "ix_agent_apps_mode", ["mode"])
    _ensure_index("agent_apps", "ix_agent_apps_status", ["status"])
    _ensure_index("agent_apps", "ix_agent_apps_team_status_updated_at", ["team_id", "status", "updated_at"])
    _ensure_index("agent_apps", "ix_agent_apps_team_owner_created_at", ["team_id", "created_by_user_id", "created_at"])

    if "agent_app_versions" not in table_names:
        op.create_table(
            "agent_app_versions",
            sa.Column("version_id", sa.String(length=36), nullable=False),
            sa.Column("app_id", sa.String(length=36), nullable=False),
            sa.Column("team_id", sa.String(length=64), nullable=False),
            sa.Column("version_number", sa.Integer(), nullable=False),
            sa.Column("snapshot_json", sa.Text(), nullable=False),
            sa.Column("notes", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_by_user_id", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["app_id"], ["agent_apps.app_id"]),
            sa.ForeignKeyConstraint(["created_by_user_id"], ["users.user_id"]),
            sa.ForeignKeyConstraint(["team_id"], ["teams.team_id"]),
            sa.PrimaryKeyConstraint("version_id"),
        )

    _ensure_index("agent_app_versions", "ix_agent_app_versions_app_id", ["app_id"])
    _ensure_index("agent_app_versions", "ix_agent_app_versions_team_id", ["team_id"])
    _ensure_index("agent_app_versions", "ix_agent_app_versions_created_by_user_id", ["created_by_user_id"])
    _ensure_index(
        "agent_app_versions",
        "ix_agent_app_versions_app_version",
        ["app_id", "version_number"],
        unique=True,
    )
    _ensure_index("agent_app_versions", "ix_agent_app_versions_team_created_at", ["team_id", "created_at"])

    if "agent_runs" in table_names:
        _ensure_column("agent_runs", sa.Column("app_id", sa.String(length=36), nullable=True))
        _ensure_column("agent_runs", sa.Column("app_version", sa.Integer(), nullable=True))
        _ensure_column(
            "agent_runs",
            sa.Column("trigger_channel", sa.String(length=32), nullable=False, server_default="agent"),
        )
        _ensure_index("agent_runs", "ix_agent_runs_app_id", ["app_id"])


def _ensure_column(table_name: str, column: sa.Column) -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns(table_name)}
    if column.name not in columns:
        op.add_column(table_name, column)


def _ensure_index(
    table_name: str,
    index_name: str,
    columns: list[str],
    *,
    unique: bool = False,
) -> None:
    inspector = sa.inspect(op.get_bind())
    if table_name not in set(inspector.get_table_names()):
        return
    index_names = {item["name"] for item in inspector.get_indexes(table_name)}
    if index_name not in index_names:
        op.create_index(index_name, table_name, columns, unique=unique)


def downgrade() -> None:
    op.drop_index("ix_agent_runs_app_id", table_name="agent_runs")
    op.drop_column("agent_runs", "trigger_channel")
    op.drop_column("agent_runs", "app_version")
    op.drop_column("agent_runs", "app_id")

    op.drop_index("ix_agent_app_versions_team_created_at", table_name="agent_app_versions")
    op.drop_index("ix_agent_app_versions_app_version", table_name="agent_app_versions")
    op.drop_index("ix_agent_app_versions_created_by_user_id", table_name="agent_app_versions")
    op.drop_index("ix_agent_app_versions_team_id", table_name="agent_app_versions")
    op.drop_index("ix_agent_app_versions_app_id", table_name="agent_app_versions")
    op.drop_table("agent_app_versions")

    op.drop_index("ix_agent_apps_team_owner_created_at", table_name="agent_apps")
    op.drop_index("ix_agent_apps_team_status_updated_at", table_name="agent_apps")
    op.drop_index("ix_agent_apps_status", table_name="agent_apps")
    op.drop_index("ix_agent_apps_mode", table_name="agent_apps")
    op.drop_index("ix_agent_apps_space_id", table_name="agent_apps")
    op.drop_index("ix_agent_apps_created_by_user_id", table_name="agent_apps")
    op.drop_index("ix_agent_apps_team_id", table_name="agent_apps")
    op.drop_table("agent_apps")
