"""add call_context_snapshots

What the model was given during each agent segment of a call — the rendered
system instruction and the context messages (#177). A table of its own rather
than call events: `/events` returns every event type, and a snapshot is large
and holds whatever the prompt and the tools contained. Cascades with the call,
so project purge (ADR-0011) removes it too.

Revision ID: b3d9e1f7a2c4
Revises: c5f1a7d3e9b2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b3d9e1f7a2c4"
down_revision: str | None = "c5f1a7d3e9b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "call_context_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "call_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("calls.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("segment", sa.Integer(), nullable=False),
        sa.Column("system_instruction", sa.Text(), nullable=False),
        sa.Column("messages", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_call_context_snapshots_call_segment",
        "call_context_snapshots",
        ["call_id", "segment"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_call_context_snapshots_call_segment", table_name="call_context_snapshots"
    )
    op.drop_table("call_context_snapshots")
