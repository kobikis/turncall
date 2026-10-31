"""A procedure step's tool and the call's variables (#447), over the running app.

A step may name a tool to call there: the window says so ("tool:
line_status"), in the current step and in the preview, and the tool is an
ordinary agent tool — it reaches its webhook and is recorded in
`tool_invocations` like any other. A step's text renders the call's
`{{variables}}` like the rest of the agent config, in the window, the preview
and the accepted move's result. Seam A: the real app, stub providers, a fake
carrier, a customer's server answering call-init and the tool's webhook.
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
ACCOUNT = "ACC-4471"

NO_INTERNET = {
    "name": "no_internet",
    "steps": [
        {
            "id": "restart",
            "kind": "instruct",
            "start": True,
            "text": "Have the caller restart the router on account {{account_id}}.",
            "moves": [{"to": "after_restart", "when": "the caller restarted it"}],
        },
        {
            "id": "after_restart",
            "kind": "check",
            "tool": "line_status",
            "text": "Check the line status for {{account_id}}.",
            "moves": [{"to": "resolved", "when": "the line is fine"}],
        },
        {"id": "resolved", "kind": "end", "text": "Close politely."},
    ],
}


class Backend:
    """The customer's server: call-init on `/call-init` (the stored agent and
    the call's variables), every tool's webhook on its own path."""

    def __init__(self, variables: dict[str, str]) -> None:
        self.agent_id = ""
        self.variables = variables
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.url = ""
        self._runner: web.AppRunner | None = None

    async def __aenter__(self) -> Backend:
        app = web.Application()
        app.router.add_post("/{path:.*}", self._answer)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _answer(self, request: web.Request) -> web.Response:
        self.requests.append((request.path, await request.json()))
        if request.path == "/call-init":
            return web.json_response(
                {"agent_id": self.agent_id, "variables": self.variables}
            )
        return web.json_response({"line": "ok"})


def _remembering(given: list[Given], reply: Any) -> Callable[[Given], Any]:
    def answer(context: Given) -> Any:
        given.append(context)
        return reply

    return answer


def _window(given: Given) -> str:
    [window] = [
        m["content"]
        for m in given
        if isinstance(m.get("content"), str) and m["content"].startswith(MARKER)
    ]
    return window


def _tool_results(given: Given) -> list[dict[str, Any]]:
    """A built-in answers a JSON string, which pipecat encodes once more."""
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


async def _invocations(app: Any, call_id: str, count: int) -> list[dict[str, Any]]:
    """The call's tool invocation rows, once `count` are written (recorded
    off the tool's critical path)."""
    rows: list[dict[str, Any]] = []
    for _ in range(100):
        rows = (await app.api.get(f"/v1/tools/invocations/{call_id}")).json()["data"]
        if len(rows) >= count:
            break
        await asyncio.sleep(0.1)
    return rows


async def test_a_step_names_its_tool_and_renders_the_calls_variables(monkeypatch):
    given: list[Given] = []
    script = Script(
        caller=("I restarted it.", "Okay."),
        agent=(
            # A move with no speech, so its result is answered by another
            # inference whatever the move's latency rule.
            _remembering(
                given,
                ToolCall(
                    "move_to_step", {"procedure": "no_internet", "to": "after_restart"}
                ),
            ),
            _remembering(given, "Let me check your line."),
            _remembering(given, ToolCall("line_status", {})),
            _remembering(given, "Your line is fine."),
        ),
    )
    async with (
        Backend(variables={"account_id": ACCOUNT}) as backend,
        run_app(
            script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
    ):
        backend.agent_id = await app.create_agent(
            {
                **HARNESS_AGENT,
                "first_message": "Hello.",
                "tools": [
                    {
                        "name": "line_status",
                        "description": "Look up the caller's line status",
                        "parameters_schema": {"type": "object", "properties": {}},
                        "webhook_url": f"{backend.url}/line_status",
                    }
                ],
                "procedures": [NO_INTERNET],
            }
        )
        number = await _bind_to_call_init(app, f"{backend.url}/call-init")
        call = await place_inbound_call(
            app.carrier, app.api, from_number=CALLER, to_number=number
        )
        await call.wait_for_agent_speech(1)  # the greeting
        await call.say(400)
        await _until(lambda: len(given) >= 2)
        await call.wait_for_agent_speech(2)
        await call.say(400)
        await _until(lambda: len(given) >= 4)
        await call.wait_for_agent_speech(3)
        rows = await _invocations(app, call.call_id, 2)
        await call.hang_up()
        await app.sink.wait_for("call.ended")

    at_restart, after_move, at_check, answered = given
    # The step's text and the preview, rendered; the preview names the tool.
    window = _window(at_restart)
    assert f"Do now: Have the caller restart the router on account {ACCOUNT}." in (
        window
    )
    assert (
        f"after_restart (check, tool: line_status): Check the line status for "
        f"{ACCOUNT}." in window
    ), window
    assert "{{" not in window
    # The accepted move's result carries the next step's rendered text.
    [moved] = _tool_results(after_move)
    assert moved["success"] is True
    assert f"Check the line status for {ACCOUNT}." in moved["message"]

    # At the step: the window names the tool, which the model was offered.
    window = _window(at_check)
    assert "current step: after_restart (check, tool: line_status)" in window
    assert f"Do now: Check the line status for {ACCOUNT}." in window
    assert "line_status" in at_check.tools

    # An ordinary agent tool: its webhook was reached, and it was recorded.
    [(_, posted)] = [r for r in backend.requests if r[0] == "/line_status"]
    assert posted["tool_name"] == "line_status"
    assert posted["call_id"] == call.call_id
    by_name = {r["tool_name"]: r for r in rows}
    assert by_name["line_status"]["status"] == "succeeded", rows
    assert "move_to_step" in by_name, rows

    # The step's own tool says where to move now, as the model reads it; the
    # record keeps what the tool answered.
    [read] = [m["content"] for m in answered if m["role"] == "tool"][-1:]
    assert (
        "[Procedure] 'no_internet' is at step 'after_restart', whose tool this "
        "was. Report the move with move_to_step before you reply: resolved "
        "(when the line is fine); back to restart." in read
    ), read
    assert "[Procedure]" not in json.dumps(by_name["line_status"]["output_json"])
