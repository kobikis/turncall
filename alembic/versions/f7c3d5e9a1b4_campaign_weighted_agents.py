"""campaigns: weighted agents, each target's assigned agent (#234)

Revision ID: f7c3d5e9a1b4
Revises: f3a7c1e9b5d2
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "f7c3d5e9a1b4"
down_revision: str | None = "f3a7c1e9b5d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A weighted campaign has no one agent: its weights say who, per target.
    op.alter_column("campaigns", "agent_id", nullable=True)
    op.add_column("campaigns", sa.Column("agent_weights", JSONB(), nullable=True))
    # Assigned at create; null only on a target written before this revision,
    # which dials as its campaign's `agent_id`.
    op.add_column(
        "campaign_targets",
        sa.Column("agent_id", UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("campaign_targets", "agent_id")
    op.drop_column("campaigns", "agent_weights")
    # A weighted campaign has no agent to restore; it is dropped with its
    # targets rather than given one it never had.
    op.execute("DELETE FROM campaigns WHERE agent_id IS NULL")
    op.alter_column("campaigns", "agent_id", nullable=False)
