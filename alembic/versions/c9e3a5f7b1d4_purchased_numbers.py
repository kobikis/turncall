"""purchased_numbers: the numbers a project bought, and on which account (#321)

One row per number a project bought through `POST /v1/phone-numbers/purchase`:
its carrier, its E.164 and whether it went on the platform's shared account or
the project's own. A release on the platform's account is allowed only for a
number the project has a `platform` row for (ADR-0025). Deleted with the
project (FK cascade) and on the release.

A new table, so its indexes are built with it: nothing writes to it yet.

Revision ID: c9e3a5f7b1d4
Revises: b3d7f1a9c5e2
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c9e3a5f7b1d4"
down_revision: str | None = "b3d7f1a9c5e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "purchased_numbers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("carrier", sa.String(16), nullable=False),
        sa.Column("e164_number", sa.String(20), nullable=False),
        sa.Column("account", sa.String(16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "project_id",
            "carrier",
            "e164_number",
            name="uq_purchased_numbers_project_number",
        ),
        sa.CheckConstraint(
            "account IN ('platform', 'own')", name="ck_purchased_numbers_account"
        ),
    )
    # A number is on the platform's account once: one project bought it.
    op.create_index(
        "uq_purchased_numbers_platform_number",
        "purchased_numbers",
        ["carrier", "e164_number"],
        unique=True,
        postgresql_where=sa.text("account = 'platform'"),
    )


def downgrade() -> None:
    op.drop_table("purchased_numbers")  # its indexes go with it
