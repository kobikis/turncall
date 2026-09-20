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
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from loguru import logger


def _encode(response: Any) -> str:
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
        self, name: str, args: dict[str, Any], result: str, *, mocked: bool
    ) -> None:
        """Record one tool call for the iteration's results entry."""
        self.calls.append(
            {"tool_name": name, "arguments": args, "result": result, "mocked": mocked}
        )


def intercept(mocks: ToolMocks | None, name: str, args: dict[str, Any]) -> str | None:
    """The canned result for this call, or None to execute it for real.

    A named mock always wins: it is the explicit intent of the test, and
    `live` only decides what happens to the tools no mock covers.
    """
    if mocks is None:
        return None

    if name in mocks.responses:
        result = _encode(mocks.responses[name])
        mocks.record(name, args, result, mocked=True)
        return result

    if mocks.live:
        return None

    # Fail closed. The model is handed an error so the turn completes rather
    # than hanging, but the iteration is errored on `refused` whatever it does
    # with it — a refused side effect is not a verdict about the agent.
    reason = f"unmocked tool: {name}"
    mocks.refused.append(name)
    mocks.record(name, args, json.dumps({"error": reason}), mocked=False)
    logger.warning("eval_unmocked_tool_refused", tool=name)
    return json.dumps({"error": reason})
