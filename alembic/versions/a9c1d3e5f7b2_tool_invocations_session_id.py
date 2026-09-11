"""tool_invocations: record text-session tool calls too

Tool calling reached SMS, the Chat API and WhatsApp text, but the table was
keyed by call_id (NOT NULL, FK to calls) and a chat session has no call — so
those invocations were recorded nowhere. Chat history keeps only the customer
and assistant text, so a text tool call left no trace on either side.

call_id becomes nullable, session_id joins it, and a CHECK keeps exactly one
set: a row belongs to a call or to a session, never both and never neither.

Revision ID: a9c1d3e5f7b2
Revises: f8a9b0c1d2e3
Create Date: 2026-09-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9c1d3e5f7b2"
down_revision: str | None = "f8a9b0c1d2e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "tool_invocations",
        "call_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.add_column(
        "tool_invocations",
        sa.Column(
            "session_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_tool_invocations_session_id",
        "tool_invocations",
        "sms_sessions",
        ["session_id"],
        ["id"],
        ondelete="CASCADE",
    )
    # The read path lists a session's invocations in order, same as the call one.
    op.create_index(
        "ix_tool_invocations_session_id",
        "tool_invocations",
        ["session_id"],
    )
    op.create_check_constraint(
        "ck_tool_invocations_owner",
        "tool_invocations",
        "num_nonnulls(call_id, session_id) = 1",
    )


def downgrade() -> None:
    # Session-owned rows have no call to fall back on, so they can't survive a
    # NOT NULL call_id. Dropped rather than blocking the downgrade.
    op.execute("DELETE FROM tool_invocations WHERE call_id IS NULL")
    op.drop_constraint("ck_tool_invocations_owner", "tool_invocations")
    op.drop_index("ix_tool_invocations_session_id", table_name="tool_invocations")
    op.drop_constraint(
        "fk_tool_invocations_session_id", "tool_invocations", type_="foreignkey"
    )
    op.drop_column("tool_invocations", "session_id")
    op.alter_column(
        "tool_invocations",
        "call_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=False,
    )
