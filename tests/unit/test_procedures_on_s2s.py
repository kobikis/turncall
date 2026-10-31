"""Procedures on an S2S agent (#444): not applied, and said so.

The window is a context message placed before each turn, and a realtime model
has no such point — so on S2S the build skips an agent's procedures with a
warning, and the agent read carries the same message (the rule auto-mode
knowledge bases follow on S2S). One function, `procedures_on_s2s_warning`,
decides both, so the two cannot disagree. Seam 1 is the warnings an agent
read derives, seam 2 the build.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from loguru import logger

from tests.integration.test_procedures_api import procedure
from turncall.domain.models import AgentConfig
from turncall.orchestrator.config_warnings import config_warnings
from turncall.services.credentials import Credentials

pytestmark = pytest.mark.unit


def _s2s(**extra: Any) -> AgentConfig:
    return AgentConfig.model_validate(
        {
            "pipeline_mode": "s2s",
            "s2s": {"provider": "google"},
            "procedures": [procedure()],
            **extra,
        }
    )


def test_an_s2s_agent_with_procedures_is_told_they_are_ignored():
    [warning] = config_warnings(_s2s())

    assert warning["code"] == "procedures_ignored_on_s2s"
    assert warning["procedures"] == ["no_internet"]
    assert warning["provider"] == "google"
    assert "no_internet" in warning["message"] and "google" in warning["message"]


def test_cascade_and_an_s2s_agent_without_procedures_say_nothing():
    cascade = AgentConfig.model_validate({"procedures": [procedure()]})

    assert config_warnings(cascade) == []
    assert config_warnings(_s2s(procedures=[])) == []


async def _build(config: AgentConfig) -> tuple[Any, list[str]]:
    """Run build_call_pipeline; the create_pipeline mock and the warnings."""
    from turncall.orchestrator import pipeline_builder

    pipeline = MagicMock()
    pipeline.processors_with_metrics.return_value = []
    said: list[str] = []
    sink = logger.add(lambda m: said.append(str(m)), level="WARNING")
    try:
        with (
            patch.object(
                pipeline_builder, "create_pipeline", return_value=pipeline
            ) as cp,
            patch.object(pipeline_builder, "CallSession"),
            patch.object(
                pipeline_builder,
                "load_call_credentials",
                new=AsyncMock(return_value=Credentials(platform={"google": "k"})),
            ),
            patch(
                "turncall.orchestrator.knowledge_processor.load_agent_kb_attachments",
                new=AsyncMock(return_value=[]),
            ),
        ):
            await pipeline_builder.build_call_pipeline(
                config=config,
                transport="T",
                call_context=SimpleNamespace(
                    agent_id="a",
                    project_id="p",
                    is_eval=True,
                    records=False,
                    has_database=True,
                ),
                settings=SimpleNamespace(pipecat=None, byom=None),
                session_factory="SF",
            )
    finally:
        logger.remove(sink)
    return cp, said


async def test_the_build_skips_them_saying_so_and_the_call_runs_without():
    cp, said = await _build(_s2s())

    built = cp.call_args.kwargs["config"]
    assert "move_to_step" not in [t.name for t in built.tools]
    assert any(
        "procedures_skipped" in m and "no_internet" in m and "google" in m for m in said
    ), said


async def test_an_inline_s2s_agent_is_not_failed_for_procedures_it_never_runs():
    broken = procedure()
    broken["steps"][0]["moves"] = [{"to": "nowhere", "when": "ever"}]

    cp, said = await _build(_s2s(procedures=[broken]))

    cp.assert_called_once()
    assert any("procedures_skipped" in m for m in said), said
