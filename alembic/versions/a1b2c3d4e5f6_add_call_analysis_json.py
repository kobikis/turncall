"""add analysis_json to calls

Revision ID: a1b2c3d4e5f6
Revises: d8e5f2a3b7c1
Create Date: 2026-04-17 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "d8e5f2a3b7c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("calls", sa.Column("analysis_json", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("calls", "analysis_json")
