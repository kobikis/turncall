"""The api origin checks a config against its project (#474, S1).

An agent saved through the API names an ambience sound, skills and a
procedure's handoff target that must be the project's. Those checks are the
`api` row of agent acceptance, against a real Postgres: each refusal carries
the 422 body the agent endpoints answer (`details` whole), and the other
origins do not run them — a call-init agent's skills are a warning on the
call, by design (#431).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from tests.integration.test_procedures_api import AGENT, _escalating
from turncall.config.settings import Settings
from turncall.services.agent_acceptance import (
    AcceptedAgent,
    AgentOrigin,
    AgentRefusal,
    accept_agent,
)
from turncall.storage.database import create_engine, create_session_factory
from turncall.storage.models import AgentRow, ProjectRow

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def session():
    engine = create_engine(Settings().database)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.connection()
            yield session
            await session.rollback()
    except OSError:
        pytest.skip("Postgres not reachable")
    finally:
        await engine.dispose()


@pytest.fixture
async def project_id(session) -> Any:
    project = ProjectRow(name=f"api-acceptance-{uuid4().hex[:8]}")
    session.add(project)
    await session.flush()
    return project.id


async def _accept(session, project_id, config: dict[str, Any], origin=AgentOrigin.API):
    return await accept_agent(
        config, project_id=project_id, origin=origin, session=session
    )


async def test_an_ambience_sound_outside_the_project_is_refused(
    session, project_id
) -> None:
    sound = str(uuid4())

    refusal = await _accept(session, project_id, {"ambience": {"sound": sound}})

    assert isinstance(refusal, AgentRefusal)
    assert refusal.reason == (
        f"ambience.sound {sound} is not an ambience sound in this project"
    )
    assert refusal.details == {"field": "ambience.sound", "sound": sound}


async def test_a_skill_the_project_does_not_have_is_refused(
    session, project_id
) -> None:
    refusal = await _accept(session, project_id, {"skills": [{"skill": "billing"}]})

    assert isinstance(refusal, AgentRefusal)
    assert refusal.reason.startswith("skills[0] 'billing'")
    assert refusal.details is not None
    assert refusal.details["field"] == "skills[0]"


async def test_a_handoff_escalation_outside_the_project_is_refused(
    session, project_id
) -> None:
    stranger = str(uuid4())
    config = {
        **AGENT,
        "procedures": [_escalating({"handoff_to_agent": {"agent_id": stranger}})],
    }

    refusal = await _accept(session, project_id, config)

    assert isinstance(refusal, AgentRefusal)
    assert "which is not an agent in this project" in refusal.reason
    assert refusal.details == {
        "field": "procedures[0].steps[2].escalate.handoff_to_agent.agent_id",
        "agent_id": stranger,
    }


async def test_a_handoff_escalation_to_the_projects_agent_is_accepted(
    session, project_id
) -> None:
    billing = AgentRow(
        project_id=project_id,
        name="billing",
        environment="development",
        version=1,
        state="draft",
        config_blob={},
    )
    session.add(billing)
    await session.flush()
    config = {
        **AGENT,
        "procedures": [
            _escalating({"handoff_to_agent": {"agent_id": str(billing.id)}})
        ],
    }

    assert isinstance(await _accept(session, project_id, config), AcceptedAgent)


async def test_call_init_runs_none_of_the_project_checks(session, project_id) -> None:
    """A call-init agent's skills are skipped with a warning on the call, and
    its ambience and escalations resolve there too (#173, #431)."""
    config = {
        "ambience": {"sound": str(uuid4())},
        "skills": [{"skill": "billing"}],
    }

    outcome = await _accept(session, project_id, config, origin=AgentOrigin.CALL_INIT)

    assert isinstance(outcome, AcceptedAgent)
