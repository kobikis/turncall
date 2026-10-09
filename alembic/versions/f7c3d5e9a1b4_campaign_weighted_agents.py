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
    # The revision before this one has no room for a campaign without one
    # agent. Deleting those campaigns would take every target and its attempt
    # history with them (ON DELETE CASCADE), on a rollback likely run under
    # pressure — so refuse, and let the operator decide what they become.
    weighted = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM campaigns WHERE agent_id IS NULL"))
        .scalar_one()
    )
    if weighted:
        raise RuntimeError(
            f"{weighted} weighted campaign(s) exist (campaigns.agent_id IS NULL), "
            "which the previous revision cannot hold. Give each one an agent_id "
            "or delete it yourself, then downgrade again."
        )
    op.drop_column("campaign_targets", "agent_id")
    op.drop_column("campaigns", "agent_weights")
    op.alter_column("campaigns", "agent_id", nullable=False)
