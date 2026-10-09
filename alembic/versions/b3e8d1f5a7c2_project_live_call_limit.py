"""projects.limit_live_calls: a project's live-call concurrency limit (#224)

Revision ID: b3e8d1f5a7c2
Revises: a2d6f0b4c8e1
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3e8d1f5a7c2"
down_revision: str | None = "a2d6f0b4c8e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Null = the platform default (LIMIT_LIVE_CALLS), unlimited unless set, so
    # every existing project behaves exactly as before.
    op.add_column(
        "projects", sa.Column("limit_live_calls", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("projects", "limit_live_calls")
