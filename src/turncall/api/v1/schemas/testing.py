"""Testing framework API schemas."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ScenarioSchema(BaseModel):
    """A scripted conversation scenario."""

    name: str = Field(..., min_length=1, max_length=255)
    turns: list[dict[str, str]] = Field(
        ...,
        description="List of {role, content} turn pairs",
    )
    expected_outcomes: dict[str, Any] = Field(default_factory=dict)


class CreateTestSuiteRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    agent_id: UUID
    scenarios: list[ScenarioSchema] = Field(..., min_length=1)
    rubric: dict[str, Any] = Field(default_factory=dict)


class TestSuiteResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    project_id: UUID
    name: str
    agent_id: UUID
    scenario_count: int
    created_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> "TestSuiteResponse":
        scenarios = row.scenarios or {}
        return cls(
            id=row.id,
            project_id=row.project_id,
            name=row.name,
            agent_id=row.agent_id,
            scenario_count=len(scenarios.get("scenarios", [])),
            created_at=row.created_at,
        )


class CreateTestRunRequest(BaseModel):
    test_suite_id: UUID


class TestRunResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    test_suite_id: UUID
    project_id: UUID
    status: str
    score: float | None
    results: dict[str, Any]
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> "TestRunResponse":
        return cls(
            id=row.id,
            test_suite_id=row.test_suite_id,
            project_id=row.project_id,
            status=row.status,
            score=row.score,
            results=row.results,
            started_at=row.started_at,
            completed_at=row.completed_at,
            created_at=row.created_at,
        )
