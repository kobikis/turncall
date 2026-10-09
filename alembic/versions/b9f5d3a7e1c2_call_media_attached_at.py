"""calls.media_attached_at: one carrier media stream per call

Revision ID: b9f5d3a7e1c2
Revises: a8e4c2f6d0b1
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b9f5d3a7e1c2"
down_revision: str | None = "a8e4c2f6d0b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Set once, by the stream that claims the call; a second stream for the
    # same call finds it set and is refused. Null on existing rows: a call
    # already streaming across this upgrade is not marked, but any further
    # stream for it still needs the call's media token.
    op.add_column(
        "calls",
        sa.Column("media_attached_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("calls", "media_attached_at")
