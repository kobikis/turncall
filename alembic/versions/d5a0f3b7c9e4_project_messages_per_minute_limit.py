"""projects.limit_messages_per_minute: a per-session message rate (#226)

Revision ID: d5a0f3b7c9e4
Revises: c4f9e2a6b8d3
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5a0f3b7c9e4"
down_revision: str | None = "c4f9e2a6b8d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Null = the platform default (LIMIT_MESSAGES_PER_MINUTE), unlimited unless
    # set.
    op.add_column(
        "projects",
        sa.Column("limit_messages_per_minute", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("projects", "limit_messages_per_minute")
