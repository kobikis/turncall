"""An eval target that cannot run errors the run before an iteration (#479).

A published agent's config is checked by agent acceptance's `eval_target`
row when the worker resolves the run's target, against a real Postgres and
the real runner: a config breaking a provider rule — one the build would
refuse halfway into the first conversation — errors the run naming the
field, and no iteration is executed.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from turncall.config.settings import Settings
from turncall.domain.enums import EvalRunStatus
from turncall.evals.runner import execute_run
from turncall.storage.database import create_engine, create_session_factory
from turncall.storage.models import AgentRow, ProjectRow
from turncall.storage.repositories import eval_repo

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SCRIPTED = {
    "turns": [
        {"user": "hello", "expect": [{"event": "llm_response", "text_contains": "hi"}]}
    ]
}
# xAI's chat endpoint is fixed: a base_url is refused (`xai_config.llm_refusal`).
BROKEN = {
    "system_prompt": "You are the support agent.",
    "llm": {"provider": "xai", "base_url": "https://gateway.example/v1"},
}


@pytest.fixture
async def factory():
    engine = create_engine(Settings().database)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            await session.connection()
    except Exception:
        pytest.skip("Postgres not reachable")
    yield session_factory
    await engine.dispose()


async def test_a_published_agent_that_cannot_run_errors_the_run_naming_the_field(
    factory,
) -> None:
    async with factory() as session:
        project = ProjectRow(name=f"eval-acceptance-{uuid4().hex[:8]}")
        session.add(project)
        await session.flush()
        # Written past the API, as a config saved before a rule existed is.
        session.add(
            AgentRow(
                project_id=project.id,
                name="support",
                environment="production",
                version=3,
                state="published",
                config_blob=BROKEN,
            )
        )
        run = await eval_repo.create_run(
            session,
            project_id=project.id,
            scenario_id=None,
            scenario_name="greets",
            kind="script",
            target={"type": "agent_name", "name": "support"},
            resolved_scenario={"definition": SCRIPTED},
            modality="text",
            iterations=2,
        )
        await session.commit()
        run_id = run.id

    executed: list[Any] = []

    async def execute(*args: Any, **kwargs: Any) -> Any:
        executed.append(args)
        raise AssertionError("no iteration should run")

    await execute_run(
        run_id, session_factory=factory, settings=Settings(), execute=execute
    )

    async with factory() as session:
        finished = await eval_repo.get_run(session, run_id)
    assert executed == []
    assert finished.status == EvalRunStatus.ERRORED.value
    assert finished.error == (
        "agent 'support' (v3) cannot run: "
        "llm: base_url is not supported for the xai provider"
    )
    assert (finished.passed_count, finished.failed_count) == (0, 0)
    assert finished.results == []
