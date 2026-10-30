"""A move costs no extra inference (#445), over the running app.

A response that already held speech and reported a `move_to_step` is the
whole turn: the caller has been answered, so the move's result asks for no
inference of its own. One with no speech does ask for one, so the caller is
always answered. The rule lives in the pipeline after the LLM, so it holds
whichever member of an `llm.failover` group answered. Seam A: the real app,
stub providers, a fake carrier.

How a test counts inferences: every scripted reply records what it was
given. An inference that follows a tool result ends on that result; one
that answers the caller ends on the caller's turn.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from aiohttp import web
from pipecat.frames.frames import Frame, LLMContextFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.utils.errors import ErrorCategory

from tests.harness import HARNESS_AGENT, Given, Script, ToolCall, run_app
from tests.harness.stubs import StubLLM
from tests.integration.test_harness_call_procedures import (
    NO_INTERNET,
    _call,
    _move,
    _remembering,
    _tool_results,
    _until,
    _window,
)
from turncall.orchestrator import pipeline_factory

pytestmark = pytest.mark.integration

AGENT = {**HARNESS_AGENT, "first_message": "Hello.", "procedures": [NO_INTERNET]}


def _answers_the_caller(given: Given, said: str) -> bool:
    return given[-1] == {"role": "user", "content": said}


async def test_speech_and_a_move_in_one_response_is_one_inference(monkeypatch):
    given: list[Given] = []
    script = Script(
        caller=("The internet light is red.", "Okay, I unplugged it."),
        agent=(
            _remembering(given, ("That's the internet light.", _move("restart"))),
            _remembering(given, "Now wait two minutes."),
        ),
    )
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await app.bind_number(await app.create_agent(AGENT))
        call = await _call(app, number)
        await call.say(400)
        await call.wait_for_agent_speech(2)
        await call.say(400)
        await _until(lambda: len(given) >= 2)
        await call.wait_for_agent_speech(3)
        await call.hang_up()
        await app.sink.wait_for("call.ended")

    moved, next_turn = given
    assert _answers_the_caller(moved, "The internet light is red.")
    # The reply after the move is the caller's next turn, not a follow-up to
    # the tool result: the move's response was the whole turn.
    assert _answers_the_caller(next_turn, "Okay, I unplugged it.")
    [result] = _tool_results(next_turn)
    assert result["success"] is True
    assert "current step: restart (instruct)" in _window(next_turn)


async def test_a_move_with_no_speech_is_answered_by_one_inference(monkeypatch):
    given: list[Given] = []
    script = Script(
        caller=("The internet light is red.", "Okay, I unplugged it."),
        agent=(
            _remembering(given, _move("restart")),
            _remembering(given, "Please restart the router."),
            _remembering(given, "Now wait two minutes."),
        ),
    )
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await app.bind_number(await app.create_agent(AGENT))
        call = await _call(app, number)
        await call.say(400)
        await call.wait_for_agent_speech(2)
        await call.say(400)
        await _until(lambda: len(given) >= 3)
        await call.wait_for_agent_speech(3)
        await call.hang_up()
        await app.sink.wait_for("call.ended")

    moved, follow_up, next_turn = given
    assert _answers_the_caller(moved, "The internet light is red.")
    # One inference after the result, and what it said reached the caller.
    assert follow_up[-1]["role"] == "tool"
    [result] = _tool_results(follow_up)
    assert "Now at step 'restart'" in result["message"]
    assert _answers_the_caller(next_turn, "Okay, I unplugged it.")


async def test_a_refused_move_with_speech_is_read_on_the_next_turn(monkeypatch):
    given: list[Given] = []
    script = Script(
        caller=("It's working now.", "Yes, I waited."),
        agent=(
            # Skips the steps between: not a move from `lights`.
            _remembering(given, ("Great to hear.", _move("resolved"))),
            _remembering(given, "Did you restart it first?"),
        ),
    )
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await app.bind_number(await app.create_agent(AGENT))
        call = await _call(app, number)
        await call.say(400)
        await call.wait_for_agent_speech(2)
        await call.say(400)
        await _until(lambda: len(given) >= 2)
        await call.wait_for_agent_speech(3)
        await call.hang_up()
        await app.sink.wait_for("call.ended")

    refused_turn, next_turn = given
    assert _answers_the_caller(refused_turn, "It's working now.")
    # No inference of its own; the refusal is in the one the caller's next
    # turn makes, beside a window that did not move.
    assert _answers_the_caller(next_turn, "Yes, I waited.")
    [refused] = _tool_results(next_turn)
    assert refused["success"] is False
    assert "'resolved' is not an allowed move from 'lights'" in refused["message"]
    assert "current step: lights (ask)" in _window(next_turn)


class _Lookup:
    """A step's lookup tool, on its own webhook, answering after `delay` —
    so the move beside it settles first."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.url = ""
        self._runner: web.AppRunner | None = None

    async def __aenter__(self) -> _Lookup:
        app = web.Application()
        app.router.add_post("/router-status", self._answer)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.url = f"http://127.0.0.1:{port}/router-status"
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _answer(self, _request: web.Request) -> web.Response:
        await asyncio.sleep(self.delay)
        return web.json_response({"internet_light": "red"})


async def test_a_move_beside_another_tool_is_answered_once_both_are_in(monkeypatch):
    given: list[Given] = []
    script = Script(
        caller=("The internet light is red.", "Okay, I unplugged it."),
        agent=(
            _remembering(
                given,
                (
                    "Let me look at your router.",
                    _move("restart"),
                    ToolCall("router_status", {}),
                ),
            ),
            _remembering(given, "It's red on our side too; restart it."),
            _remembering(given, "Now wait two minutes."),
        ),
    )
    async with (
        _Lookup(delay=1.0) as lookup,
        run_app(
            script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        tool = {
            "name": "router_status",
            "description": "Read the router's lights",
            "parameters_schema": {"type": "object", "properties": {}},
            "webhook_url": lookup.url,
        }
        number = await app.bind_number(
            await app.create_agent({**AGENT, "tools": [tool]})
        )
        call = await _call(app, number)
        await call.say(400)
        await call.wait_for_agent_speech(2)
        await _until(lambda: len(given) >= 2)
        await call.wait_for_agent_speech(3)
        await call.say(400)
        await _until(lambda: len(given) >= 3)
        await call.hang_up()
        await app.sink.wait_for("call.ended")

    moved, follow_up, next_turn = given
    assert _answers_the_caller(moved, "The internet light is red.")
    # The lookup's result needs an answer, speech or not: one inference, with
    # both results in.
    assert follow_up[-1]["role"] == "tool"
    assert len(_tool_results(follow_up)) == 2
    assert _answers_the_caller(next_turn, "Okay, I unplugged it.")


class _RefusingLLM(StubLLM):
    """A primary whose key the provider refuses: every turn is unusable."""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        if not isinstance(frame, LLMContextFrame):
            await super().process_frame(frame, direction)
            return
        await self.push_error("401 invalid key", category=ErrorCategory.AUTHENTICATION)


async def test_the_rule_holds_after_an_llm_failover(monkeypatch):
    given: list[Given] = []
    script = Script(
        caller=("The internet light is red.", "Okay, I unplugged it."),
        agent=(
            _remembering(given, ("That's the internet light.", _move("restart"))),
            _remembering(given, "Now wait two minutes."),
        ),
    )
    backup = {"provider": "openai", "model": "gpt-4.1-mini"}

    def llm(config: Any, *_args: Any, system_instruction: str = "", **_kw: Any) -> Any:
        if config.llm.model == backup["model"]:
            return StubLLM(script.agent, system_instruction)
        return _RefusingLLM((), system_instruction)

    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        monkeypatch.setattr(pipeline_factory, "_create_llm_service", llm)
        agent = {**AGENT, "llm": {"failover": [backup]}}
        number = await app.bind_number(await app.create_agent(agent))
        call = await _call(app, number)
        await call.say(400)
        await call.wait_for_agent_speech(2)
        await call.say(400)
        await _until(lambda: len(given) >= 2)
        await call.wait_for_agent_speech(3)
        await call.hang_up()
        ended = await app.sink.wait_for("call.ended")

    # The backup answered the turn the primary could not, speech and move.
    assert [f["to"] for f in ended["payload"]["failovers"]] == ["openai/gpt-4.1-mini"]
    moved, next_turn = given
    assert _answers_the_caller(moved, "The internet light is red.")
    assert _answers_the_caller(next_turn, "Okay, I unplugged it.")
    assert "current step: restart (instruct)" in _window(next_turn)
