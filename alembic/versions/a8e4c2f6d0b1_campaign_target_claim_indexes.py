"""campaign_targets: indexes for the claim's two lookups and the janitor

Revision ID: a8e4c2f6d0b1
Revises: f7c3d5e9a1b4
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a8e4c2f6d0b1"
down_revision: str | None = "f7c3d5e9a1b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A never-dialled target, in list order.
    op.create_index(
        "ix_campaign_targets_fresh",
        "campaign_targets",
        ["campaign_id", "position"],
        postgresql_where=sa.text("status = 'pending' AND next_attempt_at IS NULL"),
    )
    # A retried or deferred one, earliest due first.
    op.create_index(
        "ix_campaign_targets_due",
        "campaign_targets",
        ["campaign_id", "next_attempt_at", "position"],
        postgresql_where=sa.text("status = 'pending' AND next_attempt_at IS NOT NULL"),
    )
    # The janitor reads claims across every campaign, every pass.
    op.create_index(
        "ix_campaign_targets_claimed",
        "campaign_targets",
        ["status", "claimed_at"],
        postgresql_where=sa.text("status IN ('dialling', 'placing')"),
    )


def downgrade() -> None:
    op.drop_index("ix_campaign_targets_claimed", table_name="campaign_targets")
    op.drop_index("ix_campaign_targets_due", table_name="campaign_targets")
    op.drop_index("ix_campaign_targets_fresh", table_name="campaign_targets")
