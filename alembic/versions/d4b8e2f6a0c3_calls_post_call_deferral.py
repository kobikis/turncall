"""calls.post_call_deferred_at, post_call_started_at: post-call processing
starts once per call, and a call's pipeline starts it (#356)

Revision ID: d4b8e2f6a0c3
Revises: c9e3a5f7b1d4
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4b8e2f6a0c3"
down_revision: str | None = "c9e3a5f7b1d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Null on existing rows, and on rows a replica still on the previous
    # release ends during a rolling deploy: such a call started its post-call
    # processing the old way, so nothing here may start it again — only a call
    # an ending explicitly deferred to its pipeline is ever swept.
    op.add_column(
        "calls",
        sa.Column("post_call_deferred_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "calls",
        sa.Column("post_call_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    # What the sweep reads: deferred, not yet started. A handful at any time.
    op.create_index(
        "ix_calls_post_call_deferred",
        "calls",
        ["post_call_deferred_at"],
        postgresql_where=sa.text(
            "post_call_deferred_at IS NOT NULL AND post_call_started_at IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_calls_post_call_deferred", table_name="calls")
    op.drop_column("calls", "post_call_started_at")
    op.drop_column("calls", "post_call_deferred_at")
