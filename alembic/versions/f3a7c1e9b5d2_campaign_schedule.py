"""campaigns.calling_window: when targets may be dialled (#230)

Revision ID: f3a7c1e9b5d2
Revises: e6b2c4d8f1a3
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "f3a7c1e9b5d2"
down_revision: str | None = "e6b2c4d8f1a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("campaigns", sa.Column("calling_window", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("campaigns", "calling_window")
