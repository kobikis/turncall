"""The audit log (#317, ADR-0024): every change made through the API

`audit_log`: one row per change to an agent, a project credential, a phone
number, an API key or a project setting, keyed by the API key that made it.
No foreign keys: an entry outlives its key and its project (the ADR-0011
purge leaves the trail).

Append-only in the database too: a trigger refuses every UPDATE and TRUNCATE,
and every DELETE except the audit retention janitor's, which marks its
transaction with `SET LOCAL turncall.audit_log_retention = 'on'`. It guards
against the application and anything else connecting as it, not against
whoever owns the table, who can drop the trigger.

A new table, so its indexes are built with it: nothing writes to it yet.

Revision ID: b3d7f1a9c5e2
Revises: e2c6a8f4b9d1
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b3d7f1a9c5e2"
down_revision: str | None = "e2c6a8f4b9d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY = """
CREATE FUNCTION audit_log_append_only() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE'
       AND current_setting('turncall.audit_log_retention', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'audit_log is append-only: % refused', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("api_key_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor", sa.String(16), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.String(64), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column(
            "changes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_audit_log_project_created",
        "audit_log",
        ["project_id", "created_at", "id"],
    )
    op.create_index(
        "ix_audit_log_project_resource",
        "audit_log",
        ["project_id", "resource_type", "resource_id", "created_at"],
    )
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])
    op.execute(APPEND_ONLY)
    op.execute(
        "CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION audit_log_append_only()"
    )
    op.execute(
        "CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log "
        "FOR EACH STATEMENT EXECUTE FUNCTION audit_log_append_only()"
    )


def downgrade() -> None:
    op.drop_table("audit_log")  # its triggers go with it
    op.execute("DROP FUNCTION IF EXISTS audit_log_append_only()")
