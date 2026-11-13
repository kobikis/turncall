"""A provider's content filter refusing one turn (#456), over the running app.

pipecat 1.16 reports it as `ErrorCategory.CONTENT_FILTERED`, which leaves the
service usable: the call goes on, on the same LLM, and only that turn goes
unanswered. The call's event log says why. Seam A: the real app, stub
providers, a fake carrier.
"""

from __future__ import annotations

from typing import Any

import pytest
from pipecat.frames.frames import Frame, LLMContextFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.utils.errors import ErrorCategory

from tests.harness import HARNESS_AGENT, Given, Script, run_app
from tests.harness.stubs import StubLLM
from tests.integration.test_harness_call_procedures import _call, _remembering, _until
from turncall.orchestrator import pipeline_factory

pytestmark = pytest.mark.integration

AGENT = {**HARNESS_AGENT, "first_message": "Hello."}
REFUSAL = "Error code: 400 - content_filter"


class _FilteredOnceLLM(StubLLM):
    """Refuses its first inference for its content, then answers as scripted."""

    refused = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        if isinstance(frame, LLMContextFrame) and not self.refused:
            self.refused = True
            await self.push_error(REFUSAL, category=ErrorCategory.CONTENT_FILTERED)
            return
        await super().process_frame(frame, direction)


async def test_a_filtered_turn_is_recorded_and_the_call_keeps_its_llm(monkeypatch):
    answered: list[Given] = []
    backup_given: list[Given] = []
    script = Script(
        caller=("Something the filter refuses.", "Something else."),
        agent=(_remembering(answered, "Happy to help with that."),),
    )
    backup = {"provider": "openai", "model": "gpt-4.1-mini"}
    primary: list[_FilteredOnceLLM] = []

    def llm(config: Any, *_args: Any, system_instruction: str = "", **_kw: Any) -> Any:
        if config.llm.model == backup["model"]:
            return StubLLM((_remembering(backup_given, "Backup."),), system_instruction)
        primary.append(_FilteredOnceLLM(script.agent, system_instruction))
        return primary[-1]

    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        monkeypatch.setattr(pipeline_factory, "_create_llm_service", llm)
        agent = {**AGENT, "llm": {"failover": [backup]}}
        number = await app.bind_number(await app.create_agent(agent))
        call = await _call(app, number)
        await call.say(400)
        await _until(lambda: bool(primary) and primary[0].refused)
        await call.say(400)
        await _until(lambda: len(answered) >= 1)
        await call.wait_for_agent_speech(2)
        await call.hang_up()
        ended = await app.sink.wait_for("call.ended")
        events = (await app.api.get(f"/v1/calls/{call.call_id}/events")).json()

    [raised] = [
        e["payload"] for e in events["data"] if e["event_type"] == "error.raised"
    ]
    assert raised["content_filtered"] is True
    assert "content_filter" in raised["error"]
    # Not a failed call, and not a switch: the next turn is the same LLM's.
    assert ended["payload"]["ended_reason"] == "customer_ended_call"
    assert ended["payload"]["failovers"] == []
    assert backup_given == []
    assert answered[0][-1] == {"role": "user", "content": "Something else."}
