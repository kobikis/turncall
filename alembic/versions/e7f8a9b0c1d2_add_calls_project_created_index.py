"""add (project_id, created_at) index on calls

GET /v1/calls filters project_id and orders by created_at DESC; without this
the sort cost grows linearly on an append-forever table.

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-07-07
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e7f8a9b0c1d2"
down_revision: str | None = "d6e7f8a9b0c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # IF NOT EXISTS so a dev DB that got the index out-of-band adopts cleanly.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_calls_project_created "
        "ON calls (project_id, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_calls_project_created")
