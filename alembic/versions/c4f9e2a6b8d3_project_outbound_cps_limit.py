"""projects.limit_outbound_calls_per_second: a per-number dial rate (#225)

Revision ID: c4f9e2a6b8d3
Revises: b3e8d1f5a7c2
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4f9e2a6b8d3"
down_revision: str | None = "b3e8d1f5a7c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Null = the platform default (LIMIT_OUTBOUND_CALLS_PER_SECOND), unlimited
    # unless set.
    op.add_column(
        "projects",
        sa.Column("limit_outbound_calls_per_second", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("projects", "limit_outbound_calls_per_second")
