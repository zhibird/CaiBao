"""Keep actual execution identity and bounded conversation context per run.

Revision ID: 20260601_00
Revises: 20260531_00
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260601_00"
down_revision: str | None = "20260531_00"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Development's SQLite bootstrap may have added the nullable columns first.
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("agent_runs")}
    for name in ("execution_identity_json", "context_snapshot_json"):
        if name not in columns:
            op.add_column("agent_runs", sa.Column(name, sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "context_snapshot_json")
    op.drop_column("agent_runs", "execution_identity_json")
