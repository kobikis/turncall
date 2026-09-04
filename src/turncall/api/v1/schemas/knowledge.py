"""Knowledge base API schemas."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# --- Knowledge Base ---


class CreateKnowledgeBaseRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    embedding_model: str = Field(default="text-embedding-3-small", max_length=100)
    chunk_size: int = Field(default=512, ge=64, le=4096)
    chunk_overlap: int = Field(default=64, ge=0, le=512)

    @model_validator(mode="after")
    def validate_overlap(self) -> "CreateKnowledgeBaseRequest":
        if self.chunk_overlap >= self.chunk_size:
            msg = "chunk_overlap must be less than chunk_size"
            raise ValueError(msg)
        return self


class UpdateKnowledgeBaseRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None


class KnowledgeBaseResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: UUID
    project_id: UUID
    name: str
    description: str | None
    embedding_model: str
    chunk_size: int
    chunk_overlap: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> "KnowledgeBaseResponse":
        return cls(
            id=row.id,
            project_id=row.project_id,
            name=row.name,
            description=row.description,
            embedding_model=row.embedding_model,
            chunk_size=row.chunk_size,
            chunk_overlap=row.chunk_overlap,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


# --- Document ---


class DocumentResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: UUID
    knowledge_base_id: UUID
    filename: str
    content_type: str
    storage_key: str
    char_count: int
    chunk_count: int
    status: str
    error_message: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> "DocumentResponse":
        return cls(
            id=row.id,
            knowledge_base_id=row.knowledge_base_id,
            filename=row.filename,
            content_type=row.content_type,
            storage_key=row.storage_key,
            char_count=row.char_count,
            chunk_count=row.chunk_count,
            status=row.status,
            error_message=row.error_message,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


# --- Search ---


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=50)
    similarity_threshold: float = Field(default=0.3, ge=0.0, le=1.0)


class SearchResultItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: UUID
    document_id: UUID
    content: str
    similarity: float
    chunk_index: int
    token_count: int


class SearchResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    results: list[SearchResultItem]
    query: str
    total: int


# --- Agent Linkage ---


class LinkKnowledgeBaseRequest(BaseModel):
    knowledge_base_id: UUID
    mode: str = Field(default="auto", pattern=r"^(auto|tool|prompt)$")
    priority: int = Field(default=0, ge=0, le=100)
    top_k: int = Field(default=5, ge=1, le=50)
    similarity_threshold: float = Field(default=0.3, ge=0.0, le=1.0)
    tool_description: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def validate_tool_mode(self) -> "LinkKnowledgeBaseRequest":
        if self.mode == "tool" and not self.tool_description:
            msg = "tool_description is required when mode is 'tool'"
            raise ValueError(msg)
        return self


class AgentKnowledgeBaseResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    agent_id: UUID
    knowledge_base_id: UUID
    mode: str
    priority: int
    top_k: int
    similarity_threshold: float
    tool_description: str | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> "AgentKnowledgeBaseResponse":
        return cls(
            agent_id=row.agent_id,
            knowledge_base_id=row.knowledge_base_id,
            mode=row.mode,
            priority=row.priority,
            top_k=row.top_k,
            similarity_threshold=row.similarity_threshold,
            tool_description=row.tool_description,
            created_at=row.created_at,
        )
