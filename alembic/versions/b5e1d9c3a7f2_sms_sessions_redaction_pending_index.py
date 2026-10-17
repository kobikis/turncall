"""sms_sessions: a partial index for the redaction sweep's claim (#314)

The session sweep claims expired sessions still to be rewritten redacted
(`metadata_json->>'redaction' = 'pending'`, `sms_session_repo
.claim_pending_redaction`) on every API replica, every 15 minutes. Without it
that is a scan of every session that ever expired.

Revision ID: b5e1d9c3a7f2
Revises: d7f2a4c9e5b8
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b5e1d9c3a7f2"
down_revision: str | None = "d7f2a4c9e5b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_sms_sessions_redaction_pending",
        "sms_sessions",
        ["expires_at"],
        postgresql_where=sa.text("metadata_json ->> 'redaction' = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_sms_sessions_redaction_pending", table_name="sms_sessions")
