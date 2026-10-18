"""calls.post_call_started_at: post-call processing starts once per call

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
    # Set once, by whichever path starts the call's post-call processing
    # (#356). The sweep starts it for an ended call that has none; every call
    # already over across this upgrade had its chance, so it is marked here and
    # never swept.
    op.add_column(
        "calls",
        sa.Column("post_call_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE calls SET post_call_started_at = ended_at WHERE ended_at IS NOT NULL"
    )
    # What the sweep reads: ended, pipeline-owned, no post-call yet. Few rows.
    op.create_index(
        "ix_calls_post_call_unstarted",
        "calls",
        ["ended_at"],
        postgresql_where=sa.text(
            "post_call_started_at IS NULL AND media_attached_at IS NOT NULL"
            " AND ended_at IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_calls_post_call_unstarted", table_name="calls")
    op.drop_column("calls", "post_call_started_at")
