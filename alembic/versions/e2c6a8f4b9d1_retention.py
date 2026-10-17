"""Retention (#316, ADR-0024): a project's policy, and what expired on a call

`projects.retention_*_days`: how long the project keeps each kind of call
data, null = for good (the default, so nothing changes until an admin sets
one). `calls.*_expired_at`: when the retention janitor deleted that kind of
data from the call, null = not yet. `calls.ended_reason`: frozen when the
call's events expire, since it was derived from them. One partial index per
kind on (project_id, created_at), holding only calls still to be swept: the
janitor runs hourly on every API replica, and without them each pass would
scan every call the project ever made.

Revision ID: e2c6a8f4b9d1
Revises: b5e1d9c3a7f2
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2c6a8f4b9d1"
down_revision: str | None = "b5e1d9c3a7f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = ("recording", "transcript", "events")


def upgrade() -> None:
    for kind in ("recordings", "transcripts", "events"):
        op.add_column(
            "projects",
            sa.Column(f"retention_{kind}_days", sa.Integer(), nullable=True),
        )
    for kind in KINDS:
        op.add_column(
            "calls",
            sa.Column(f"{kind}_expired_at", sa.DateTime(timezone=True), nullable=True),
        )
    op.add_column("calls", sa.Column("ended_reason", sa.String(40), nullable=True))
    for kind in KINDS:
        op.create_index(
            f"ix_calls_{kind}_retention",
            "calls",
            ["project_id", "created_at"],
            postgresql_where=sa.text(f"{kind}_expired_at IS NULL"),
        )


def downgrade() -> None:
    for kind in KINDS:
        op.drop_index(f"ix_calls_{kind}_retention", table_name="calls")
    op.drop_column("calls", "ended_reason")
    for kind in KINDS:
        op.drop_column("calls", f"{kind}_expired_at")
    for kind in ("recordings", "transcripts", "events"):
        op.drop_column("projects", f"retention_{kind}_days")
