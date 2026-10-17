"""Retention (#316, ADR-0024): a project's policy, and what expired on a call

`projects.retention_*_days`: how long the project keeps each kind of call
data, null = for good (the default, so nothing changes until an admin sets
one). `calls.*_expired_at`: when the retention janitor deleted that kind of
data from the call, null = not yet. `calls.ended_reason`: frozen when the
call's events expire, since it was derived from them. One partial index per
kind on (project_id, created_at), holding only calls still to be swept: the
janitor runs hourly on every API replica, and without them each pass would
scan every call the project ever made. Built CONCURRENTLY, so the deploy never
blocks writes to `calls`. `recording_status` gains `expired`: the recording
was deleted under retention.

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
    # Outside a transaction: a new enum value cannot be used in the transaction
    # that adds it, and CONCURRENTLY cannot run in one at all. A plain CREATE
    # INDEX holds a SHARE lock on `calls` for the whole build, which blocks
    # every insert and update: no call could start or end until it finished
    # (#199). IF NOT EXISTS, so a re-run after a failure picks up where it
    # stopped (a build that failed half-way leaves an INVALID index: drop it
    # before re-running).
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE recording_status ADD VALUE IF NOT EXISTS 'expired'")
        for kind in KINDS:
            op.create_index(
                f"ix_calls_{kind}_retention",
                "calls",
                ["project_id", "created_at"],
                postgresql_where=sa.text(f"{kind}_expired_at IS NULL"),
                postgresql_concurrently=True,
                if_not_exists=True,
            )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for kind in KINDS:
            op.drop_index(
                f"ix_calls_{kind}_retention",
                table_name="calls",
                postgresql_concurrently=True,
                if_exists=True,
            )
    # Postgres cannot drop an enum value; it stays, unused, and the upgrade
    # adds it only if missing. The rows using it read as having no recording,
    # which is what an earlier build can say of them.
    op.execute(
        "UPDATE calls SET recording_status = 'none' "
        "WHERE recording_status::text = 'expired'"
    )
    op.drop_column("calls", "ended_reason")
    for kind in KINDS:
        op.drop_column("calls", f"{kind}_expired_at")
    for kind in ("recordings", "transcripts", "events"):
        op.drop_column("projects", f"retention_{kind}_days")
