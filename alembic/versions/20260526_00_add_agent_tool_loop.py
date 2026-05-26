"""add agent tool loop fields (request_payload_json, loop_state_json, model_routes_json, llm_routing_json)

Revision ID: 20260526_00
Revises: 20260525_00
Create Date: 2026-05-26 10:00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260526_00"
down_revision: Union[str, None] = "20260525_00"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # agent_runs: new columns for tool-loop execution
    _ensure_column("agent_runs", sa.Column("request_payload_json", sa.Text(), nullable=False, server_default="{}"))
    _ensure_column("agent_runs", sa.Column("loop_state_json", sa.Text(), nullable=True))
    _ensure_column("agent_runs", sa.Column("model_routes_json", sa.Text(), nullable=True))

    # agent_apps: new column for per-app LLM routing config
    _ensure_column("agent_apps", sa.Column("llm_routing_json", sa.Text(), nullable=False, server_default="{}"))


def _ensure_column(table_name: str, column: sa.Column) -> None:
    inspector = sa.inspect(op.get_bind())
    if table_name not in set(inspector.get_table_names()):
        return
    columns = {item["name"] for item in inspector.get_columns(table_name)}
    if column.name not in columns:
        op.add_column(table_name, column)


def downgrade() -> None:
    op.drop_column("agent_apps", "llm_routing_json")
    op.drop_column("agent_runs", "model_routes_json")
    op.drop_column("agent_runs", "loop_state_json")
    op.drop_column("agent_runs", "request_payload_json")
