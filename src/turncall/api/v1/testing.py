"""Testing and evaluation framework endpoints."""

from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import select

from turncall.api.deps import DbSession
from turncall.api.errors import NotFoundError
from turncall.api.responses import ok
from turncall.api.v1.schemas.testing import (
    CreateTestRunRequest,
    CreateTestSuiteRequest,
    TestRunResponse,
    TestSuiteResponse,
)
from turncall.auth import Auth
from turncall.storage.models import TestRunRow, TestSuiteRow

router = APIRouter(prefix="/test-suites", tags=["testing"])


@router.post("", status_code=201)
async def create_test_suite(
    body: CreateTestSuiteRequest,
    auth: Auth,
    session: DbSession,
) -> dict:
    """Create a test suite with scripted scenarios."""
    row = TestSuiteRow(
        project_id=auth.project_id,
        name=body.name,
        agent_id=body.agent_id,
        scenarios={"scenarios": [s.model_dump() for s in body.scenarios]},
        rubric=body.rubric,
    )
    session.add(row)
    await session.flush()
    return ok(TestSuiteResponse.from_row(row))


@router.get("")
async def list_test_suites(
    auth: Auth,
    session: DbSession,
) -> dict:
    """List test suites for the project."""
    result = await session.execute(
        select(TestSuiteRow)
        .where(TestSuiteRow.project_id == auth.project_id)
        .order_by(TestSuiteRow.created_at.desc())
    )
    return ok([TestSuiteResponse.from_row(r) for r in result.scalars().all()])


@router.get("/{suite_id}")
async def get_test_suite(
    suite_id: UUID,
    auth: Auth,
    session: DbSession,
) -> dict:
    """Get a test suite by ID."""
    result = await session.execute(
        select(TestSuiteRow).where(
            TestSuiteRow.id == suite_id,
            TestSuiteRow.project_id == auth.project_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise NotFoundError("TestSuite", str(suite_id))
    return ok(TestSuiteResponse.from_row(row))


# --- Test Runs ---

test_runs_router = APIRouter(prefix="/test-runs", tags=["testing"])


@test_runs_router.post("", status_code=201)
async def create_test_run(
    body: CreateTestRunRequest,
    auth: Auth,
    session: DbSession,
) -> dict:
    """Create and queue a test run for a test suite.

    The run starts in 'pending' status. A background worker
    will execute the scenarios and update results.
    """
    # Verify suite exists
    suite_result = await session.execute(
        select(TestSuiteRow).where(
            TestSuiteRow.id == body.test_suite_id,
            TestSuiteRow.project_id == auth.project_id,
        )
    )
    suite = suite_result.scalar_one_or_none()
    if suite is None:
        raise NotFoundError("TestSuite", str(body.test_suite_id))

    row = TestRunRow(
        test_suite_id=body.test_suite_id,
        project_id=auth.project_id,
        status="pending",
        results={},
    )
    session.add(row)
    await session.flush()
    return ok(TestRunResponse.from_row(row))


@test_runs_router.get("/{run_id}")
async def get_test_run(
    run_id: UUID,
    auth: Auth,
    session: DbSession,
) -> dict:
    """Get a test run by ID."""
    result = await session.execute(
        select(TestRunRow).where(
            TestRunRow.id == run_id,
            TestRunRow.project_id == auth.project_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise NotFoundError("TestRun", str(run_id))
    return ok(TestRunResponse.from_row(row))
