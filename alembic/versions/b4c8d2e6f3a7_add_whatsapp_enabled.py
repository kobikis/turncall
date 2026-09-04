"""add whatsapp_enabled column to phone_numbers

Revision ID: b4c8d2e6f3a7
Revises: a3b7c9d1e5f2
Create Date: 2026-04-13 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4c8d2e6f3a7"
down_revision: str | None = "a3b7c9d1e5f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "phone_numbers",
        sa.Column(
            "whatsapp_enabled", sa.Boolean(), nullable=False, server_default="false"
        ),
    )


def downgrade() -> None:
    op.drop_column("phone_numbers", "whatsapp_enabled")
