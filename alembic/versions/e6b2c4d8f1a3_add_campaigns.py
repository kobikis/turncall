"""campaigns + campaign_targets: outbound calls to a list (#229)

Revision ID: e6b2c4d8f1a3
Revises: d5a0f3b7c9e4
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "e6b2c4d8f1a3"
down_revision: str | None = "d5a0f3b7c9e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts(name: str, *, nullable: bool = True) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=None if nullable else sa.text("now()"),
    )


def upgrade() -> None:
    op.create_table(
        "campaigns",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("agent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("from_number_id", UUID(as_uuid=True), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        _ts("created_at", nullable=False),
        _ts("started_at"),
        _ts("completed_at"),
    )
    op.create_index("ix_campaigns_status", "campaigns", ["status"])
    op.create_index("ix_campaigns_project", "campaigns", ["project_id"])
    op.create_table(
        "campaign_targets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "campaign_id",
            UUID(as_uuid=True),
            sa.ForeignKey("campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("e164_number", sa.String(20), nullable=False),
        sa.Column("variables", JSONB(), nullable=False, server_default="{}"),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        _ts("next_attempt_at"),
        _ts("claimed_at"),
        sa.Column(
            "call_id",
            UUID(as_uuid=True),
            sa.ForeignKey("calls.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("ended_reason", sa.String(40), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        _ts("updated_at", nullable=False),
        sa.UniqueConstraint(
            "campaign_id", "e164_number", name="uq_campaign_targets_number"
        ),
    )
    op.create_index(
        "ix_campaign_targets_claim",
        "campaign_targets",
        ["campaign_id", "status", "position"],
    )


def downgrade() -> None:
    op.drop_table("campaign_targets")
    op.drop_table("campaigns")
