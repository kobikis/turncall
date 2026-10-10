"""sms_sessions.agent_id nullable: a text session can run an inline agent

Revision ID: c4e8a2f6b1d3
Revises: b9f5d3a7e1c2
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e8a2f6b1d3"
down_revision: str | None = "b9f5d3a7e1c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A text session on a call-init number may run the inline agent call-init
    # answered with, which has no row to name (ADR-0017): its config rides in
    # `metadata_json["dynamic_config"]` and `agent_id` is null.
    op.alter_column("sms_sessions", "agent_id", nullable=True)


def downgrade() -> None:
    inline = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM sms_sessions WHERE agent_id IS NULL"))
        .scalar_one()
    )
    if inline:
        raise RuntimeError(
            f"{inline} text session(s) ran an inline agent (sms_sessions.agent_id "
            "IS NULL), which the previous revision cannot hold. Delete them "
            "yourself, then downgrade again."
        )
    op.alter_column("sms_sessions", "agent_id", nullable=False)
