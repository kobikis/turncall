"""projects.redaction_enabled + redaction_entities: a project's redaction (#313)

Revision ID: d7f2a4c9e5b8
Revises: c4e8a2f6b1d3
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d7f2a4c9e5b8"
down_revision: str | None = "c4e8a2f6b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Off for every existing project: redaction is opt-in (ADR-0024), so a
    # project behaves exactly as before until an admin turns it on.
    op.add_column(
        "projects",
        sa.Column(
            "redaction_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Null = the platform's default entity types, so the defaults can move
    # without a migration; a list is the project's own choice.
    op.add_column(
        "projects",
        sa.Column(
            "redaction_entities",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("projects", "redaction_entities")
    op.drop_column("projects", "redaction_enabled")
