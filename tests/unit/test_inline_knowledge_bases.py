"""Knowledge bases on an inline agent (#260).

KB links live on an agent row, and an inline agent — call-init answering
`agent`, an eval `inline` target — has none (ADR-0017), so every mode was
dropped without a word on cascade and S2S alike. An inline config now names its
own in `knowledge_bases`, resolved against the call's project the way an inline
ambience sound is (#173). A stored agent keeps the link table as its one source,
so the field is refused there.

Seam 1 is the config parse, seam 2 the assembly that loads attachments, seam 3
the API boundary.
"""

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from turncall.api.v1.schemas.agents import AgentConfigSchema
from turncall.domain.models import AgentConfig
from turncall.orchestrator import pipeline_builder
from turncall.orchestrator.pipeline_factory import DYNAMIC_AGENT_ID
from turncall.services.credentials import Credentials

pytestmark = pytest.mark.unit

_KB = "turncall.storage.repositories.knowledge_repo.get_knowledge_base_by_id"


def _warnings() -> tuple[list[str], Any]:
    from loguru import logger

    seen: list[str] = []
    return seen, logger.add(lambda m: seen.append(str(m)), level="WARNING")


# --- Seam 1: the config ------------------------------------------------------


class TestTheConfig:
    def test_an_inline_config_names_its_knowledge_bases(self) -> None:
        kb = uuid.uuid4()
        config = AgentConfig.model_validate(
            {"knowledge_bases": [{"knowledge_base_id": str(kb), "mode": "tool"}]}
        )

        [attachment] = config.knowledge_bases
        assert attachment.knowledge_base_id == kb
        assert attachment.mode == "tool"
        assert (attachment.top_k, attachment.similarity_threshold) == (5, 0.3)

    def test_an_unusable_entry_is_skipped_never_the_call(self) -> None:
        """A call-init webhook's config passes through no API schema, and a
        malformed KB there must drop that KB, not the call."""
        good = uuid.uuid4()
        seen, sink = _warnings()
        try:
            config = AgentConfig.model_validate(
                {
                    "system_prompt": "hello",
                    "knowledge_bases": [
                        {"knowledge_base_id": "kb-1"},
                        {"knowledge_base_id": str(good), "mode": "sideways"},
                        {"knowledge_base_id": str(good), "top_k": 0},
                        {"knowledge_base_id": str(good), "mode": "prompt"},
                    ],
                }
            )
        finally:
            from loguru import logger

            logger.remove(sink)

        assert config.system_prompt == "hello"
        assert [a.knowledge_base_id for a in config.knowledge_bases] == [good]
        assert sum("knowledge_base_skipped" in m for m in seen) == 3, seen

    def test_a_block_that_is_not_a_list_is_skipped(self) -> None:
        config = AgentConfig.model_validate({"knowledge_bases": {"oops": 1}})
        assert config.knowledge_bases == []

    def test_null_is_none_and_says_nothing(self) -> None:
        seen, sink = _warnings()
        try:
            config = AgentConfig.model_validate({"knowledge_bases": None})
        finally:
            from loguru import logger

            logger.remove(sink)

        assert config.knowledge_bases == []
        assert seen == []


# --- Seam 2: the assembly ----------------------------------------------------


@asynccontextmanager
async def _factory():
    yield AsyncMock()


async def _assemble(
    config: AgentConfig, *, agent_id: Any, project_id: Any, kbs: dict[Any, Any]
) -> tuple[Any, Any, Any]:
    """Run build_call_pipeline; return the create_pipeline, preamble and
    link-table mocks. `kbs` is what the project's KB lookup finds, by id."""
    pipeline = MagicMock()
    pipeline.processors_with_metrics.return_value = []
    preamble = AsyncMock(return_value="P")
    links = AsyncMock(return_value=[])

    async def _get(session: Any, kb_id: Any, *, project_id: Any = None) -> Any:
        return kbs.get((kb_id, project_id))

    with (
        patch.object(pipeline_builder, "create_pipeline", return_value=pipeline) as cp,
        patch.object(pipeline_builder, "CallSession"),
        patch.object(
            pipeline_builder,
            "load_call_credentials",
            new=AsyncMock(return_value=Credentials(platform={"openai": "k"})),
        ),
        patch(
            "turncall.orchestrator.knowledge_processor.load_agent_kb_attachments",
            new=links,
        ),
        patch(_KB, new=_get),
        patch("turncall.services.retrieval.build_knowledge_preamble", preamble),
    ):
        await pipeline_builder.build_call_pipeline(
            config=config,
            transport="T",
            call_context=SimpleNamespace(
                agent_id=agent_id, project_id=project_id, is_eval=True
            ),
            settings=SimpleNamespace(pipecat=None, byom=None),
            session_factory=_factory,
        )
    return cp, preamble, links


class TestTheAssembly:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "blob",
        [{}, {"pipeline_mode": "s2s", "s2s": {"provider": "openai_live"}}],
        ids=["cascade", "s2s"],
    )
    async def test_an_inline_agent_gets_its_knowledge_bases(
        self, blob: dict[str, Any]
    ) -> None:
        project, prompt_kb, tool_kb = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        config = AgentConfig.model_validate(
            {
                **blob,
                "knowledge_bases": [
                    {"knowledge_base_id": str(tool_kb), "mode": "tool", "priority": 1},
                    {"knowledge_base_id": str(prompt_kb), "mode": "prompt"},
                ],
            }
        )
        found = {(prompt_kb, project): object(), (tool_kb, project): object()}

        cp, preamble, links = await _assemble(
            config, agent_id=DYNAMIC_AGENT_ID, project_id=project, kbs=found
        )

        attached = cp.call_args.kwargs["knowledge_base_attachments"]
        # Priority orders them, as it orders a stored agent's links.
        assert [(a["knowledge_base_id"], a["mode"]) for a in attached] == [
            (prompt_kb, "prompt"),
            (tool_kb, "tool"),
        ]
        assert preamble.call_args.args[1] == attached
        links.assert_not_awaited()  # there is no agent row to read links off

    @pytest.mark.asyncio
    async def test_a_kb_outside_the_calls_project_is_skipped_and_said(self) -> None:
        project, ours, theirs = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        config = AgentConfig.model_validate(
            {
                "knowledge_bases": [
                    {"knowledge_base_id": str(theirs), "mode": "prompt"},
                    {"knowledge_base_id": str(ours), "mode": "prompt"},
                ]
            }
        )
        # `theirs` exists, but in another project: the lookup is scoped.
        found = {(ours, project): object(), (theirs, uuid.uuid4()): object()}
        seen, sink = _warnings()
        try:
            cp, _, _ = await _assemble(
                config, agent_id=DYNAMIC_AGENT_ID, project_id=project, kbs=found
            )
        finally:
            from loguru import logger

            logger.remove(sink)

        attached = cp.call_args.kwargs["knowledge_base_attachments"]
        assert [a["knowledge_base_id"] for a in attached] == [ours]
        assert any("knowledge_base_skipped" in m and str(theirs) in m for m in seen)

    @pytest.mark.asyncio
    async def test_a_repeated_kb_is_attached_once(self) -> None:
        project, kb = uuid.uuid4(), uuid.uuid4()
        entry = {"knowledge_base_id": str(kb), "mode": "prompt"}
        config = AgentConfig.model_validate({"knowledge_bases": [entry, entry]})

        cp, _, _ = await _assemble(
            config,
            agent_id=DYNAMIC_AGENT_ID,
            project_id=project,
            kbs={(kb, project): object()},
        )

        attached = cp.call_args.kwargs["knowledge_base_attachments"]
        assert [a["knowledge_base_id"] for a in attached] == [kb]

    @pytest.mark.asyncio
    async def test_a_stored_agent_reads_its_links_never_its_config(self) -> None:
        """A config_blob from before the field was refused could carry one; the
        link table stays the one source for an agent that has a row."""
        kb = uuid.uuid4()
        config = AgentConfig.model_validate(
            {"knowledge_bases": [{"knowledge_base_id": str(kb), "mode": "prompt"}]}
        )

        cp, _, links = await _assemble(
            config, agent_id=uuid.uuid4(), project_id=uuid.uuid4(), kbs={}
        )

        links.assert_awaited_once()
        assert cp.call_args.kwargs["knowledge_base_attachments"] is None


# --- Seam 3: the API boundary ------------------------------------------------


class TestTheApi:
    def _auth(self, app: Any) -> None:
        from turncall.api.deps import get_session
        from turncall.auth.context import AuthContext
        from turncall.auth.dependencies import resolve_auth_context
        from turncall.domain.enums import ProjectRole

        ctx = AuthContext(
            project_id=uuid.uuid4(), api_key_id=uuid.uuid4(), role=ProjectRole.ADMIN
        )
        app.app.dependency_overrides[resolve_auth_context] = lambda: ctx
        app.app.dependency_overrides[get_session] = lambda: AsyncMock()

    _CONFIG: ClassVar[dict[str, Any]] = {
        "knowledge_bases": [{"knowledge_base_id": str(uuid.uuid4()), "mode": "prompt"}]
    }

    def test_a_stored_agent_create_is_422_naming_the_link_endpoint(
        self, app: Any
    ) -> None:
        self._auth(app)
        with patch(
            "turncall.storage.repositories.agent_repo.create_agent", AsyncMock()
        ) as create:
            r = app.post(
                "/v1/agents",
                headers={"Authorization": "Bearer x"},
                json={"name": "a", "config": self._CONFIG},
            )

        assert r.status_code == 422, r.text
        assert "/v1/agents/{agent_id}/knowledge-bases" in r.json()["error"]
        create.assert_not_awaited()

    def test_a_stored_agent_update_is_422_too(self, app: Any) -> None:
        self._auth(app)
        row = SimpleNamespace(state="draft", config_blob={})
        with (
            patch(
                "turncall.storage.repositories.agent_repo.get_agent_by_id",
                AsyncMock(return_value=row),
            ),
            patch(
                "turncall.storage.repositories.agent_repo.update_agent_config",
                AsyncMock(),
            ) as update,
        ):
            r = app.put(
                f"/v1/agents/{uuid.uuid4()}",
                headers={"Authorization": "Bearer x"},
                json={"config": self._CONFIG},
            )

        assert r.status_code == 422, r.text
        assert "knowledge-bases" in r.json()["error"]
        update.assert_not_awaited()

    def test_an_eval_inline_target_accepts_them(self) -> None:
        from turncall.api.v1.schemas.evals import EvalTarget

        target = EvalTarget(type="inline", agent=self._CONFIG)
        assert target.agent == self._CONFIG

    def test_the_schema_validates_them_as_the_link_endpoint_does(self) -> None:
        kb = str(uuid.uuid4())
        with pytest.raises(ValidationError, match="tool_description is required"):
            AgentConfigSchema(
                knowledge_bases=[{"knowledge_base_id": kb, "mode": "tool"}]
            )
        with pytest.raises(ValidationError, match="Extra inputs"):
            AgentConfigSchema(knowledge_bases=[{"knowledge_base_id": kb, "topk": 3}])

    def test_the_field_never_lands_in_a_stored_config(self) -> None:
        assert "knowledge_bases" not in AgentConfigSchema().model_dump()

    def test_a_tool_kb_counts_as_a_tool_for_openai_live_failover(self) -> None:
        """It brings a client delegate (#254), which can fail over (#246) — the
        schema can see an inline KB where it cannot see a link."""
        AgentConfigSchema(
            pipeline_mode="s2s",
            s2s={"provider": "openai_live"},
            llm={"failover": [{"provider": "openai", "model": "gpt-4.1-mini"}]},
            knowledge_bases=[
                {
                    "knowledge_base_id": str(uuid.uuid4()),
                    "mode": "tool",
                    "tool_description": "Search the menu.",
                }
            ],
        )
