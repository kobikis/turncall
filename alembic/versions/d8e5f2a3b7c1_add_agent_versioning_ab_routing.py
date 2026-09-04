"""add agent versioning and A/B routing

Revision ID: d8e5f2a3b7c1
Revises: c7d4e8f1a2b3
Create Date: 2026-04-17 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8e5f2a3b7c1"
down_revision: str | None = "c7d4e8f1a2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Convert agents.state from enum to varchar to support 'archived' and future states
    op.execute(
        "ALTER TABLE agents " "ALTER COLUMN state TYPE VARCHAR(20) USING state::text"
    )
    op.execute("DROP TYPE IF EXISTS agent_state")

    # Add routing_weights JSONB column to phone_numbers
    op.add_column(
        "phone_numbers",
        sa.Column("routing_weights", sa.dialects.postgresql.JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("phone_numbers", "routing_weights")

    # Recreate enum and convert back
    op.execute("CREATE TYPE agent_state AS ENUM ('draft', 'published', 'archived')")
    op.execute(
        "ALTER TABLE agents "
        "ALTER COLUMN state TYPE agent_state USING state::agent_state"
    )
