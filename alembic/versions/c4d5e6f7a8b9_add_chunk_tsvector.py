"""Add generated tsvector column + GIN index to document_chunks (hybrid retrieval).

Revision ID: c4d5e6f7a8b9
Revises: b2c3d4e5f6a7
Create Date: 2026-07-06
"""

from alembic import op

revision = "c4d5e6f7a8b9"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Generated column: Postgres maintains it on insert/update — ingestion code
    # never touches it, and existing rows backfill on migration.
    op.execute(
        """
        ALTER TABLE document_chunks
        ADD COLUMN IF NOT EXISTS tsv tsvector
        GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_document_chunks_tsv ON document_chunks USING gin (tsv)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_tsv")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS tsv")
