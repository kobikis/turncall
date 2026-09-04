"""add sms tables and sms_enabled column

Revision ID: a3b7c9d1e5f2
Revises: e1f72f522b68
Create Date: 2026-04-12 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3b7c9d1e5f2"
down_revision: str | None = "e1f72f522b68"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create sms_sessions table
    # Use sa.String for enum columns to avoid asyncpg checkfirst issues.
    # The ORM Enum(...) in models.py handles validation at the app layer.
    op.create_table(
        "sms_sessions",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "assistant_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "phone_number_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("customer_number", sa.String(20), nullable=False),
        sa.Column("turncall_number", sa.String(20), nullable=False),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="active",
        ),
        sa.Column("channel", sa.String(10), nullable=False, server_default="sms"),
        sa.Column("message_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "metadata_json",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_index("ix_sms_sessions_project_id", "sms_sessions", ["project_id"])
    op.create_index(
        "ix_sms_sessions_lookup",
        "sms_sessions",
        ["customer_number", "turncall_number", "status"],
    )
    op.create_index("ix_sms_sessions_expires_at", "sms_sessions", ["expires_at"])

    # Create sms_messages table
    op.create_table(
        "sms_messages",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sms_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("channel", sa.String(10), nullable=False, server_default="sms"),
        sa.Column("provider_message_sid", sa.String(64), nullable=True),
        sa.Column("token_count", sa.Integer, nullable=True),
        sa.Column(
            "metadata_json",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_index("ix_sms_messages_session_id", "sms_messages", ["session_id"])
    op.create_index(
        "ix_sms_messages_session_created",
        "sms_messages",
        ["session_id", "created_at"],
    )

    # Add sms_enabled to phone_numbers
    op.add_column(
        "phone_numbers",
        sa.Column("sms_enabled", sa.Boolean, nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("phone_numbers", "sms_enabled")

    op.drop_index("ix_sms_messages_session_created", table_name="sms_messages")
    op.drop_index("ix_sms_messages_session_id", table_name="sms_messages")
    op.drop_table("sms_messages")

    op.drop_index("ix_sms_sessions_expires_at", table_name="sms_sessions")
    op.drop_index("ix_sms_sessions_lookup", table_name="sms_sessions")
    op.drop_index("ix_sms_sessions_project_id", table_name="sms_sessions")
    op.drop_table("sms_sessions")
