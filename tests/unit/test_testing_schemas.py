"""Tests for testing framework schemas."""

import uuid

import pytest
from pydantic import ValidationError

from turncall.api.v1.schemas.testing import (
    CreateTestRunRequest,
    CreateTestSuiteRequest,
    ScenarioSchema,
)


@pytest.mark.unit
class TestScenarioSchema:
    def test_valid_scenario(self) -> None:
        scenario = ScenarioSchema(
            name="greeting_test",
            turns=[
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there!"},
            ],
        )
        assert len(scenario.turns) == 2

    def test_with_expected_outcomes(self) -> None:
        scenario = ScenarioSchema(
            name="transfer_test",
            turns=[{"role": "user", "content": "Transfer me"}],
            expected_outcomes={"tool_called": "transfer_call"},
        )
        assert scenario.expected_outcomes["tool_called"] == "transfer_call"


@pytest.mark.unit
class TestCreateTestSuiteRequest:
    def test_valid_suite(self) -> None:
        req = CreateTestSuiteRequest(
            name="regression_v1",
            agent_id=uuid.uuid4(),
            scenarios=[
                ScenarioSchema(
                    name="test1",
                    turns=[{"role": "user", "content": "Hello"}],
                ),
            ],
        )
        assert len(req.scenarios) == 1

    def test_empty_scenarios_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CreateTestSuiteRequest(
                name="empty",
                agent_id=uuid.uuid4(),
                scenarios=[],
            )


@pytest.mark.unit
class TestCreateTestRunRequest:
    def test_valid_run(self) -> None:
        req = CreateTestRunRequest(test_suite_id=uuid.uuid4())
        assert req.test_suite_id is not None
