"""The demo's agents: read once from a file, refused whole if any cannot run.

A demo call has no database behind it, so an agent may use only what needs
none. Anything else is refused when the server starts, naming the agent and
the field — never discovered by a visitor halfway through a call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from turncall.domain.models import AgentConfig

# The one built-in a demo call can run: it has nothing to record, only a
# goodbye to say. Every other built-in acts on a call row, a carrier or
# another agent, and none of those exist here.
DEMO_TOOLS = frozenset({"end_call"})


class DemoAgentError(ValueError):
    """An agent file the demo cannot run."""


def load_demo_agents(
    path: str | Path, *, max_call_seconds: int
) -> dict[str, AgentConfig]:
    """The agents in `path`, by name, each capped at `max_call_seconds`."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise DemoAgentError(f"cannot read the demo agents file {path}: {exc}") from exc
    if not isinstance(raw, dict) or not raw:
        raise DemoAgentError(
            f"{path} must be a JSON object of agent configs by name, with at least one"
        )
    return {
        str(name): _demo_agent(str(name), blob, max_call_seconds)
        for name, blob in raw.items()
    }


def _demo_agent(name: str, blob: Any, max_call_seconds: int) -> AgentConfig:
    # The API's schema first: it forbids unknown fields, so a typo is an error
    # here rather than a setting silently ignored on the call. An inline
    # config, so a plain http:// URL is not refused (#340).
    from turncall.api.v1.schemas import INLINE_CONFIG
    from turncall.api.v1.schemas.agents import AgentConfigSchema

    try:
        config = AgentConfig.model_validate(
            AgentConfigSchema.model_validate(blob, context=INLINE_CONFIG).model_dump()
        )
    except ValidationError as exc:
        raise DemoAgentError(f"demo agent {name!r} is invalid: {exc}") from exc
    unusable = _needs_a_platform(config)
    if unusable:
        raise DemoAgentError(
            f"demo agent {name!r} uses {', '.join(unusable)}, which a demo call "
            "cannot run: it has no database, project or carrier behind it"
        )
    return config.model_copy(
        update={
            "max_call_duration_seconds": min(
                config.max_call_duration_seconds, max_call_seconds
            )
        }
    )


def _needs_a_platform(config: AgentConfig) -> list[str]:
    """What in `config` reads or writes something only the platform has."""
    found = [
        f"the tool {tool.name!r}"
        for tool in config.tools
        if tool.name not in DEMO_TOOLS or tool.webhook_url
    ]
    if config.mcp_servers:
        found.append("MCP servers")
    if config.knowledge_bases:
        found.append("knowledge bases")
    if config.ambience is not None:
        found.append("ambience (an uploaded sound)")
    if config.avatar.enabled:
        found.append("a video avatar")
    return found
