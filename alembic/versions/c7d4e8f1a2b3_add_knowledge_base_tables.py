"""add knowledge base tables

Revision ID: c7d4e8f1a2b3
Revises: f5a9b3c7d2e1
Create Date: 2026-04-16 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7d4e8f1a2b3"
down_revision: str | None = "f5a9b3c7d2e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Enable pgvector extension
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Knowledge bases
    op.create_table(
        "knowledge_bases",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "project_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column(
            "embedding_model",
            sa.String(100),
            nullable=False,
            server_default="text-embedding-3-small",
        ),
        sa.Column("chunk_size", sa.Integer, nullable=False, server_default="512"),
        sa.Column("chunk_overlap", sa.Integer, nullable=False, server_default="64"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_knowledge_bases_project_id", "knowledge_bases", ["project_id"])
    op.create_index(
        "ix_knowledge_bases_project_name",
        "knowledge_bases",
        ["project_id", "name"],
        unique=True,
    )

    # Documents
    op.create_table(
        "documents",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "knowledge_base_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("raw_text", sa.Text, nullable=True),
        sa.Column("char_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("chunk_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="processing",
        ),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_documents_knowledge_base_id", "documents", ["knowledge_base_id"]
    )

    # Document chunks with vector embedding
    op.create_table(
        "document_chunks",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "document_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "knowledge_base_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("token_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_document_chunks_document_id", "document_chunks", ["document_id"]
    )
    op.create_index(
        "ix_document_chunks_knowledge_base_id",
        "document_chunks",
        ["knowledge_base_id"],
    )

    # Add vector column (pgvector) — 1536 dims for text-embedding-3-small
    op.execute("ALTER TABLE document_chunks ADD COLUMN embedding vector(1536)")
    # HNSW index for fast approximate nearest-neighbor search
    op.execute(
        "CREATE INDEX ix_document_chunks_embedding ON document_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    # Agent ↔ Knowledge Base join table
    op.create_table(
        "agent_knowledge_bases",
        sa.Column(
            "agent_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "knowledge_base_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "mode",
            sa.String(20),
            nullable=False,
            server_default="auto",
        ),
        sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
        sa.Column("top_k", sa.Integer, nullable=False, server_default="5"),
        sa.Column(
            "similarity_threshold",
            sa.Float,
            nullable=False,
            server_default="0.7",
        ),
        sa.Column("tool_description", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_agent_kb_agent_id", "agent_knowledge_bases", ["agent_id"])
    op.create_index(
        "ix_agent_kb_knowledge_base_id",
        "agent_knowledge_bases",
        ["knowledge_base_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_kb_knowledge_base_id", table_name="agent_knowledge_bases")
    op.drop_index("ix_agent_kb_agent_id", table_name="agent_knowledge_bases")
    op.drop_table("agent_knowledge_bases")

    op.execute("DROP INDEX IF EXISTS ix_document_chunks_embedding")
    op.drop_index("ix_document_chunks_knowledge_base_id", table_name="document_chunks")
    op.drop_index("ix_document_chunks_document_id", table_name="document_chunks")
    op.drop_table("document_chunks")

    op.drop_index("ix_documents_knowledge_base_id", table_name="documents")
    op.drop_table("documents")

    op.drop_index("ix_knowledge_bases_project_name", table_name="knowledge_bases")
    op.drop_index("ix_knowledge_bases_project_id", table_name="knowledge_bases")
    op.drop_table("knowledge_bases")

    op.execute("DROP EXTENSION IF EXISTS vector")
