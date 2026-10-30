"""projects.skill_library_id: the project whose skills a project reads (#432)

A project linked to a skill library resolves every skill name in that project
instead of its own (ADR-0031). Set only with the platform credential. A purged
library leaves the projects linked to it unlinked (`ON DELETE SET NULL`), never
deleted with it.

Revision ID: b7d1f3a9c5e2
Revises: a4c8e2f6b0d3
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7d1f3a9c5e2"
down_revision: str | None = "a4c8e2f6b0d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Null = no library: the project's own skills, as every project had.
    op.add_column(
        "projects",
        sa.Column(
            "skill_library_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "projects.id",
                ondelete="SET NULL",
                name="fk_projects_skill_library_id",
            ),
            nullable=True,
        ),
    )
    # A library's delete refusal and agents listing read who links to it.
    op.create_index(
        "ix_projects_skill_library_id",
        "projects",
        ["skill_library_id"],
        postgresql_where=sa.text("skill_library_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_projects_skill_library_id", table_name="projects")
    op.drop_constraint("fk_projects_skill_library_id", "projects", type_="foreignkey")
    op.drop_column("projects", "skill_library_id")
