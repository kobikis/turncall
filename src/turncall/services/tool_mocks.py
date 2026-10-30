"""Canned tool responses for an eval iteration, and the fail-closed policy
around them (#71, ADR-0018).

`pipecat.evals` has no tool mocking — a `function_call` expectation only
observes that a call happened — so without this a scenario exercising a booking
tool books a real appointment on every iteration, and the agent's MCP servers
are connected exactly as in production.

The scenario's mocks reach the tool paths through `CallContext` and
short-circuit the call *before* it is dispatched: webhook, MCP and built-in
alike, since all three act on something outside the eval. Under the default
`mock_only` an unmocked call is refused instead of executed, and the iteration
is errored naming the tool.

`load_skill` (#431) is the one exception: it reaches nothing outside the eval,
it changes the agent under test — the skill's instructions and tools join the
prompt — so it always runs, and a scenario's mock for it is ignored (the run
says so, `run_warnings.warn_never_mocked`). Mocking it would test an agent
that never gets the skill. The skill's own tools are tools like any other:
mockable, and refused unmocked.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from loguru import logger

from turncall.config.settings import get_settings
from turncall.domain.procedures import MOVE_TO_STEP
from turncall.services.tool_webhook import cap_tool_result

# What happened to one tool call, for the iteration's record.
MOCKED = "mocked"  # the canned response was returned, nothing was dispatched
EXECUTED = "executed"  # `live`: the real tool ran
REFUSED = "refused"  # no mock under `mock_only`: nothing ran, the run is errored
APPLIED = "applied"  # never mocked: it changed the agent under test, nothing else

# Tools that act on the agent under test and nothing else, so an eval runs
# them whatever its mocks and policy say: each name with what it changes, for
# the run's warning when a scenario mocks one anyway. `load_skill` (#431) adds
# a skill to the agent; `move_to_step` (#449) moves it in a procedure, so a
# canned "accepted" would hide the very skip a procedure eval is there to catch.
NEVER_MOCKED: Mapping[str, str] = MappingProxyType(
    {
        "load_skill": "loads a skill into the agent",
        MOVE_TO_STEP: "moves the agent through its procedure",
    }
)


def encode_mock(response: Any) -> str:
    """A mock as the string a tool handler returns.

    Authors write mocks as JSON objects, but a tool result is a string to the
    model either way, so a string mock is passed through verbatim rather than
    JSON-quoted.
    """
    return response if isinstance(response, str) else json.dumps(response)


@dataclass
class ToolMocks:
    """One iteration's mocks, the policy around them, and what happened.

    Mutable on purpose: it is the only channel the tool paths have back to the
    runner. `CallContext` is frozen, and an eval has no `calls` row, so the
    invocation record that a real call writes to `tool_invocations` has nowhere
    to go — `calls` becomes the iteration's tool record instead, and `refused`
    is what errors it.
    """

    responses: dict[str, Any] = field(default_factory=dict)
    live: bool = False
    refused: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self, name: str, args: dict[str, Any], result: str, *, outcome: str
    ) -> None:
        """Record one tool call for the iteration's results entry.

        Three outcomes, not two: a `refused` call and one that really executed
        under `live` are opposites, and a single `mocked: false` flag made them
        identical in the only record an eval has.
        """
        self.calls.append(
            {
                "tool_name": name,
                "arguments": args,
                "result": result,
                "outcome": outcome,
            }
        )

    def ran(self, name: str, args: dict[str, Any], result: str) -> None:
        """Record a tool that really ran: `applied` for one never mocked,
        `executed` for one a `live` policy let through."""
        self.record(
            name, args, result, outcome=APPLIED if name in NEVER_MOCKED else EXECUTED
        )


def intercept(mocks: ToolMocks | None, name: str, args: dict[str, Any]) -> str | None:
    """The canned result for this call, or None to execute it for real.

    A named mock always wins: it is the explicit intent of the test, and
    `live` only decides what happens to the tools no mock covers.
    """
    if mocks is None or name in NEVER_MOCKED:
        return None

    if name in mocks.responses:
        # Capped like any other tool result: a mock reaches the model by the
        # same route a webhook's answer does, and stays in the context for the
        # rest of the conversation. Skipping the cap here would make the mock
        # the one way past a limit every other path honours.
        result = cap_tool_result(
            encode_mock(mocks.responses[name]),
            get_settings().tools.max_response_bytes,
            tool=name,
            source="mock",
        )
        mocks.record(name, args, result, outcome=MOCKED)
        return result

    if mocks.live:
        return None

    # Fail closed. The model is handed an error so the turn completes rather
    # than hanging, but the iteration is errored on `refused` whatever it does
    # with it — a refused side effect is not a verdict about the agent.
    reason = f"unmocked tool: {name}"
    mocks.refused.append(name)
    mocks.record(name, args, json.dumps({"error": reason}), outcome=REFUSED)
    logger.warning("eval_unmocked_tool_refused", tool=name)
    return json.dumps({"error": reason})
