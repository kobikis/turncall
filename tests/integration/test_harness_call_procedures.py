"""Following a procedure on a voice call (#442), over the running app.

An agent's `start: "call"` procedure runs from the first turn. Before each
caller turn the model is given a window — the current step, its moves, the
move back, and the steps up to `window_depth` ahead — as one `developer`
message right before the turn, replacing the previous turn's. The model
reports a move with `move_to_step`: an allowed move (or one back) is
accepted and the result says what to do at the new step; anything else is
refused naming the allowed moves, and the window stays where it was. An agent
with no procedures is given exactly what it was before. Seam A: the real app,
stub providers, a fake carrier.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from aiohttp import web

from tests.harness import HARNESS_AGENT, Given, Script, ToolCall, run_app
from tests.harness.twilio import place_inbound_call

pytestmark = pytest.mark.integration

CALLER = "+15550001234"
MARKER = "[Procedure]"

NO_INTERNET = {
    "name": "no_internet",
    "description": "Caller has no internet connection at home",
    "start": "call",
    "window_depth": 1,
    "steps": [
        {
            "id": "lights",
            "kind": "ask",
            "start": True,
            "text": "Ask which lights on the router are on.",
            "moves": [
                {"to": "power", "when": "the power light is off"},
                {"to": "restart", "when": "the internet light is red"},
            ],
        },
        {
            "id": "power",
            "kind": "check",
            "text": "Have the caller check the power cable.",
            "moves": [{"to": "technician", "when": "still no power light"}],
        },
        {
            "id": "restart",
            "kind": "instruct",
            "text": "Have the caller restart the router and wait 2 minutes.",
            "moves": [
                {
                    "to": "after_restart",
                    "when": "the caller confirms they waited 2 minutes",
                }
            ],
        },
        {
            "id": "after_restart",
            "kind": "check",
            "text": "Ask about the internet light again.",
            "moves": [{"to": "resolved", "when": "the internet light is green"}],
        },
        {"id": "resolved", "kind": "end", "text": "Close politely."},
        {
            "id": "technician",
            "kind": "escalate",
            "text": "Explain a technician will be booked.",
        },
    ],
}


def _move(to: str, procedure: str = "no_internet") -> ToolCall:
    return ToolCall("move_to_step", {"procedure": procedure, "to": to})


def _remembering(given: list[Given], reply: Any) -> Callable[[Given], Any]:
    def answer(context: Given) -> Any:
        given.append(context)
        return reply

    return answer


def _windows(given: Given) -> list[str]:
    return [
        m["content"]
        for m in given
        if isinstance(m.get("content"), str) and m["content"].startswith(MARKER)
    ]


def _window(given: Given) -> str:
    [window] = _windows(given)
    return window


def _tool_results(given: Given) -> list[dict[str, Any]]:
    """The tool results in the context. A built-in answers a JSON string,
    which pipecat encodes once more as the message's content."""
    results = []
    for m in given:
        if m["role"] == "tool":
            value = json.loads(m["content"])
            results.append(json.loads(value) if isinstance(value, str) else value)
    return results


async def _until(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("the call never got there")
        await asyncio.sleep(0.05)


async def _call(app: Any, number: str) -> Any:
    """A call to `number`, once its greeting has played."""
    call = await place_inbound_call(
        app.carrier, app.api, from_number=CALLER, to_number=number
    )
    await call.wait_for_agent_speech(1)  # the greeting
    return call


async def test_the_window_follows_an_accepted_move(monkeypatch):
    given: list[Given] = []
    script = Script(
        caller=(
            "My internet is down.",
            "The red one, with the globe.",
            "Okay, it's unplugged.",
        ),
        agent=(
            _remembering(given, "Which lights are on?"),
            # Speech and a move in one response, as a provider sends it: the
            # whole turn, with no inference after the result (#445).
            _remembering(given, ("That's the internet light.", _move("restart"))),
            _remembering(given, "Is it back?"),
        ),
    )
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await app.bind_number(
            await app.create_agent(
                {
                    **HARNESS_AGENT,
                    "first_message": "Hello.",
                    "procedures": [NO_INTERNET],
                }
            )
        )
        call = await _call(app, number)
        await call.say(400)
        await _until(lambda: len(given) >= 1)
        await call.wait_for_agent_speech(2)
        await call.say(400)
        await _until(lambda: len(given) >= 2)
        await call.wait_for_agent_speech(3)
        await call.say(400)
        await _until(lambda: len(given) >= 3)
        await call.hang_up()
        await app.sink.wait_for("call.ended")

    first, _moved, next_turn = given
    # The first turn: the start step, one step ahead, nothing beyond.
    window = _window(first)
    assert "current step: lights (ask)" in window
    assert "Do now: Ask which lights on the router are on." in window
    assert "→ restart  when: the internet light is red" in window
    assert "restart (instruct): Have the caller restart the router" in window
    assert "power (check): Have the caller check the power cable." in window
    for beyond in ("after_restart", "technician", "resolved"):
        assert beyond not in window, window
    # Right before the caller's turn.
    assert first[-2]["role"] == "developer" and first[-2]["content"] == window
    assert first[-1] == {"role": "user", "content": "My internet is down."}
    assert "move_to_step" in first.tools

    # The move is accepted, and its result says what to do at the new step.
    [result] = _tool_results(next_turn)
    assert result["success"] is True
    assert "Have the caller restart the router and wait 2 minutes." in result["message"]

    # The next turn's window is at the new step; only one window, ever.
    window = _window(next_turn)
    assert "current step: restart (instruct)" in window
    assert "← back to lights  (always allowed)" in window
    assert "after_restart (check): Ask about the internet light again." in window
    assert "resolved" not in window and "technician" not in window
    for each in given:
        assert len(_windows(each)) <= 1, list(each)


async def test_a_move_not_allowed_is_refused_and_moving_back_is_not(monkeypatch):
    given: list[Given] = []
    script = Script(
        caller=(
            "The internet light is red.",
            "Okay, it's back on.",
            "Wait, the power light is off too.",
        ),
        agent=(
            _remembering(given, _move("restart")),
            _remembering(given, "Restart it and wait two minutes."),
            # Skips the confirmation: not a move from `restart`.
            _remembering(given, _move("resolved")),
            _remembering(given, "Did you wait the full two minutes?"),
            # Back to the previous step, which has no move entry for it.
            _remembering(given, _move("lights")),
            _remembering(given, "Let's look at the lights again."),
        ),
    )
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await app.bind_number(
            await app.create_agent(
                {
                    **HARNESS_AGENT,
                    "first_message": "Hello.",
                    "procedures": [NO_INTERNET],
                }
            )
        )
        call = await _call(app, number)
        for turn, inferences in enumerate((2, 4, 6), start=2):
            await call.say(400)
            await _until(lambda n=inferences: len(given) >= n)
            await call.wait_for_agent_speech(turn)
        await call.hang_up()
        await app.sink.wait_for("call.ended")

    refused_turn, after_refusal, back_turn, after_back = given[2:]
    # Refused, naming what is allowed from where the procedure is.
    refused = _tool_results(after_refusal)[-1]
    assert refused["success"] is False
    assert "'resolved' is not an allowed move from 'restart'" in refused["message"]
    assert (
        "after_restart (when the caller confirms they waited 2 minutes)"
        in (refused["message"])
    )
    assert refused["details"]["allowed"] == ["after_restart", "lights"]
    # The window did not move.
    assert "current step: restart" in _window(refused_turn)
    assert "current step: restart" in _window(back_turn)

    back = _tool_results(after_back)[-1]
    assert back["success"] is True
    assert "Now at step 'lights'" in back["message"]


async def test_an_agent_with_no_procedures_is_given_what_it_was(monkeypatch):
    given: list[Given] = []
    script = Script(
        caller=("Hi.",), agent=(_remembering(given, "Hello, how can I help?"),)
    )
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await app.bind_number(
            await app.create_agent({**HARNESS_AGENT, "first_message": "Hello."})
        )
        call = await _call(app, number)
        await call.say(400)
        await _until(lambda: len(given) >= 1)
        await call.hang_up()
        await app.sink.wait_for("call.ended")

    [only] = given
    assert _windows(only) == []
    assert all(m["role"] != "developer" for m in only), list(only)
    assert "move_to_step" not in only.tools


async def _bind_to_call_init(app: Any, server_url: str) -> str:
    e164 = "+1555" + str(uuid.uuid4().int)[:7]
    number = app.carrier.add_number(e164)
    bound = await app.api.post(
        "/v1/phone-numbers",
        json={
            "e164_number": e164,
            "external_number_sid": number.sid,
            "routing_target_type": "webhook",
            "server_url": server_url,
        },
    )
    assert bound.status_code == 201, bound.text
    return e164


class _CallInit:
    """A customer's call-init server answering with an inline agent."""

    def __init__(self, agent: dict[str, Any]) -> None:
        self.agent = agent
        self.url = ""
        self._runner: web.AppRunner | None = None

    async def __aenter__(self) -> _CallInit:
        app = web.Application()
        app.router.add_post("/call-init", self._answer)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.url = f"http://127.0.0.1:{port}/call-init"
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _answer(self, _request: web.Request) -> web.Response:
        return web.json_response({"agent": self.agent})


async def test_a_call_init_inline_agent_follows_its_procedure(monkeypatch):
    given: list[Given] = []
    script = Script(
        caller=("My internet is down.",),
        agent=(_remembering(given, "Which lights are on?"),),
    )
    inline = {**HARNESS_AGENT, "first_message": "Hello.", "procedures": [NO_INTERNET]}
    async with (
        _CallInit(inline) as server,
        run_app(
            script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        number = await _bind_to_call_init(app, server.url)
        call = await _call(app, number)
        await call.say(400)
        await _until(lambda: len(given) >= 1)
        await call.hang_up()
        await app.sink.wait_for("call.ended")

    [first] = given
    assert "current step: lights (ask)" in _window(first)
    assert "move_to_step" in first.tools


PASSWORD = {
    "name": "reset_password",
    "description": "Caller forgot the password to their account",
    "start": "on_request",
    "steps": [
        {
            "id": "identify",
            "kind": "ask",
            "start": True,
            "text": "Ask for the email address on the account.",
            "moves": [{"to": "sent", "when": "the caller gave an email"}],
        },
        {"id": "sent", "kind": "end", "text": "Say a reset link is on its way."},
    ],
}
ON_REQUEST = {**NO_INTERNET, "start": "on_request"}


async def _procedure_events(
    app: Any, call_id: str, *, at_least: int, timeout: float = 10.0
) -> list[tuple[str, dict[str, Any]]]:
    """The call's `procedure.*` events in log order (#443), once there are
    `at_least` of them (or the timeout passed): written off the tool's path."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        response = await app.api.get(f"/v1/calls/{call_id}/events")
        assert response.status_code == 200, response.text
        events = [
            (e["event_type"], e["payload"])
            for e in response.json()["data"]
            if e["event_type"].startswith("procedure.")
        ]
        if len(events) >= at_least or asyncio.get_running_loop().time() > deadline:
            return events
        await asyncio.sleep(0.05)


def _prompt(given: Given) -> str:
    return next((m["content"] for m in given if m["role"] == "system"), "")


def _shown(given: Given) -> str:
    return json.dumps(list(given))


async def test_an_on_request_procedure_runs_once_the_model_starts_it(monkeypatch):
    """#446. Before the start the model has each procedure's name and
    description, never a step; a move in one not running is refused unless
    it starts it; once started, its window is at its start step — and the
    other procedure's steps are never shown. The start is recorded as a step
    from nowhere (#443)."""
    given: list[Given] = []
    script = Script(
        caller=("What are your hours?", "My internet is down.", "The red one."),
        agent=(
            _remembering(given, "Nine to five."),
            # Not its start step: refused, naming it.
            _remembering(given, _move("power")),
            _remembering(given, _move("lights")),
            _remembering(given, "Which lights are on?"),
            _remembering(given, "That's the internet light."),
        ),
    )
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        number = await app.bind_number(
            await app.create_agent(
                {
                    **HARNESS_AGENT,
                    "first_message": "Hello.",
                    "procedures": [ON_REQUEST, PASSWORD],
                }
            )
        )
        call = await _call(app, number)
        for turn, inferences in enumerate((1, 4, 5), start=2):
            await call.say(400)
            await _until(lambda n=inferences: len(given) >= n)
            await call.wait_for_agent_speech(turn)
        events = await _procedure_events(app, call.call_id, at_least=2)
        await call.hang_up()
        ended = await app.sink.wait_for("call.ended")

    before, refusing, starting, after_start, next_turn = given
    # Before: names and descriptions and how to start; no step, no window.
    prompt = _prompt(before)
    assert "- no_internet: Caller has no internet connection at home" in prompt
    assert "- reset_password: Caller forgot the password to their account" in prompt
    assert "start step: lights" in prompt and "start step: identify" in prompt
    assert _windows(before) == [] and _windows(refusing) == []
    assert "move_to_step" in before.tools
    for each in (before, refusing):
        for step in (*NO_INTERNET["steps"], *PASSWORD["steps"]):
            assert step["text"] not in _shown(each), step["id"]

    # A move in it before the start is refused, naming the start.
    [refused] = _tool_results(starting)
    assert refused["success"] is False
    assert (
        "'no_internet' is not running. Start it at its start step 'lights'"
        in (refused["message"])
    )

    # The start: its result says what to do at the start step.
    started = _tool_results(after_start)[-1]
    assert started["success"] is True
    assert "Started 'no_internet'" in started["message"]
    assert "Ask which lights on the router are on." in started["message"]

    # The next turn's window is at the start step, and only this procedure's.
    window = _window(next_turn)
    assert "current step: lights (ask)" in window
    assert "reset_password" not in window and "identify" not in window

    # The other procedure's steps never reach the model.
    for each in given:
        for step in PASSWORD["steps"]:
            assert step["text"] not in _shown(each), step["id"]

    # The refused move and the start, neither from a step.
    assert events == [
        (
            "procedure.refused",
            {"procedure": "no_internet", "from": None, "to": "power"},
        ),
        (
            "procedure.step",
            {"procedure": "no_internet", "from": None, "to": "lights", "kind": "ask"},
        ),
    ]
    assert ended["payload"]["procedures"] == [
        {
            "name": "no_internet",
            "path": ["lights"],
            "completed": False,
            "ended_at": "lights",
            "escalated_at": None,
            "refused_moves": 1,
        }
    ]
