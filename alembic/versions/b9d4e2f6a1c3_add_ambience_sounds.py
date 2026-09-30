"""add ambience_sounds table

A project's uploaded ambience sounds (ADR-0021, #167). One row per sound; the
renditions it plays from live in object storage, keyed per transport rate.

Revision ID: b9d4e2f6a1c3
Revises: c5f1a7d3e9b2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b9d4e2f6a1c3"
down_revision: str | None = "c5f1a7d3e9b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ambience_sounds",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("duration_ms", sa.Integer, nullable=False),
        sa.Column("renditions", postgresql.JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_ambience_sounds_project_id", "ambience_sounds", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_ambience_sounds_project_id", table_name="ambience_sounds")
    op.drop_table("ambience_sounds")
