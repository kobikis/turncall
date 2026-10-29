"""add skills table (#423)

`skills`: one row per version of a named skill, as `agents` is one row per
version of a named agent. `tools` is JSONB whose webhook secrets the ORM seals
(`SealedTools`, ADR-0020); a new table, so there is nothing to seal here.

Revision ID: a4c8e2f6b0d3
Revises: e6c2a8f4b0d9
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a4c8e2f6b0d3"
down_revision: str | None = "e6c2a8f4b0d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "skills",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("instructions", sa.Text, nullable=False),
        sa.Column(
            "tools",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "knowledge_bases",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "project_id", "name", "version", name="uq_skills_project_name_version"
        ),
    )
    op.create_index("ix_skills_project_created", "skills", ["project_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_skills_project_created", table_name="skills")
    op.drop_table("skills")
