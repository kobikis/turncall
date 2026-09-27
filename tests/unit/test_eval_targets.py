"""Resolving a run's target: a pinned id, a published name, an inline config.

The rule worth its own module is that a name with nothing published **fails the
run** rather than falling back to a draft — "test what is live" is the whole
reason to target by name, and quietly testing an unpublished draft answers a
different question behind the same green tick.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from turncall.evals.targets import TargetError, resolve_target

pytestmark = pytest.mark.unit


class TestResolveTarget:
    async def test_an_agent_target_resolves_to_its_config(self) -> None:
        agent_id = project_id = uuid4()
        agent = SimpleNamespace(
            id=agent_id,
            version=3,
            config_blob={"system_prompt": "You are a receptionist."},
        )
        with patch(
            "turncall.storage.repositories.agent_repo.get_agent_by_id",
            AsyncMock(return_value=agent),
        ):
            resolved = await resolve_target(
                AsyncMock(),
                project_id=project_id,
                target={"type": "agent", "agent_id": str(agent_id)},
            )
        assert resolved.agent_id == agent_id
        assert resolved.agent_version == 3
        assert resolved.config.system_prompt == "You are a receptionist."
        assert resolved.config_blob == {"system_prompt": "You are a receptionist."}

    async def test_a_missing_agent_is_a_target_error(self) -> None:
        with patch(
            "turncall.storage.repositories.agent_repo.get_agent_by_id",
            AsyncMock(return_value=None),
        ):
            with pytest.raises(TargetError, match="not found"):
                await resolve_target(
                    AsyncMock(),
                    project_id=uuid4(),
                    target={"type": "agent", "agent_id": str(uuid4())},
                )

    async def test_an_unsupported_target_type_is_rejected(self) -> None:
        with pytest.raises(TargetError, match="unsupported target type"):
            await resolve_target(
                AsyncMock(), project_id=uuid4(), target={"type": "carrier-pigeon"}
            )

    async def test_a_name_resolves_to_whatever_is_published_now(self) -> None:
        """#74. An agent row is one immutable version, so a target pinned by id
        silently stops testing production the moment the next version is
        published. A name is resolved at run time, and the run records which
        version it found — `agent_id` has no foreign key, so the row can be
        deleted and take the answer with it."""
        agent_id = project_id = uuid4()
        published = SimpleNamespace(
            id=agent_id, version=7, config_blob={"system_prompt": "live"}
        )
        latest = AsyncMock(return_value=published)
        with patch(
            "turncall.storage.repositories.agent_repo.get_latest_published", latest
        ):
            resolved = await resolve_target(
                AsyncMock(),
                project_id=project_id,
                target={"type": "agent_name", "name": "support"},
            )
        assert (resolved.agent_id, resolved.agent_version) == (agent_id, 7)
        assert latest.await_args.args[1:] == (project_id, "support", "production")

    async def test_a_name_with_no_published_version_fails_rather_than_taking_a_draft(
        self,
    ) -> None:
        """Quietly testing an unpublished draft would answer a different
        question with the same green tick."""
        with patch(
            "turncall.storage.repositories.agent_repo.get_latest_published",
            AsyncMock(return_value=None),
        ):
            with pytest.raises(TargetError, match="no published version"):
                await resolve_target(
                    AsyncMock(),
                    project_id=uuid4(),
                    target={"type": "agent_name", "name": "support"},
                )

    async def test_an_inline_target_has_no_agent_id_and_no_version(self) -> None:
        """ADR-0017's first rule: null is the honest answer to "which stored
        agent was this", not missing data. The zero-UUID sentinel exists so a
        pipeline can build — writing it to a column would claim a row that does
        not exist."""
        blob = {"system_prompt": "a prompt nobody published yet"}
        resolved = await resolve_target(
            AsyncMock(),
            project_id=uuid4(),
            target={"type": "inline", "agent": blob},
        )
        assert resolved.agent_id is None
        assert resolved.agent_version is None
        assert resolved.config_blob == blob
        assert resolved.config.system_prompt == "a prompt nobody published yet"

    async def test_an_empty_inline_target_is_rejected(self) -> None:
        with pytest.raises(TargetError, match="needs an 'agent'"):
            await resolve_target(
                AsyncMock(), project_id=uuid4(), target={"type": "inline", "agent": {}}
            )

    async def test_an_inline_config_that_cannot_be_parsed_errors_the_run(self) -> None:
        """The API validates it first; this is the net for a run queued before
        a schema change. An errored run naming the problem beats a pipeline
        that dies halfway into a conversation."""
        with pytest.raises(TargetError, match="inline agent configuration is invalid"):
            await resolve_target(
                AsyncMock(),
                project_id=uuid4(),
                target={"type": "inline", "agent": {"llm": "not a mapping"}},
            )
